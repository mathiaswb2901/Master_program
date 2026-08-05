//! Supervision of the FastAPI backend the UI talks to.
//!
//! Two modes, decided by one probe of `GET /api/health`:
//!
//! * **Attach** — something already answers on the backend port. That is the
//!   developer's own `uv run workbench-server`, and it keeps owning the
//!   workspace (the server's CWD *is* the workspace), which is the contract the
//!   app has today. We touch nothing.
//! * **Spawn** — nothing answers, so the shell starts one from the repo root
//!   with no console window and pipes its output into our own stderr.
//!
//! A spawned child is wrapped in a Windows **Job Object** with
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. The job handle is then held for the
//! lifetime of the process and deliberately never closed: when this process
//! dies — cleanly, by crash, or by Task Manager — the OS closes its handles,
//! the job closes, and the child dies with it. That is the point. A graceful
//! "kill the child on quit" path was measured on the target machine to leave
//! orphaned servers behind; the job object needs no code of ours to run at exit.
//!
//! Deliberately not `bundle.externalBin` / `tauri-plugin-shell` sidecars: both
//! have documented orphan bugs (tauri#11686, tauri#2464) and neither gives the
//! kill-on-close guarantee above.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Matches `Settings.host`/`Settings.port` in `server/src/workbench_server/config.py`.
const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8787;

/// Long enough to cross a loopback connect, short enough that a cold start is
/// not perceived as a hang: this runs before the window is usable.
const PROBE_TIMEOUT: Duration = Duration::from_millis(500);
/// How long a spawned server gets to answer before the window opens anyway.
///
/// This wait is load-bearing, not cosmetic. Tauri creates the config's window
/// after `setup` returns, so blocking here is what keeps the webview from
/// loading against a port nobody is listening on yet. Measured without it: the
/// UI came up with `ECONNREFUSED` on its sockets, and while `/ws/events`
/// reconnects, the terminal does not — the first PTY tab rendered "Terminal
/// exited" until the user clicked Reconnect.
const READY_TIMEOUT: Duration = Duration::from_secs(30);
const READY_POLL_INTERVAL: Duration = Duration::from_millis(150);

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

pub fn log(message: &str) {
    eprintln!("[workbench-shell] {message}");
}

/// Probe first, spawn only if nobody answers.
pub fn start() -> Backend {
    let addr = backend_addr();

    if health_ok(addr) {
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

    let Some(root) = repo_root() else {
        log(
            "no repo root found (looked for pyproject.toml + server/ above the \
             executable and the working directory) — start the backend yourself \
             with `uv run workbench-server`",
        );
        return unavailable();
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

    #[cfg(windows)]
    let job = match confine_to_job(&child) {
        Ok(job) => Some(job),
        Err(err) => {
            // Without the job object an orphan becomes possible, so refuse the
            // half-safe version outright rather than leaving one behind later.
            log(&format!(
                "job object setup failed ({err}); killing the child rather than \
                          risking an orphan"
            ));
            let _ = child.kill();
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

/// `WORKBENCH_HOST`/`WORKBENCH_PORT` are the same env vars the server itself
/// reads (pydantic-settings, prefix `WORKBENCH_`), so overriding one moves both.
fn backend_addr() -> SocketAddr {
    let port = std::env::var("WORKBENCH_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT);
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

/// One HTTP/1.0 request over a raw socket. A dependency-free probe is enough
/// here — we need "is a Workbench backend answering", not an HTTP client.
fn health_ok(addr: SocketAddr) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, PROBE_TIMEOUT) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(PROBE_TIMEOUT));
    let _ = stream.set_write_timeout(Some(PROBE_TIMEOUT));
    let request = format!("GET /api/health HTTP/1.0\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    // Bounded read: the status line is all we need, and a hostile listener must
    // not be able to stream at us forever.
    let mut buffer = [0u8; 512];
    while response.len() < 4096 {
        match stream.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(n) => response.push_str(&String::from_utf8_lossy(&buffer[..n])),
        }
        if response.contains("\r\n") {
            break;
        }
    }
    status_code(&response) == Some(200)
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

/// Forward one of the child's pipes into our stderr, line by line.
fn pump<R: Read + Send + 'static>(stream: R, tag: &'static str) {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines() {
            match line {
                Ok(line) => log(&format!("backend[{tag}] {line}")),
                Err(_) => break,
            }
        }
    });
}

/// Block until the freshly spawned server answers, or the deadline passes. See
/// `READY_TIMEOUT` for why the window is worth delaying over.
fn wait_until_ready(addr: SocketAddr) {
    let started = Instant::now();
    while started.elapsed() < READY_TIMEOUT {
        if health_ok(addr) {
            log(&format!(
                "backend ready on {addr} after {:?}",
                started.elapsed()
            ));
            return;
        }
        thread::sleep(READY_POLL_INTERVAL);
    }
    // Opening a broken window beats never opening one: the UI reconnects its
    // event socket, and the log above says what went wrong.
    log(&format!(
        "backend did not answer on {addr} within {READY_TIMEOUT:?}; opening the window anyway"
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

/// Put `child` in a fresh job marked kill-on-close, and hand the job back so the
/// caller can keep it alive.
#[cfg(windows)]
fn confine_to_job(child: &Child) -> windows::core::Result<JobHandle> {
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    // SAFETY: every call below is a plain Win32 call on handles we just made
    // (the job) or that `Child` owns for as long as this function runs (the
    // process). `info` outlives the SetInformationJobObject call it is read by.
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
        AssignProcessToJobObject(job, HANDLE(child.as_raw_handle()))?;
        Ok(JobHandle(job))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        assert!(!health_ok(SocketAddr::from(([127, 0, 0, 1], 1))));
    }
}
