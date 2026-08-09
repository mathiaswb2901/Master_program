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
//!
//! **Commands are not the only caller.** Two of the shell's threads reach window
//! work from outside the IPC surface, and both come through here: the watchdog
//! sweep, when a guest that died under the caret needs the keyboard handed back,
//! and the close-ack watchdog in `lib.rs`, whose whole job is to tear the hosted
//! panels down when the UI never answered a close prompt. That second one used
//! to call the teardown directly from its own thread — `SetParent`,
//! `SetWindowLongPtrW` and `DestroyWindow` on windows it does not own, which
//! Win32 does not merely discourage: `DestroyWindow` cannot destroy a window
//! created by another thread. [`on_main_within`] is what it uses instead, and
//! [`dispatch_within`] is the routing underneath both, factored out so the
//! discipline can be *measured* — a test nominates a thread, drains the queue on
//! it, and checks the work ran there and not on the caller.

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
    on_main_within(app, MAIN_THREAD_TIMEOUT, work)
}

/// [`on_main`] with the bound named by the caller.
///
/// Only one caller needs a bound of its own — the close-ack teardown, which
/// states its reasoning where the number is — and it is a separate entry point
/// rather than a parameter on `on_main` so that every ordinary window call keeps
/// answering to one number.
pub fn on_main_within<T, F>(app: &AppHandle, timeout: Duration, work: F) -> Result<T, HostError>
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

    let handle = app.clone();
    dispatch_within(
        move |posted| {
            handle.run_on_main_thread(posted).map_err(|err| {
                HostError::new(
                    HostErrorCode::MainThread,
                    format!("could not reach the main thread: {err}"),
                )
            })
        },
        timeout,
        work,
    )
}

/// The routing itself, with "how work reaches the main thread" passed in.
///
/// Factored out of [`on_main_within`] for one reason: the rule this module
/// exists to enforce is a claim about *which thread runs the Win32 calls*, and a
/// claim like that should be measured rather than reviewed. A test supplies a
/// `post` that hands the closure to a thread it nominates, and asserts the work
/// ran there — see the tests below and
/// `hosting_tests::a_teardown_asked_for_off_thread_runs_its_win32_where_the_windows_live`.
///
/// A timeout does **not** cancel the work: the closure has already been handed
/// over, and if the main thread comes back it still runs, in the order it was
/// posted. Timing out means only that this caller has stopped waiting for the
/// answer — which is what makes giving up safe.
pub(super) fn dispatch_within<T, F, P>(post: P, timeout: Duration, work: F) -> Result<T, HostError>
where
    F: FnOnce() -> Result<T, HostError> + Send + 'static,
    T: Send + 'static,
    P: FnOnce(Box<dyn FnOnce() + Send + 'static>) -> Result<(), HostError>,
{
    let (tx, rx) = mpsc::channel();
    post(Box::new(move || {
        // A receiver that has already timed out makes this a no-op, which is
        // the right outcome: the caller has given up and reported it.
        let _ = tx.send(work());
    }))?;
    rx.recv_timeout(timeout).map_err(|_| {
        HostError::new(
            HostErrorCode::MainThread,
            format!("the main thread did not run the window work within {timeout:?}"),
        )
    })?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::{Receiver, Sender};
    use std::sync::{Arc, Mutex};
    use std::thread::{self, ThreadId};
    use std::time::Instant;

    type Posted = Box<dyn FnOnce() + Send + 'static>;

    /// A stand-in for the main thread: a queue, and a thread that drains it.
    ///
    /// Deliberately not a Tauri app. The property under test is "the closure
    /// runs on the thread the dispatcher hands it to, never on the caller",
    /// which is about this module and not about Tauri's event loop.
    fn nominated_thread() -> (Sender<Posted>, ThreadId, thread::JoinHandle<()>) {
        let (tx, rx): (Sender<Posted>, Receiver<Posted>) = mpsc::channel();
        let (id_tx, id_rx) = mpsc::channel();
        let joiner = thread::spawn(move || {
            let _ = id_tx.send(thread::current().id());
            while let Ok(work) = rx.recv() {
                work();
            }
        });
        let id = id_rx.recv().expect("the nominated thread reports its id");
        (tx, id, joiner)
    }

    #[test]
    fn without_a_recorded_main_thread_nothing_claims_to_be_on_it() {
        // `remember_main_thread` is called from Tauri's setup; a unit test has
        // no app, so the answer must be a plain "no" rather than a panic.
        assert!(!on_main_thread() || MAIN_THREAD.get().is_some());
    }

    #[test]
    fn work_runs_on_the_thread_the_dispatcher_hands_it_to() {
        let (tx, nominated, joiner) = nominated_thread();
        let caller = thread::current().id();
        assert_ne!(
            caller, nominated,
            "the test needs two threads to prove this"
        );

        let ran_on = dispatch_within(
            |posted| {
                tx.send(posted).map_err(|_| {
                    HostError::new(HostErrorCode::MainThread, "the queue is not being drained")
                })
            },
            Duration::from_secs(5),
            || Ok(thread::current().id()),
        )
        .expect("the nominated thread ran the work");

        assert_eq!(
            ran_on, nominated,
            "the work ran on {ran_on:?}, not on the thread it was dispatched to"
        );
        drop(joiner);
    }

    #[test]
    fn a_main_thread_that_never_answers_gives_up_within_the_bound() {
        // The wedged case the close-ack teardown has to survive: the work is
        // accepted and then never run. The caller must come back with a
        // `MainThread` refusal on its own clock, and — this is the half that
        // matters — it must not have run the work itself as a consolation.
        let parked: Mutex<Vec<Posted>> = Mutex::new(Vec::new());
        let ran = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&ran);

        let started = Instant::now();
        let outcome: Result<(), HostError> = dispatch_within(
            |posted| {
                parked.lock().expect("not poisoned").push(posted);
                Ok(())
            },
            Duration::from_millis(300),
            move || {
                flag.store(true, Ordering::SeqCst);
                Ok(())
            },
        );
        let waited = started.elapsed();

        let err = outcome.expect_err("a main thread that never answers is a refusal");
        assert_eq!(err.code, HostErrorCode::MainThread);
        assert!(
            waited >= Duration::from_millis(250) && waited < Duration::from_secs(2),
            "gave up after {waited:?}, which is not the bound it was given"
        );
        assert!(
            !ran.load(Ordering::SeqCst),
            "the work ran on the caller's thread after the dispatch timed out"
        );
        // And giving up is not cancelling: the closure is still there, and
        // running it now still does the work.
        let parked = parked.into_inner().expect("not poisoned");
        for work in parked {
            work();
        }
        assert!(
            ran.load(Ordering::SeqCst),
            "the abandoned work was lost rather than left queued"
        );
    }
}
