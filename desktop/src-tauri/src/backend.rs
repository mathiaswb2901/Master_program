//! Supervision of the FastAPI backend the UI talks to.
//!
//! Two modes, decided by one probe of `GET /api/health`:
//!
//! * **Attach** — a Workbench backend already answers on the backend port. That
//!   is the developer's own `uv run workbench-server`, and it keeps owning the
//!   workspace (the server's CWD *is* the workspace), which is the contract the
//!   app has today. We touch nothing.
//! * **Spawn** — nothing answers, so the shell starts one from the repo root
//!   with no console window and pipes its output into the shell log.
//!
//! A spawned child is confined by a Windows **Job Object** with
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, which this process joins *before*
//! spawning anything (see `confine_self_to_job`). The job handle is then held
//! for the lifetime of the process and deliberately never closed: when this
//! process dies — cleanly, by crash, or by Task Manager — the OS closes its
//! handles, the job closes, and every descendant dies with it. That is the
//! point. A graceful "kill the child on quit" path was measured on the target
//! machine to leave orphaned servers behind; the job object needs no code of
//! ours to run at exit.
//!
//! Deliberately not `bundle.externalBin` / `tauri-plugin-shell` sidecars: both
//! have documented orphan bugs (tauri#11686, tauri#2464) and neither gives the
//! kill-on-close guarantee above.

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Matches `Settings.host`/`Settings.port` in `server/src/workbench_server/config.py`.
const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8787;

/// Long enough to cross a loopback connect, short enough that a cold start is
/// not perceived as a hang.
const PROBE_TIMEOUT: Duration = Duration::from_millis(500);
/// A status line plus a small JSON body is all `/api/health` returns; anything
/// beyond this is a listener we would not attach to anyway.
const MAX_HEALTH_RESPONSE: usize = 4096;
/// How long a spawned server gets to answer before we stop waiting for it.
///
/// The window is already open by then (`lib.rs` supervises on a worker thread)
/// and the UI is showing that it is starting, so this budget buys a cold
/// `uv sync` rather than blocking a double-click into nothing.
const READY_TIMEOUT: Duration = Duration::from_secs(30);
const READY_POLL_INTERVAL: Duration = Duration::from_millis(150);
/// Keep the shell log from growing without bound across runs while still
/// carrying the previous run, which is where a crash's evidence is.
const MAX_LOG_BYTES: u64 = 1024 * 1024;

/// How the shell got its backend. Recorded so the log says which one happened.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// A server was already listening; it owns the workspace, we just use it.
    Attached,
    /// We started one, and the job object owns its lifetime.
    Spawned,
    /// Nothing was listening and we could not start one (see the log).
    Unavailable,
}

/// Live backend state, kept in Tauri's managed state so it lives as long as the
/// app does. Both fields exist purely to be *held*: dropping either would end
/// the child, which is exactly what we want to happen at process exit and never
/// before.
pub struct Backend {
    pub mode: Mode,
    _child: Option<Child>,
    #[cfg(windows)]
    _job: Option<JobHandle>,
}

// ---- Logging -----------------------------------------------------------------

static LOG_FILE: Mutex<Option<File>> = Mutex::new(None);
static STARTED: OnceLock<Instant> = OnceLock::new();

/// Also write the shell log to `dir/shell.log`.
///
/// Release builds are `windows_subsystem = "windows"` and therefore have **no**
/// stderr — in exactly the build a non-developer runs, every supervision
/// diagnostic would otherwise vanish: which mode was chosen, "no repo root
/// found", a failed `uv run`, a backend that never answered, and every line the
/// child printed. The file is the copy that always exists.
pub fn open_log(dir: Option<PathBuf>) {
    let Some(dir) = dir else {
        log("no app log directory; the shell log is stderr only");
        return;
    };
    if let Err(err) = std::fs::create_dir_all(&dir) {
        log(&format!("could not create {}: {err}", dir.display()));
        return;
    }
    let path = dir.join("shell.log");
    // Append so a crash and the relaunch that follows it sit in one file, but
    // do not let that file grow forever.
    let too_big = std::fs::metadata(&path).is_ok_and(|meta| meta.len() > MAX_LOG_BYTES);
    match OpenOptions::new()
        .create(true)
        .append(!too_big)
        .write(true)
        .truncate(too_big)
        .open(&path)
    {
        Ok(file) => {
            if let Ok(mut slot) = LOG_FILE.lock() {
                *slot = Some(file);
            }
            log(&format!("shell log: {}", path.display()));
        }
        Err(err) => log(&format!("could not open {}: {err}", path.display())),
    }
}

