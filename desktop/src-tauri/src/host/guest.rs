//! Starting a guest, finding its window, and being certain it dies.
//!
//! Nothing in here is specific to the synthetic guest. Launching a process,
//! waiting for a top-level window of a known class to appear in it, and
//! confining it to a job object is exactly what `HostBackend::launch` will do
//! for `WINWORD.EXE` — the class is `OpusApp` instead of
//! [`SYNTHETIC_GUEST_CLASS`] and the wait is longer. That is the point of
//! proving the mechanism against a guest we wrote: the code that ships is the
//! same code either way.
//!
//! **Finding the window by class and pid, not by a private channel.** The
//! synthetic guest *does* print its `HWND` on stdout, and the integration test
//! reads it — but only to check that the finder found the right window. Word
//! will never print us anything, so a handshake would have been a test of code
//! that cannot ship.
//!
//! **The job object is load-bearing.** The spike that preceded this PR measured
//! a real Word surviving `Application.Quit` and only dying when the job handle
//! closed. So a guest is put in a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
//! at launch and reaped by closing that handle — a kill we cannot forget to
//! perform and that survives our own crash, because the OS closes handles for
//! processes that stop existing.
//!
//! The job is created *before* the spawn and assigned immediately after, which
//! is a weaker guarantee than `backend.rs` gets by joining its job before
//! spawning anything. It is the right trade here and the difference is
//! deliberate: `backend.rs` spawns `uv`, a launcher whose *grandchild* holds the
//! port, so there is a real window in which that grandchild can be created
//! outside the job. A guest is the window owner itself and spawns nothing, so
//! the only process that could escape is one it never creates. Making even that
//! impossible means `CreateProcessW` with `PROC_THREAD_ATTRIBUTE_JOB_LIST`, and
//! with it a hand-rolled replacement for `std::process::Command`.

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use windows::core::BOOL;
use windows::Win32::Foundation::{HANDLE, HWND, LPARAM};
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetClassNameW, GetWindowThreadProcessId, IsWindow, IsWindowVisible, WM_APP,
};

use super::{HostError, HostErrorCode, WindowId};

/// The window class the synthetic guest registers. The stand-in for `OpusApp`
/// (Word) and `XLMAIN` (Excel).
pub const SYNTHETIC_GUEST_CLASS: &str = "WorkbenchSyntheticGuest";

/// How tall a strip the synthetic guest draws for itself, in physical pixels at
/// 100%. It exists so the clip offset has something real to hide — Word's own
/// caption row is the thing this models.
pub const SYNTHETIC_GUEST_CAPTION: i32 = 28;

/// Post this to the guest with a millisecond count in `wParam` and its window
/// procedure stops returning for that long. The whole point of the fixture:
/// a hang we can schedule.
pub const SYNTHETIC_GUEST_HANG: u32 = WM_APP + 1;

/// The line the synthetic guest prints once its window exists, used by the
/// integration test to check the finder against the truth.
pub const SYNTHETIC_GUEST_HANDSHAKE: &str = "guest-hwnd ";

/// How often to look for the guest's window while it is starting.
const FIND_POLL_INTERVAL: Duration = Duration::from_millis(20);

/// How long [`GuestProcess::reap`] waits for a condemned guest to actually go.
///
/// Termination through a job object is asynchronous, so without a wait `reap`
/// would return while the instance is still on screen. Two seconds is a bound,
/// not an expectation: a synthetic guest goes in single-digit milliseconds and a
/// real Office instance mid-save is what the far end of it is for.
const REAP_TIMEOUT: Duration = Duration::from_secs(2);

/// A launched instance: the process, and the top-level window it produced.
pub struct LaunchedGuest {
    pub process: GuestProcess,
    pub window: WindowId,
}

/// A process we started, and the job object that guarantees its end.
pub struct GuestProcess {
    child: Child,
    pid: u32,
    job: Option<Job>,
}

impl GuestProcess {
    pub fn pid(&self) -> u32 {
        self.pid
    }

    /// The child's own stdout, once.
    ///
    /// A piped stream nobody drains is a child that blocks when the pipe
    /// fills, so somebody has to own these: the shell pumps them into
    /// `shell.log` ([`crate::backend::pump`]), and the hosting tests read the
    /// fixture's handshake line off stdout before doing that.
    pub fn take_stdout(&mut self) -> Option<std::process::ChildStdout> {
        self.child.stdout.take()
    }

    pub fn take_stderr(&mut self) -> Option<std::process::ChildStderr> {
        self.child.stderr.take()
    }

