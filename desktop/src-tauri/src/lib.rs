//! The Workbench desktop shell.
//!
//! A native window is not cosmetics here: real Word/Excel/PowerPoint windows
//! get reparented into panels in a later PR, and a browser tab has no HWND to
//! parent them to. This PR lands the window and the two behaviours that a
//! browser gave us for free and WebView2 does not:
//!
//! * **Close guard.** WebView2 ignores `beforeunload`, so closing the native
//!   window used to discard unsaved buffers in silence. `CloseRequested` is now
//!   held, handed to the UI, and only completed once the user has answered the
//!   dirty-close prompt (`confirm_close`) — or dropped (`cancel_close`). The
//!   state machine and its escape hatches live in [`close_guard`].
//! * **Attention badge.** `document.title` does not reach a native title bar or
//!   the taskbar, so `set_attention` retitles the window instead.
//!
//! Backend supervision runs on a worker thread and the window opens
//! immediately: it can take seconds for a spawned server to bind, and a
//! double-click that shows nothing at all for that long reads as a failed
//! launch. The UI waits for `workbench://backend-ready` before it opens any
//! socket, which is the half of the problem that actually mattered.

mod backend;
mod close_guard;

use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use tauri::webview::PageLoadEvent;
use tauri::{Emitter, Manager, WindowEvent};

use close_guard::{CloseGuard, Decision};

const TITLE: &str = "Workbench";
/// Same leading dot the browser build puts in `document.title` (DESIGN.md §6.4).
const TITLE_ATTENTION: &str = "\u{25CF} Workbench";
/// Must match `CLOSE_REQUESTED_EVENT` in `ui/src/shell.ts`.
const CLOSE_REQUESTED_EVENT: &str = "workbench://close-requested";
/// Must match `BACKEND_READY_EVENT` in `ui/src/shell.ts`.
const BACKEND_READY_EVENT: &str = "workbench://backend-ready";

/// How long the UI gets to acknowledge a close prompt before the shell stops
/// holding the window.
///
/// Only the *acknowledgement* is on this clock. Once the UI answers, the user
/// has as long as they like with the modal. This exists because `emit` cannot
/// report an undelivered event (see `CloseGuard::expired`): without it, a page
/// with no listener — a dev server killed mid-session, a bundle that failed to
/// load — leaves a window that cannot be closed by the X, by Alt+F4 or from the
/// taskbar, and killing it from Task Manager then reaps the supervised backend
/// too. An unclosable window is a worse failure than the buffer the guard is
/// protecting.
const CLOSE_ACK_TIMEOUT: Duration = Duration::from_secs(3);

static GUARD: CloseGuard = CloseGuard::new();
/// Set once supervision has settled, for a UI that asks after the event fired.
static BACKEND_READY: AtomicBool = AtomicBool::new(false);

/// The UI is live and listening; from here on the close guard applies.
#[tauri::command]
fn shell_ready() {
    GUARD.arm();
}

/// The UI received a close prompt — the watchdog for it can stand down.
#[tauri::command]
fn close_ack() {
    GUARD.ack();
}

/// Has supervision settled? Asked once at startup, to close the race against
/// `BACKEND_READY_EVENT` firing before the UI subscribed.
#[tauri::command]
fn backend_ready() -> bool {
    BACKEND_READY.load(Ordering::SeqCst)
}

/// Needs-attention badge, in the one place a native app can show it.
#[tauri::command]
fn set_attention(window: tauri::Window, on: bool) -> tauri::Result<()> {
    window.set_title(if on { TITLE_ATTENTION } else { TITLE })
}

/// The user answered the dirty-close prompt with "close" — close for real.
#[tauri::command]
fn confirm_close(window: tauri::Window) -> tauri::Result<()> {
    GUARD.confirm();
    window.close()
}

/// The user answered "cancel".
#[tauri::command]
fn cancel_close() {
    GUARD.cancel();
}

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // Before anything worth logging happens: a release build is a GUI
            // subsystem app with no stderr, so the file is the only copy.
            backend::open_log(app.path().app_log_dir().ok());
            let handle = app.handle().clone();
            // Off the main thread on purpose. Probing, spawning and waiting for
            // health is up to `READY_TIMEOUT` of work, and Tauri creates the
            // configured window only after `setup` returns — doing it here
            // means no window, no splash and no taskbar entry while it runs.
            thread::spawn(move || {
                let backend = backend::start();
                backend::log(&format!("backend {:?}", backend.mode));
                // Held in managed state: dropping it would end the child.
                handle.manage(backend);
                BACKEND_READY.store(true, Ordering::SeqCst);
                if let Err(err) = handle.emit(BACKEND_READY_EVENT, ()) {
                    backend::log(&format!("backend-ready event failed: {err}"));
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            shell_ready,
            close_ack,
            backend_ready,
            set_attention,
            confirm_close,
            cancel_close
        ])
        .on_page_load(|_webview, payload| {
            if payload.event() == PageLoadEvent::Started {
                // The document that armed the guard is gone. The UI re-arms
                // through `shell_ready` once it is running again; if it never
                // does (a reload onto a dead dev server), the window stays
                // closable, which is the point.
                GUARD.disarm();
            }
        })
        .on_window_event(|window, event| {
            let WindowEvent::CloseRequested { api, .. } = event else {
                return;
            };
            let Decision::AskUi { ticket } = GUARD.on_close_requested() else {
                return;
            };
            api.prevent_close();
            if let Err(err) = window.emit(CLOSE_REQUESTED_EVENT, ()) {
                backend::log(&format!("close-requested event failed: {err}"));
            }
            // `emit` reports nothing about delivery — it evaluates a script in
            // the webview, which succeeds even on a page with no listener. The
            // ack is the only evidence anyone is home, so hold the window only
            // as long as it is plausibly on its way.
            let window = window.clone();
            thread::spawn(move || {
                thread::sleep(CLOSE_ACK_TIMEOUT);
                if GUARD.expired(ticket) {
                    backend::log(&format!(
                        "the UI did not acknowledge the close prompt within \
                         {CLOSE_ACK_TIMEOUT:?}; closing anyway"
                    ));
                    GUARD.confirm();
                    if let Err(err) = window.close() {
                        backend::log(&format!("forced close failed: {err}"));
                    }
                }
            });
        })
        .run(tauri::generate_context!())
        .expect("failed to start the Workbench shell");
}
