//! Waiting for a condemned guest, off the thread that owns windows.
//!
//! **The second deliberate exception to "everything happens on the main
//! thread"**, and it is the same shape as the first ([`super::mover`]): the
//! Win32 calls that touch hosted windows stay where they must, and the part that
//! *waits* moves to a thread that owns nothing.
//!
//! Closing a hosted panel used to cost the UI thread up to two seconds. The
//! kill is a job-object handle closing, which is instant — but the termination
//! it triggers is asynchronous, so `GuestProcess::reap` polls until the process
//! is really gone before it returns. That poll ran inside `host_close`, which
//! runs on the main thread because the windows are the main thread's, so
//! closing one docked document froze the whole window for as long as the
//! instance took to die: milliseconds for the synthetic guest, and the far end
//! of the bound for a real Office instance in the middle of a save.
//!
//! Inverting it is the whole fix:
//!
//! | step | where |
//! |---|---|
//! | give the guest window back, destroy our two windows | main thread (Win32 requires it) |
//! | condemn the instance (`GuestProcess::kill`) | main thread — it is a `CloseHandle`, not a wait |
//! | wait for it to actually go | **here** |
//! | hand the keyboard back, once it really has | main thread again, through [`super::main_thread::on_main`] |
//!
//! The last row is why this takes a continuation rather than being fire and
//! forget. Focus can only be reclaimed *after* the instance is gone — a released
//! window that is still on the desktop is entitled to keep the keyboard, and
//! reclaiming before it dies is how the caret ends up stranded (the ordering
//! `hosting_tests::closing_a_focused_panel_does_not_strand_the_keyboard` pins).
//! Moving the wait off the main thread therefore has to take that step with it,
//! and hand it back through the seam when the answer is known.
//!
//! **A thread per closed panel, not a worker.** Closing a document is a thing a
//! user does now and then, so there is no queue to amortise and no head-of-line
//! blocking worth inventing: one slow-dying instance must not delay the next
//! panel's keyboard. That is the opposite trade from [`super::mover`], whose
//! work arrives on every animation frame of a drag, and for the opposite reason.
//!
//! **And a thread per panel is a thread that can fail to start.** Thread and
//! handle exhaustion is exactly the pressure a shell juggling several hosted
//! Office instances can reach, and `thread::Builder::spawn` *drops* the closure
//! it could not run — so the obvious shape, moving the instance and the
//! continuation straight into the closure, loses the continuation silently on
//! the one day it matters. The keyboard would never come back to the webview and
//! nothing but a log line would say why. So the work travels to the reaper over
//! a channel, is only handed over once the thread exists, and comes back to the
//! caller to be finished inline when it does not; see [`reap_with`].

use std::io;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use super::guest::GuestProcess;

/// How long the inline fallback waits for an instance when there is no reaper
/// thread to wait for it on.
///
/// A tenth of `guest::REAP_TIMEOUT`, and the difference is the point: this wait
/// runs on the caller — which for `host_close` is the main thread, the very
/// thread this module exists to keep out of. Long enough for an instance that is
/// going anyway (the synthetic guest goes in single-digit milliseconds, and a
/// job object that has already been closed is not usually slow), short enough
/// that a machine too loaded to start a thread costs a brief hitch rather than
/// the two-second freeze this fix removed.
const FALLBACK_WAIT: Duration = Duration::from_millis(200);

/// A condemned instance and the step that may only run once it is gone.
type Condemned = (GuestProcess, Box<dyn FnOnce() + Send + 'static>);

/// See a guest off: kill it here, wait for it there, then run `then`.
///
/// Returns at once. `then` runs on the reaper thread once the instance is
/// really gone (or once the wait has given up on it) — so anything in it that
/// touches a window has to ask the main thread for itself; see the module docs.
///
/// **`then` always runs.** If no reaper thread can be started it runs on this
/// thread instead, after a wait bounded by [`FALLBACK_WAIT`] rather than by the
/// reaper's own timeout — so the caller pays a short hitch, never the loss of
/// whatever the continuation was for.
pub(super) fn reap(process: GuestProcess, then: impl FnOnce() + Send + 'static) {
    reap_with(process, Box::new(then), |body| {
        thread::Builder::new()
            .name("workbench-host-reaper".to_string())
            .spawn(body)
            .map(|_| ())
    });
}

/// The disposal itself, with "how a reaper thread is started" passed in.
///
/// Factored out of [`reap`] for the same reason
/// [`super::main_thread::dispatch_within`] is factored out of `on_main_within`:
/// the claim worth making is about what happens when the thread does *not*
/// start, and a claim like that should be measured rather than reviewed. A test
/// supplies a `spawn` that refuses, and checks the continuation ran anyway and
/// within the bound — see
/// `hosting_tests::a_reaper_thread_that_never_starts_still_hands_the_keyboard_back`.
pub(super) fn reap_with<S>(
    mut process: GuestProcess,
    then: Box<dyn FnOnce() + Send + 'static>,
    spawn: S,
) where
    S: FnOnce(Box<dyn FnOnce() + Send + 'static>) -> io::Result<()>,
{
    // Condemned on the calling thread, deliberately: whatever the reaper thread
    // does next, the instance is already dying by the time `host_close`
    // returns, and a shell that exits a moment later has already issued the
    // kill rather than racing a thread that is about to be torn down with it.
    process.kill();

    // The instance travels over a channel rather than being moved into the
    // closure, so that a thread which never starts hands the work back instead
    // of taking it to the grave: `thread::Builder::spawn` drops what it could
    // not run.
    let (tx, rx) = mpsc::channel::<Condemned>();
    let started = spawn(Box::new(move || {
        // A sender dropped without sending means the caller kept the work and
        // is finishing it itself; there is nothing here to wait for.
        if let Ok((mut process, then)) = rx.recv() {
            process.reap();
            then();
        }
    }));

    let unhanded = match started {
        Ok(()) => tx.send((process, then)).err().map(|err| {
            (
                "the reaper thread was gone before it could be handed the instance".to_string(),
                err.0,
            )
        }),
        Err(err) => Some((
            format!("no thread to wait out a closed guest: {err}"),
            (process, then),
        )),
    };
    let Some((why, (mut process, then))) = unhanded else {
        return;
    };

    // Nothing is going to wait for this instance except this thread. The kill
    // has already been issued so it dies either way; what would be lost is the
    // confirmation and everything the caller hung off it — for `host_close`,
    // handing the keyboard back to the webview. A bounded wait here is worth far
    // more than a silently dropped continuation.
    crate::backend::log(&format!(
        "office host: {why}; waiting up to {FALLBACK_WAIT:?} for it here instead"
    ));
    process.reap_within(FALLBACK_WAIT);
    then();
}
