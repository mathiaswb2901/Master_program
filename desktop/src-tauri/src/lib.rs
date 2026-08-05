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
//!   dirty-close prompt (`confirm_close`) — or dropped (`cancel_close`).
//! * **Attention badge.** `document.title` does not reach a native title bar or
//!   the taskbar, so `set_attention` retitles the window instead.
//!
//! The guard is armed by the UI (`shell_ready`) rather than assumed: if the
//! webview never runs our code — a broken dev server, a failed bundle — the
//! window closes normally instead of trapping the user in an unclosable app.

mod backend;

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{Emitter, Manager, WindowEvent};

const TITLE: &str = "Workbench";
/// Same leading dot the browser build puts in `document.title` (DESIGN.md §6.4).
const TITLE_ATTENTION: &str = "\u{25CF} Workbench";
/// Must match `CLOSE_REQUESTED_EVENT` in `ui/src/shell.ts`.
const CLOSE_REQUESTED_EVENT: &str = "workbench://close-requested";

/// Set once the UI has registered its close listener. Until then the guard
/// stays off, so a webview that never booted cannot make the window unclosable.
static GUARD_ARMED: AtomicBool = AtomicBool::new(false);
/// Set by `confirm_close` so the close it triggers passes straight through
/// instead of bouncing back to the UI forever.
static CLOSE_CONFIRMED: AtomicBool = AtomicBool::new(false);

/// The UI is live and listening; from here on the close guard applies.
#[tauri::command]
fn shell_ready() {
    GUARD_ARMED.store(true, Ordering::SeqCst);
}

/// Needs-attention badge, in the one place a native app can show it.
#[tauri::command]
fn set_attention(window: tauri::Window, on: bool) -> tauri::Result<()> {
    window.set_title(if on { TITLE_ATTENTION } else { TITLE })
}

/// The user answered the dirty-close prompt with "close" — close for real.
#[tauri::command]
fn confirm_close(window: tauri::Window) -> tauri::Result<()> {
    CLOSE_CONFIRMED.store(true, Ordering::SeqCst);
    window.close()
}

/// The user answered "cancel". Clearing the flag matters: a confirm that raced
/// a cancel must not leave the *next* close unguarded.
#[tauri::command]
fn cancel_close() {
    CLOSE_CONFIRMED.store(false, Ordering::SeqCst);
}

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let backend = backend::start();
            backend::log(&format!("backend {:?}", backend.mode));
            // Held in managed state: dropping it would end the child process.
            app.manage(backend);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            shell_ready,
            set_attention,
            confirm_close,
            cancel_close
        ])
        .on_window_event(|window, event| {
            let WindowEvent::CloseRequested { api, .. } = event else {
                return;
            };
            // `swap` and not `load`: the confirmation is spent on this close, so
            // a later one is guarded again.
            if CLOSE_CONFIRMED.swap(false, Ordering::SeqCst) || !GUARD_ARMED.load(Ordering::SeqCst)
            {
                return;
            }
            api.prevent_close();
            if window.emit(CLOSE_REQUESTED_EVENT, ()).is_err() {
                // The UI cannot be asked, so holding the window hostage would be
                // worse than the risk we are guarding against.
                backend::log("close-requested event could not be delivered; closing anyway");
                CLOSE_CONFIRMED.store(true, Ordering::SeqCst);
                let _ = window.close();
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to start the Workbench shell");
}
