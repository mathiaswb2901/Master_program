//! Reading the backend's per-launch auth token for the embedded webview.
//!
//! Local-API hardening (ROADMAP item 8) gives the backend a per-launch bearer
//! token that a client must present on `/api/` and `/ws/` once enforcement is
//! flipped on. The browser client fetches it from `GET /api/auth/token`; the
//! desktop shell can do better, because it already knows which port its backend
//! is on and can read the token the server drops on disk — the same path an
//! *attaching* shell (one that did not spawn the backend, so never saw the
//! token in an env var) relies on.
//!
//! The server writes it at `<app-data>/runtime/auth-token-<port>`, owner-only,
//! and unlinks it on exit — see `runtime_token_path` / `_write_runtime_token`
//! in `server/src/workbench_server/main.py`. This module mirrors that path and
//! nothing else: the server is the sole authority on the token, and the shell
//! only ever *reads* it. It never writes, mints, or caches one.
//!
//! Every step degrades to `None` rather than failing: the file is absent until
//! a spawned backend has written it, and stays absent forever when the shell is
//! attached to an externally-started server on a machine where the app-data
//! root cannot be located. A missing token is a normal state, not an error.

use std::fs;
use std::path::{Path, PathBuf};

/// The token file for `port`, under a resolved app-data `root`.
///
/// Mirrors `runtime_token_path` in `server/src/workbench_server/main.py`: keyed
/// by port so two backends on different ports never clobber each other's file.
fn token_path(root: &Path, port: u16) -> PathBuf {
    root.join("runtime").join(format!("auth-token-{port}"))
}

/// Machine-local Workbench app-data root, matching `services/app_data.py` and
/// the `WORKBENCH_APP_DATA_ROOT` override (`settings.app_data_root`).
///
/// Precedence, highest first, exactly as the server resolves it: the env
/// override (the E2E lane sets it), then `%LOCALAPPDATA%\Workbench` on Windows,
/// then `~/.workbench`. `None` when even the home directory cannot be found —
/// in which case there is no token to read and the caller returns `None`.
fn app_data_root() -> Option<PathBuf> {
    if let Some(root) = non_empty_var("WORKBENCH_APP_DATA_ROOT") {
        return Some(PathBuf::from(root));
    }
    #[cfg(windows)]
    if let Some(local) = non_empty_var("LOCALAPPDATA") {
        return Some(PathBuf::from(local).join("Workbench"));
    }
    home_dir().map(|home| home.join(".workbench"))
}

/// A process env var, or `None` when unset or empty. An empty override is
/// treated as absent so it cannot resolve to the filesystem root.
fn non_empty_var(name: &str) -> Option<std::ffi::OsString> {
    std::env::var_os(name).filter(|value| !value.is_empty())
}

/// The user's home directory without pulling in a crate for it: `USERPROFILE`
/// on Windows, `HOME` elsewhere — the same variables `Path.home()` consults.
fn home_dir() -> Option<PathBuf> {
    let name = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    non_empty_var(name).map(PathBuf::from)
}

/// Read and validate the token at `path`, or `None` when it is absent,
/// unreadable, or empty. Trimmed so a trailing newline never changes the value
/// (the server writes none, but a token that fails `compare_digest` over a stray
/// `\n` would be worse than useless).
fn read_token_file(path: &Path) -> Option<String> {
    let token = fs::read_to_string(path).ok()?;
    let trimmed = token.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Compose the path from `root`/`port` and read it. Split out from
/// [`current_token`] so a test can point it at a temp directory without a
/// running server or a mutated process environment.
fn read_token(root: &Path, port: u16) -> Option<String> {
    read_token_file(&token_path(root, port))
}

/// The current per-launch token for the port this shell's backend uses, or
/// `None` when none is on disk. A no-op-safe read: it never panics and never
/// mutates anything.
pub(crate) fn current_token() -> Option<String> {
    let root = app_data_root()?;
    read_token(&root, crate::backend::backend_port())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A temp directory unique to this test, cleaned up at the end. Mirrors the
    /// pattern `backend.rs` tests use rather than adding a `tempfile` dep.
    fn temp_root(tag: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("wb-auth-token-test-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("runtime")).unwrap();
        dir
    }

    #[test]
    fn reads_the_token_at_the_per_port_path() {
        let root = temp_root("present");
        fs::write(token_path(&root, 8787), "s3cr3t-token").unwrap();

        assert_eq!(read_token(&root, 8787).as_deref(), Some("s3cr3t-token"));
        // The port is part of the name: a different port sees no token.
        assert_eq!(read_token(&root, 9999), None);

        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn absent_file_is_none_not_a_panic() {
        let root = temp_root("absent");
        // Nothing written for this port.
        assert_eq!(read_token(&root, 8787), None);
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn empty_or_whitespace_file_is_none() {
        let root = temp_root("empty");
        fs::write(token_path(&root, 8787), "").unwrap();
        assert_eq!(read_token(&root, 8787), None);

        fs::write(token_path(&root, 8787), "  \r\n\t ").unwrap();
        assert_eq!(read_token(&root, 8787), None);

        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn trailing_newline_is_trimmed() {
        let root = temp_root("newline");
        fs::write(token_path(&root, 8787), "tok\n").unwrap();
        assert_eq!(read_token(&root, 8787).as_deref(), Some("tok"));
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn token_path_is_keyed_by_port() {
        let root = Path::new("C:\\root");
        assert!(token_path(root, 8787).ends_with("runtime/auth-token-8787"));
        assert!(token_path(root, 1234).ends_with("runtime/auth-token-1234"));
    }

    #[test]
    fn app_data_root_honours_the_env_override() {
        // The one branch that is the same on every platform and safe to assert
        // without disturbing a shared process env for long.
        let previous = std::env::var_os("WORKBENCH_APP_DATA_ROOT");
        std::env::set_var("WORKBENCH_APP_DATA_ROOT", "C:\\wb-data");
        assert_eq!(app_data_root(), Some(PathBuf::from("C:\\wb-data")));
        match previous {
            Some(value) => std::env::set_var("WORKBENCH_APP_DATA_ROOT", value),
            None => std::env::remove_var("WORKBENCH_APP_DATA_ROOT"),
        }
    }
}