    /// Has it exited? The liveness half of the Protocol's `poll`.
    pub fn is_running(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Condemn the instance: close the job, which terminates every process in
    /// it.
    ///
    /// **Instant, and that is the point.** Closing the handle is a
    /// `CloseHandle`; the termination it triggers is the kernel's business and
    /// happens after we return. So the *decision* to kill can stay wherever it
    /// belongs — including on the main thread, in the middle of closing a panel
    /// — while the waiting for it goes elsewhere ([`super::reaper`]).
    ///
    /// Safe to call twice: the second call has no job left to close.
    pub fn kill(&mut self) {
        drop(self.job.take());
    }

    /// Kill it and wait until it is really gone.
    ///
    /// **Never from the thread that owns hosted windows.** The wait runs up to
    /// [`REAP_TIMEOUT`], and a UI thread spending that here is a window frozen
    /// for two seconds per closed panel — which is exactly the bug
    /// [`super::reaper`] exists to keep out of `host_close`. Everything on the
    /// shell's side reaches this through there; the tests call it directly
    /// because a test thread is allowed to wait.
    pub fn reap(&mut self) {
        self.reap_within(REAP_TIMEOUT);
    }

    /// [`Self::reap`] with the bound named by the caller.
    ///
    /// Two callers name their own. A test measures that the wait is real
    /// without spending the production timeout to do it; and [`super::reaper`]'s
    /// fallback — the path taken when no reaper thread could be started — waits
    /// on the *caller's* thread and so answers to a much shorter number than
    /// [`REAP_TIMEOUT`], which is sized for a thread that owns nothing.
    pub(super) fn reap_within(&mut self, timeout: Duration) {
        self.kill();
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            match self.child.try_wait() {
                Ok(Some(_)) | Err(_) => return,
                Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            }
        }
        crate::backend::log(&format!(
            "office host: guest pid {} outlived its job object",
            self.pid
        ));
    }

    /// Test-only: take the job object away, so nothing this process does can
    /// kill the guest and its `reap` runs to the full bound.
    ///
    /// That is the **slow-dying instance** — a real Office busy enough not to go
    /// at once — made deterministic, without having to find one. The returned
    /// guard still closes the job when it is dropped, so a test cannot leave a
    /// guest behind on the desktop.
    #[cfg(test)]
    #[must_use = "dropping the guard is the only thing left that can kill the guest"]
    pub(super) fn detach_job_for_test(&mut self) -> Box<dyn Send> {
        Box::new(self.job.take())
    }
}

impl Drop for GuestProcess {
    /// The kill, never the wait.
    ///
    /// A `GuestProcess` is dropped wherever its owner happens to be — a failed
    /// launch, a registry entry going away — and that is often the main thread.
    /// A drop that waited would reintroduce, silently and everywhere, the freeze
    /// [`Self::reap`] is kept off that thread to avoid. Whoever needs certainty
    /// that the instance is gone asks [`super::reaper`] for it.
    fn drop(&mut self) {
        self.kill();
    }
}

/// A job handle whose *closing* is the kill.
struct Job(HANDLE);

// SAFETY: the handle is only ever stored and closed. No call takes `&mut`, and
// none is made from more than one place at a time, which is what makes sharing
// the owning struct across threads sound. Same reasoning as `backend::JobHandle`.
unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Drop for Job {
    fn drop(&mut self) {
        // SAFETY: a handle we created and have not closed. Closing the last
        // handle to a kill-on-close job terminates its processes; that is the
        // intended effect, not a side effect.
        let _ = unsafe { windows::Win32::Foundation::CloseHandle(self.0) };
    }
}

/// Start `exe`, wait for a visible top-level window of `class` to appear in it,
/// and return both.
///
/// Fails — and reaps — rather than leaving a process we cannot host.
pub fn launch(
    exe: &Path,
    args: &[&str],
    class: &str,
    timeout: Duration,
) -> Result<LaunchedGuest, HostError> {
    let job = create_job()?;
    let mut command = Command::new(exe);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(windows::Win32::System::Threading::CREATE_NO_WINDOW.0);
    }
    let child = command.spawn().map_err(|err| {
        HostError::new(
            HostErrorCode::LaunchFailed,
            format!("could not start {}: {err}", exe.display()),
        )
    })?;
    let pid = child.id();
    assign_to_job(&job, &child)?;
    let mut process = GuestProcess {
        child,
        pid,
        job: Some(job),
    };

    let deadline = Instant::now() + timeout;
    loop {
        if let Some(window) = find_window(pid, class) {
            return Ok(LaunchedGuest { process, window });
        }
        if !process.is_running() {
            return Err(HostError::new(
                HostErrorCode::LaunchFailed,
                format!("{} exited before it showed a window", exe.display()),
            ));
        }
        if Instant::now() >= deadline {
            // Dropping `process` closes the job and kills it: a launch that
            // timed out must not leave an instance nobody owns.
            return Err(HostError::new(
                HostErrorCode::LaunchFailed,
                format!(
                    "no {class} window from {} within {timeout:?}",
                    exe.display()
                ),
            ));
        }
        std::thread::sleep(FIND_POLL_INTERVAL);
    }
}