/// One line of shell log, to stderr when there is one and to the log file when
/// it is open.
///
/// Not `eprintln!`: that panics when the write fails, and a GUI-subsystem
/// process has a null `STD_ERROR_HANDLE`. A panic here would take down the pump
/// threads and any caller on the main thread — over a log line.
pub fn log(message: &str) {
    let elapsed = STARTED.get_or_init(Instant::now).elapsed().as_secs_f32();
    let line = format!("[workbench-shell +{elapsed:.2}s] {message}\n");
    let _ = std::io::stderr().write_all(line.as_bytes());
    if let Ok(mut slot) = LOG_FILE.lock() {
        if let Some(file) = slot.as_mut() {
            let _ = file.write_all(line.as_bytes());
            let _ = file.flush();
        }
    }
}

// ---- Supervision -------------------------------------------------------------

/// Probe first, spawn only if nobody answers.
pub fn start() -> Backend {
    let addr = backend_addr();

    match probe(addr) {
        Probe::Workbench => {
            log(&format!(
                "attached to the backend already listening on {addr}"
            ));
            return Backend {
                mode: Mode::Attached,
                _child: None,
                #[cfg(windows)]
                _job: None,
            };
        }
        Probe::Foreign => {
            // Attaching here is how you get a window whose every `/api/*` call
            // 404s while the log says the backend is fine.
            log(&format!(
                "something else is listening on {addr} and it is not a Workbench \
                 backend; refusing to attach — free the port or set WORKBENCH_PORT"
            ));
            return unavailable();
        }
        Probe::Nothing => {}
    }

    let Some(root) = repo_root() else {
        log(
            "no repo root found (looked for pyproject.toml + server/ above the \
             executable and the working directory) — start the backend yourself \
             with `uv run workbench-server`",
        );
        return unavailable();
    };

    // Before the spawn, never after: see `confine_self_to_job`.
    #[cfg(windows)]
    let job = match confine_self_to_job() {
        Ok(job) => Some(job),
        Err(err) => {
            // Nothing has been started yet, so refusing costs the user a
            // backend they can start themselves — where spawning anyway would
            // cost them an orphan holding the port for every future launch.
            log(&format!(
                "job object setup failed ({err}); refusing to spawn a backend we \
                 could not reap — start one yourself with `uv run workbench-server`"
            ));
            return unavailable();
        }
    };

    log(&format!(
        "no backend on {addr}; starting one in {}",
        root.display()
    ));
    let mut command = Command::new("uv");
    command
        .args(["run", "workbench-server"])
        .current_dir(&root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    command.creation_flags(windows::Win32::System::Threading::CREATE_NO_WINDOW.0);

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            log(&format!("could not start `uv run workbench-server`: {err}"));
            return unavailable();
        }
    };

    if let Some(stdout) = child.stdout.take() {
        pump(stdout, "out");
    }
    if let Some(stderr) = child.stderr.take() {
        pump(stderr, "err");
    }
    wait_until_ready(addr);

    Backend {
        mode: Mode::Spawned,
        _child: Some(child),
        #[cfg(windows)]
        _job: job,
    }
}

fn unavailable() -> Backend {
    Backend {
        mode: Mode::Unavailable,
        _child: None,
        #[cfg(windows)]
        _job: None,
    }
}

