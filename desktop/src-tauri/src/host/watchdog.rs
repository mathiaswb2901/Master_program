//! Asking, because nothing tells us.
//!
//! A hosted guest can disappear in ways we never asked for: the user quits Word
//! from its own File menu, or it crashes, or something kills the process. The
//! window simply stops existing inside our panel.
//!
//! The obvious mechanism for hearing about that is `WM_PARENTNOTIFY`, and it
//! **does not fire** for a window reparented in from another process — measured
//! three ways in [`super::hosting_tests`], including with a real mouse click.
//! So this is the whole of the crash signal: a small sweep that asks
//! `IsWindow` and `try_wait`, at an interval a user will not notice.
//!
//! It is the same question the Python `HostBackend::poll` asks, and
//! deliberately not a second authority on the *lifecycle*: this thread reports
//! `lost` and clears the dead handle so nothing tries to restyle or focus it.
//! What that means for the document — crashed, closed, reopenable — stays with
//! the service that owns the state machine.
//!
//! The sweep is cheap by construction: two syscalls per hosted panel, and a
//! machine will have one or two of those. It never touches a window's *state*
//! on another thread — `class::set_guest` writes an atomic — so it needs no hop
//! to the main thread and cannot be blocked by one.

use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Manager};

use super::commands;
use super::HostRegistry;

/// Slow enough to be free, fast enough that a panel does not sit showing a
/// document that closed a moment ago.
const SWEEP_INTERVAL: Duration = Duration::from_millis(500);

/// Start the sweep. Runs for the life of the process.
pub fn spawn(app: AppHandle) {
    thread::spawn(move || loop {
        thread::sleep(SWEEP_INTERVAL);
        sweep(&app);
    });
}

/// One pass. Anything hosted whose window or process has gone is reported once
/// and then forgotten, so the next pass has nothing to say about it.
fn sweep(app: &AppHandle) {
    let Some(registry) = app.try_state::<HostRegistry>() else {
        return;
    };
    let lost = commands::collect_lost(&registry);
    for host_id in lost {
        crate::backend::log(&format!("office host: {host_id} lost its guest"));
        commands::on_guest_gone(app, host_id);
    }
}