/// The first visible top-level window of `class` belonging to `pid`.
///
/// This is how the native backend will identify Word's frame window, so it is
/// deliberately conservative: the class must match exactly, the process must
/// match, and an invisible window (Office creates several while starting) does
/// not count.
pub fn find_window(pid: u32, class: &str) -> Option<WindowId> {
    let mut state = Finder {
        pid,
        class: class.to_string(),
        found: 0,
    };
    // SAFETY: the pointer is to a stack value that outlives the enumeration —
    // `EnumWindows` is synchronous — and `enum_windows_proc` only ever reads
    // and writes through it as a `Finder`.
    let _ = unsafe {
        EnumWindows(
            Some(enum_windows_proc),
            LPARAM(std::ptr::addr_of_mut!(state) as isize),
        )
    };
    (state.found != 0).then_some(WindowId(state.found))
}

/// Does this window still exist? The other half of the Protocol's `poll`: a
/// process can outlive the window we hosted.
pub fn window_exists(window: WindowId) -> bool {
    if window.is_null() {
        return false;
    }
    // SAFETY: a plain Win32 call that validates the handle itself.
    unsafe { IsWindow(Some(window.hwnd())).as_bool() }
}

struct Finder {
    pid: u32,
    class: String,
    found: isize,
}

unsafe extern "system" fn enum_windows_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
    // SAFETY: `lparam` is the `Finder` pointer passed to `EnumWindows` above,
    // valid for the whole enumeration and used by no other thread.
    let Some(state) = (unsafe { (lparam.0 as *mut Finder).as_mut() }) else {
        return BOOL(0);
    };
    let mut pid = 0u32;
    // SAFETY: plain Win32 calls on the handle the enumeration just handed us.
    unsafe {
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        if pid != state.pid || !IsWindowVisible(hwnd).as_bool() {
            return BOOL(1);
        }
        let mut buffer = [0u16; 256];
        let length = GetClassNameW(hwnd, &mut buffer);
        if length <= 0 {
            return BOOL(1);
        }
        let name = String::from_utf16_lossy(&buffer[..length as usize]);
        if name != state.class {
            return BOOL(1);
        }
    }
    state.found = hwnd.0 as isize;
    // FALSE stops the enumeration: we have what we came for.
    BOOL(0)
}

fn create_job() -> Result<Job, HostError> {
    // SAFETY: the job is created here and the limit structure outlives the call
    // that reads it.
    unsafe {
        let job = CreateJobObjectW(None, windows::core::PCWSTR::null())?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            std::ptr::addr_of!(info).cast(),
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )?;
        Ok(Job(job))
    }
}

fn assign_to_job(job: &Job, child: &Child) -> Result<(), HostError> {
    use std::os::windows::io::AsRawHandle;
    // SAFETY: the child's handle is owned by `child`, which outlives this call.
    unsafe { AssignProcessToJobObject(job.0, HANDLE(child.as_raw_handle())) }.map_err(|err| {
        HostError::new(
            HostErrorCode::LaunchFailed,
            format!("could not confine the guest to a job object: {err}"),
        )
    })
}

/// Where the synthetic guest executable is.
///
/// Debug builds only. The fixture is a sibling binary of the shell, so it sits
/// next to it in `target/debug`; the integration test finds it through
/// `CARGO_BIN_EXE_workbench-guest` instead and never comes through here.
#[cfg(debug_assertions)]
pub fn synthetic_guest_exe() -> Result<PathBuf, HostError> {
    let exe = std::env::current_exe().map_err(|err| {
        HostError::new(
            HostErrorCode::LaunchFailed,
            format!("could not locate the running executable: {err}"),
        )
    })?;
    // Next to the shell binary in a normal run; one level up from
    // `target/debug/deps` when a test binary is the one asking.
    let candidate = exe
        .parent()
        .into_iter()
        .flat_map(|dir| [dir.to_path_buf(), dir.join("..")])
        .map(|dir| dir.join("workbench-guest.exe"))
        .find(|path| path.is_file());
    candidate.ok_or_else(|| {
        HostError::new(
            HostErrorCode::LaunchFailed,
            format!(
                "workbench-guest.exe is not next to {} — build it with \
                 `cargo build --bin workbench-guest`",
                exe.display()
            ),
        )
    })
}

/// Keeps the import honest in release builds, where the locator above does not
/// exist.
#[cfg(not(debug_assertions))]
#[allow(dead_code)]
fn unused_path_marker(_: &PathBuf) {}