/// `WORKBENCH_HOST`/`WORKBENCH_PORT` are the server's own settings
/// (pydantic-settings, prefix `WORKBENCH_`). `ui/vite.config.ts` falls back to
/// the same pair, so one override moves the shell, the server it spawns and the
/// proxy the page talks through together.
fn backend_addr() -> SocketAddr {
    let port = backend_port();
    let configured = std::env::var("WORKBENCH_HOST").unwrap_or_default();
    // A server bound to the wildcard is still reached over loopback from here.
    let host = match configured.as_str() {
        "" | "0.0.0.0" | "::" => DEFAULT_HOST,
        other => other,
    };
    format!("{host}:{port}")
        .parse()
        .unwrap_or_else(|_| SocketAddr::from(([127, 0, 0, 1], port)))
}

/// The backend port this shell talks to: `WORKBENCH_PORT` if set and parseable,
/// else the default. Shared with `auth_token`, which reads the token file the
/// server keys by exactly this port.
pub(crate) fn backend_port() -> u16 {
    std::env::var("WORKBENCH_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT)
}

/// What holds the backend port.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Probe {
    /// Nothing answered, or what answered is not HTTP, or it is not 200.
    Nothing,
    /// A Workbench backend: 200 on `/api/health` with our own payload.
    Workbench,
    /// Some other HTTP service holds the port.
    Foreign,
}

/// One HTTP/1.0 request over a raw socket. Dependency-free is enough here — the
/// question is "is a *Workbench* backend answering", not "give me an HTTP
/// client".
///
/// The body is checked, not just the status line: plenty of local services
/// answer 200 to an unknown path — a static file server, a proxy, another dev
/// tool — and attaching to one silently skips the spawn and points the window
/// at something that 404s every `/api/*` call.
fn probe(addr: SocketAddr) -> Probe {
    let Some(response) = fetch_health(addr) else {
        return Probe::Nothing;
    };
    if status_code(&response) != Some(200) {
        return Probe::Nothing;
    }
    if is_workbench_health(&response) {
        Probe::Workbench
    } else {
        Probe::Foreign
    }
}

fn fetch_health(addr: SocketAddr) -> Option<String> {
    let mut stream = TcpStream::connect_timeout(&addr, PROBE_TIMEOUT).ok()?;
    let _ = stream.set_read_timeout(Some(PROBE_TIMEOUT));
    let _ = stream.set_write_timeout(Some(PROBE_TIMEOUT));
    let request = format!("GET /api/health HTTP/1.0\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;

    let mut response = String::new();
    let mut buffer = [0u8; 512];
    // Bounded read: `Connection: close` means the peer hangs up after the
    // response, and a hostile listener must not be able to stream at us forever
    // (the read timeout above closes the other half of that).
    while response.len() < MAX_HEALTH_RESPONSE {
        match stream.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(n) => response.push_str(&String::from_utf8_lossy(&buffer[..n])),
        }
    }
    Some(response)
}

/// `HTTP/1.1 200 OK` -> `200`.
fn status_code(response: &str) -> Option<u16> {
    let line = response.lines().next()?;
    let mut parts = line.split_whitespace();
    let version = parts.next()?;
    if !version.starts_with("HTTP/") {
        return None;
    }
    parts.next()?.parse().ok()
}

/// `/api/health` answers a `HealthResponse` (`status`/`version`/`workspace`), so
/// that shape is the shell's proof of identity. Matching on text keeps the probe
/// dependency-free; stripping ASCII whitespace makes it tolerant of a
/// pretty-printer without needing a JSON parser.
fn is_workbench_health(response: &str) -> bool {
    let Some((_headers, body)) = response.split_once("\r\n\r\n") else {
        return false;
    };
    let compact: String = body.chars().filter(|c| !c.is_ascii_whitespace()).collect();
    compact.contains("\"status\":\"ok\"") && compact.contains("\"workspace\":")
}

