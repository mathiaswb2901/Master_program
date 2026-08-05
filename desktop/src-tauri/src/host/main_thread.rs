//! One rule, enforced rather than remembered: **Win32 window work runs on the
//! thread that owns the Tauri window**, which is the main thread.
//!
//! It matters more here than in most Win32 code. `CreateWindowExW` binds a
//! window to the calling thread forever — its WndProc runs there, `DestroyWindow`
//! must be called from there, and its input queue is that thread's. Creating our
//! panel window on a Tauri worker thread would give it a WndProc on a thread
//! with no message pump (so `WM_PARENTNOTIFY` would never be dispatched) and
//! would attach a *third* input queue to a design whose central risk is already
//! input-queue attachment.
//!
//! Tauri documents synchronous `#[tauri::command]` functions as running on the
//! main thread, and asynchronous ones as running on the async runtime. This
//! module does not take that on trust: [`on_main`] compares thread ids and logs,
//! once, which path the first hop actually took. If the documented behaviour
//! ever changes, the shell log says so instead of the window quietly ending up
//! on the wrong thread.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::OnceLock;
use std::thread::ThreadId;
use std::time::Duration;

use tauri::AppHandle;

use super::{HostError, HostErrorCode};

/// How long a caller off the main thread waits for it. Long enough for a busy
/// event loop, short enough that a wedged main thread surfaces as an error
/// rather than as a hung request.
const MAIN_THREAD_TIMEOUT: Duration = Duration::from_secs(2);

static MAIN_THREAD: OnceLock<ThreadId> = OnceLock::new();
static MEASURED: AtomicBool = AtomicBool::new(false);

/// Record the main thread. Called from Tauri's `setup`, which runs on it.
pub fn remember_main_thread() {
    let _ = MAIN_THREAD.set(std::thread::current().id());
}

fn on_main_thread() -> bool {
    MAIN_THREAD
        .get()
        .is_some_and(|id| *id == std::thread::current().id())
}

/// Run `work` on the main thread and bring back what it returned.
///
/// Called from the main thread it runs inline — posting and then blocking on
/// the reply would deadlock, since the thread that must run the closure is the
/// one waiting for it.
pub fn on_main<T, F>(app: &AppHandle, work: F) -> Result<T, HostError>
where
    F: FnOnce() -> Result<T, HostError> + Send + 'static,
    T: Send + 'static,
{
    let direct = on_main_thread();
    if !MEASURED.swap(true, Ordering::SeqCst) {
        // The measurement, not the assumption: says out loud whether Tauri
        // really did put this command on the main thread.
        crate::backend::log(&format!(
            "office host: first window call arrived {}on the main thread",
            if direct { "" } else { "*not* " }
        ));
    }
    if direct {
        return work();
    }

    let (tx, rx) = mpsc::channel();
    app.run_on_main_thread(move || {
        // A receiver that has already timed out makes this a no-op, which is
        // the right outcome: the caller has given up and reported it.
        let _ = tx.send(work());
    })
    .map_err(|err| {
        HostError::new(
            HostErrorCode::MainThread,
            format!("could not reach the main thread: {err}"),
        )
    })?;
    rx.recv_timeout(MAIN_THREAD_TIMEOUT).map_err(|_| {
        HostError::new(
            HostErrorCode::MainThread,
            format!("the main thread did not run the window work within {MAIN_THREAD_TIMEOUT:?}"),
        )
    })?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn without_a_recorded_main_thread_nothing_claims_to_be_on_it() {
        // `remember_main_thread` is called from Tauri's setup; a unit test has
        // no app, so the answer must be a plain "no" rather than a panic.
        assert!(!on_main_thread() || MAIN_THREAD.get().is_some());
    }
}
