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

use std::thread;

use super::guest::GuestProcess;

/// See a guest off: kill it here, wait for it there, then run `then`.
///
/// Returns at once. `then` runs on the reaper thread once the instance is
/// really gone (or once the wait has given up on it) — so anything in it that
/// touches a window has to ask the main thread for itself; see the module docs.
pub(super) fn reap(mut process: GuestProcess, then: impl FnOnce() + Send + 'static) {
    // Condemned on the calling thread, deliberately: whatever the reaper thread
    // does next, the instance is already dying by the time `host_close`
    // returns, and a shell that exits a moment later has already issued the
    // kill rather than racing a thread that is about to be torn down with it.
    process.kill();
    let spawned = thread::Builder::new()
        .name("workbench-host-reaper".to_string())
        .spawn(move || {
            let mut process = process;
            process.reap();
            then();
        });
    if spawned.is_err() {
        // The kill has already been issued, so the instance still dies; what is
        // lost is the confirmation and whatever `then` would have done with it.
        crate::backend::log(
            "office host: no thread to wait out a closed guest; it was killed but not waited for",
        );
    }
}