/// The repo root is the directory holding both `pyproject.toml` and `server/`.
/// Searched from the executable first (that is where a built shell lives) and
/// then from the working directory (`tauri dev` runs us inside `src-tauri/`).
fn repo_root() -> Option<PathBuf> {
    let from_exe = std::env::current_exe().ok().and_then(|exe| {
        let start = exe.parent()?.to_path_buf();
        find_repo_root(&start)
    });
    from_exe.or_else(|| find_repo_root(&std::env::current_dir().ok()?))
}

fn find_repo_root(start: &Path) -> Option<PathBuf> {
    start
        .ancestors()
        .find(|dir| dir.join("pyproject.toml").is_file() && dir.join("server").is_dir())
        .map(Path::to_path_buf)
}

/// Forward one of the child's pipes into the shell log, line by line.
///
/// `pub(crate)` because a hosted guest's output belongs in the same log: a
/// pipe nobody drains is a child that blocks once it fills.
pub(crate) fn pump<R: Read + Send + 'static>(stream: R, tag: &'static str) {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines() {
            match line {
                Ok(line) => log(&format!("backend[{tag}] {line}")),
                Err(_) => break,
            }
        }
    });
}

/// Block until the freshly spawned server answers, or the deadline passes.
/// Runs on the supervision thread; the window is already up.
fn wait_until_ready(addr: SocketAddr) {
    let started = Instant::now();
    while started.elapsed() < READY_TIMEOUT {
        if probe(addr) == Probe::Workbench {
            log(&format!(
                "backend ready on {addr} after {:?}",
                started.elapsed()
            ));
            return;
        }
        thread::sleep(READY_POLL_INTERVAL);
    }
    // Releasing the UI beats holding it forever: `/ws/events` reconnects on its
    // own, and the log above says what went wrong.
    log(&format!(
        "backend did not answer on {addr} within {READY_TIMEOUT:?}; releasing the UI anyway"
    ));
}

// ---- Windows job object ------------------------------------------------------

/// A job-object handle we own and never close. `HANDLE` is a raw pointer and so
/// neither `Send` nor `Sync`; this wrapper only ever *stores* it (no call takes
/// `&mut`, none is made after construction), which makes sharing it sound.
#[cfg(windows)]
pub struct JobHandle(
    // Never read back, by design: its only job is to stay open. Closing it —
    // which is what dropping the last handle does — is the kill signal.
    #[allow(dead_code)] windows::Win32::Foundation::HANDLE,
);

#[cfg(windows)]
unsafe impl Send for JobHandle {}
#[cfg(windows)]
unsafe impl Sync for JobHandle {}

/// Create a kill-on-close job and put **this** process in it, before anything is
/// spawned. Job membership is inherited, so every descendant from here on is a
/// member with no window in which it could escape.
///
/// Assigning the *child* after `spawn()` returns loses a race we cannot win:
/// `uv run workbench-server` is a launcher, and the uvicorn process that holds
/// the port is its grandchild. If `uv` forks between `spawn()` and
/// `AssignProcessToJobObject`, that grandchild never joins, survives the shell,
/// and keeps 127.0.0.1:8787 — after which the next launch probes, sees a healthy
/// backend, and cheerfully attaches to a zombie that outlives every future
/// window. The same race defeats the failure path: killing the child we hold
/// terminates `uv` only.
///
/// Joining the job costs the shell nothing: the process is exiting whenever the
/// last handle to the job closes, since that handle is the one we hold. The
/// attach path never gets here, so a developer's own server is untouched.
#[cfg(windows)]
fn confine_self_to_job() -> windows::core::Result<JobHandle> {
    use windows::core::PCWSTR;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows::Win32::System::Threading::GetCurrentProcess;

    // SAFETY: every call below is a plain Win32 call on the job we just created
    // and on the current-process pseudo-handle, which is always valid and needs
    // no closing. `info` outlives the SetInformationJobObject call reading it.
    unsafe {
        let job = CreateJobObjectW(None, PCWSTR::null())?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            std::ptr::addr_of!(info).cast(),
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )?;
        AssignProcessToJobObject(job, GetCurrentProcess())?;
        Ok(JobHandle(job))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Exactly what FastAPI returns for `HealthResponse`.
    const REAL_HEALTH: &str = "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n\
         {\"status\":\"ok\",\"version\":\"0.1.0\",\"workspace\":\"C:\\\\work\"}";

    #[test]
    fn parses_a_status_line() {
        assert_eq!(status_code("HTTP/1.1 200 OK\r\n\r\n"), Some(200));
        assert_eq!(status_code("HTTP/1.0 404 Not Found\r\n"), Some(404));
    }

    #[test]
    fn rejects_a_non_http_listener() {
        assert_eq!(status_code(""), None);
        assert_eq!(status_code("SSH-2.0-OpenSSH_9.0\r\n"), None);
        assert_eq!(status_code("HTTP/1.1 nope\r\n"), None);
    }

    #[test]
    fn recognises_the_backends_own_health_payload() {
        assert!(is_workbench_health(REAL_HEALTH));
        // A pretty-printer must not change the answer.
        assert!(is_workbench_health(
            "HTTP/1.1 200 OK\r\n\r\n{\n  \"status\": \"ok\",\n  \"workspace\": \"/w\"\n}"
        ));
    }

    #[test]
    fn refuses_to_recognise_anything_else_that_answers_200() {
        // A static file server, a proxy, another dev tool: 200, wrong peer.
        assert!(!is_workbench_health(
            "HTTP/1.1 200 OK\r\n\r\n<!doctype html><title>vite</title>"
        ));
        // Right shape, wrong state — a backend reporting itself unhealthy is
        // not something to attach to either.
        assert!(!is_workbench_health(
            "HTTP/1.1 200 OK\r\n\r\n{\"status\":\"degraded\",\"workspace\":\"/w\"}"
        ));
        // Headers only, no body.
        assert!(!is_workbench_health("HTTP/1.1 200 OK\r\n\r\n"));
        assert!(!is_workbench_health("HTTP/1.1 200 OK"));
    }

    #[test]
    fn finds_the_repo_root_from_a_nested_directory() {
        let base = std::env::temp_dir().join(format!("wb-shell-test-{}", std::process::id()));
        let nested = base.join("desktop").join("src-tauri").join("target");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::create_dir_all(base.join("server")).unwrap();
        std::fs::write(base.join("pyproject.toml"), b"").unwrap();

        assert_eq!(find_repo_root(&nested).as_deref(), Some(base.as_path()));
        // `server/` itself carries neither marker, so the walk goes past it.
        assert_eq!(find_repo_root(&base.join("server")), Some(base.clone()));

        // Both markers are required: without pyproject.toml there is no root.
        std::fs::remove_file(base.join("pyproject.toml")).unwrap();
        assert_eq!(find_repo_root(&nested), None);

        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn nothing_is_listening_on_a_closed_port() {
        // Port 1 needs privileges to bind, so on a dev box it is reliably shut.
        assert_eq!(probe(SocketAddr::from(([127, 0, 0, 1], 1))), Probe::Nothing);
    }

    #[test]
    fn a_foreign_listener_is_not_a_backend() {
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let Ok((mut socket, _)) = listener.accept() else {
                return;
            };
            let mut scratch = [0u8; 512];
            let _ = socket.read(&mut scratch);
            // 200, and nothing to do with Workbench — the exact case that used
            // to make the shell attach and log that all was well.
            let _ = socket.write_all(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\n\r\n<h1>some other tool</h1>",
            );
        });

        assert_eq!(probe(addr), Probe::Foreign);
        server.join().unwrap();
    }

    #[test]
    fn a_real_backend_is_recognised() {
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let Ok((mut socket, _)) = listener.accept() else {
                return;
            };
            let mut scratch = [0u8; 512];
            let _ = socket.read(&mut scratch);
            let _ = socket.write_all(REAL_HEALTH.as_bytes());
        });

        assert_eq!(probe(addr), Probe::Workbench);
        server.join().unwrap();
    }
}
