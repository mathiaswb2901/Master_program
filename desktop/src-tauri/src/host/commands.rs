//! The IPC surface: one command per verb in the Python `HostBackend` Protocol.
//!
//! | `backend.py` | here |
//! |---|---|
//! | `launch(path, kind)` | [`host_open_guest`] today; `WINWORD.EXE` in PR 4 |
//! | `embed(handle, rect)` | [`host_embed`] |
//! | `set_bounds(handle, rect)` | [`host_set_bounds`] |
//! | `detach(handle)` | [`host_detach`] |
//! | `close(handle)` | [`host_close`] |
//! | `poll(handle)` | [`host_poll`] |
//!
//! The alignment is not decoration. That Protocol was written before any native
//! code existed, specifically so the risky half could be dropped in without the
//! domain layer changing shape; a command surface that did not match it would
//! have needed a translation layer whose only job is to apologise for the
//! mismatch. `HostError::code` uses the same words as `HostReason` where they
//! overlap, so a refusal here becomes a terminal state there without a lookup
//! table.
//!
//! Every command is **synchronous**, which Tauri documents as running on the
//! main thread, and every one of them routes its window work through
//! [`super::main_thread::on_main`] anyway — the one that must not is
//! [`host_open_guest`], which waits for a process to start and would freeze the
//! window for as long as that took.

#[cfg(debug_assertions)]
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use super::class::{self, PanelState};
use super::geometry::CssRect;
use super::guest::{self, GuestProcess};
use super::layout::{self, Coalescer};
use super::main_thread::on_main;
use super::{embed, HostError, HostErrorCode, HostGeometry, HostRegistry, HostSnapshot, WindowId};
use super::{layout_for, Panel};

/// A guest is live inside a panel. Must match `HOST_EMBEDDED_EVENT` on the UI
/// side when the panel lands.
pub const EMBEDDED_EVENT: &str = "workbench://office-host/embedded";
/// A guest went away without being asked to.
pub const LOST_EVENT: &str = "workbench://office-host/lost";
/// How long the synthetic guest gets to show a window. Word will need more;
/// the number belongs to the caller, not to the mechanism.
#[cfg(debug_assertions)]
pub(super) const SYNTHETIC_GUEST_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, Serialize)]
pub struct EmbeddedPayload {
    pub host_id: String,
    pub geometry: HostGeometry,
}

/// Why a host stopped being hosted without being asked. The words are
/// `HostReason`'s: `process_exited` is the same state on both sides.
#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LostReason {
    /// The window was destroyed — the application quit or crashed.
    ProcessExited,
}

#[derive(Debug, Clone, Serialize)]
pub struct LostPayload {
    pub host_id: String,
    pub reason: LostReason,
}

/// Is the instance still there? The Protocol's `poll`, and the backstop for
/// `WM_PARENTNOTIFY` — the only crash signal that exists is asking.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Liveness {
    Alive,
    Gone,
}

/// Reparent an existing window into a panel.
///
/// `window_id` is an `HWND` as an integer — the same opaque `window_id` the
/// Python `HostHandle` carries. It is not trusted: [`embed::embed`] refuses a
/// handle that already contains the panel, which is the one mistake that would
/// be unrecoverable.
#[tauri::command]
pub fn host_embed(
    app: AppHandle,
    window: tauri::Window,
    host_id: String,
    window_id: i64,
    rect: CssRect,
    caption_inset: Option<f64>,
) -> Result<HostGeometry, HostError> {
    let scale = scale_factor(&window)?;
    let parent = parent_window(&window)?;
    // CSS pixels, exactly like the rectangle beside it, and stored that way:
    // the physical inset is derived at each layout from the scale in force
    // then, so a window that moves to another monitor stays right. Converting
    // here and keeping the answer is the bug this used to have.
    let inset_css = caption_inset.unwrap_or(0.0).max(0.0);
    let guest = WindowId(window_id as isize);

    let handle = app.clone();
    let id = host_id.clone();
    let geometry = on_main(&app, move || {
        embed_into_panel(&handle, parent, id, guest, rect, scale, inset_css)
    })?;

    emit(
        &app,
        EMBEDDED_EVENT,
        EmbeddedPayload {
            host_id,
            geometry: geometry.clone(),
        },
    );
    Ok(geometry)
}

/// The panel moved or resized. Called on every drag frame, so the cheap path —
/// a layout identical to the last one — costs a lock and a comparison.
///
/// A resize is also how a **DPI change** arrives: dragging the window to a
/// monitor at another scale moves every panel. So the scale factor is read
/// again here and the whole layout re-derived from it — rectangle and caption
/// inset both. Nothing scaled is carried over from the embed.
#[tauri::command]
pub fn host_set_bounds(
    app: AppHandle,
    window: tauri::Window,
    host_id: String,
    rect: CssRect,
) -> Result<HostGeometry, HostError> {
    let scale = scale_factor(&window)?;
    let handle = app.clone();
    on_main(&app, move || {
        let registry = handle.state::<HostRegistry>();
        let mut panels = lock(&registry)?;
        let panel = panels
            .get_mut(&host_id)
            .ok_or_else(|| HostError::unknown_host(&host_id))?;
        let next = panel.layout_at(rect, scale)?;
        if let Some(applied) = panel.coalescer.next(next) {
            layout::apply(
                panel.window.hwnd(),
                panel.clip.hwnd(),
                panel.guest.map(|guest| guest.guest.hwnd()),
                &applied,
            )?;
        }
        Ok(HostGeometry {
            host_id,
            window: panel.window,
            guest: panel.guest.map_or(WindowId(0), |embedded| embedded.guest),
            layout: next,
            scale,
        })
    })
}

/// Show or hide a hosted panel without giving the window back.
///
/// The one thing a hosted window does that a `<div>` does not: it stays on
/// screen when the element describing it is hidden. Switching editor tabs must
/// therefore *say* so, or a real Word paints over the document the user
/// switched to.
///
/// Only our panel window is touched, never the guest. `ShowWindow` on a parent
/// takes its children with it, and hiding the guest itself would mean restoring
/// a visibility bit that belongs to the application — one more piece of its
/// state we would be holding on its behalf.
#[tauri::command]
pub fn host_set_visible(app: AppHandle, host_id: String, visible: bool) -> Result<(), HostError> {
    let handle = app.clone();
    on_main(&app, move || {
        let registry = handle.state::<HostRegistry>();
        let panels = lock(&registry)?;
        let panel = panels
            .get(&host_id)
            .ok_or_else(|| HostError::unknown_host(&host_id))?;
        class::set_visible(panel.window, visible);
        Ok(())
    })
}

/// Give the window back to the desktop, leaving the application running.
///
/// Our two windows go; the process does not. That is the whole difference
/// between `detach` and `close` in the Protocol, and getting it wrong means a
/// user who dragged a document out of the panel loses their unsaved edits.
///
/// The keyboard usually stays with the document, and that is right: the window
/// is still on screen, just outside the panel. [`reclaim_focus`] only acts when
/// the focus was left on nothing — which here means a guest that died between
/// the embed and the detach.
#[tauri::command]
pub fn host_detach(app: AppHandle, host_id: String) -> Result<(), HostError> {
    let handle = app.clone();
    on_main(&app, move || {
        let dead_ends = {
            let registry = handle.state::<HostRegistry>();
            let mut panels = lock(&registry)?;
            let panel = panels
                .get_mut(&host_id)
                .ok_or_else(|| HostError::unknown_host(&host_id))?;
            let ours = [panel.window, panel.clip];
            release_panel(panel);
            ours
        };
        reclaim_focus(&handle, &dead_ends);
        Ok(())
    })
}

/// Close the instance we launched, and be certain it is gone.
#[tauri::command]
pub fn host_close(app: AppHandle, host_id: String) -> Result<(), HostError> {
    let handle = app.clone();
    on_main(&app, move || {
        let registry = handle.state::<HostRegistry>();
        let mut panel = {
            let mut panels = lock(&registry)?;
            panels
                .remove(&host_id)
                .ok_or_else(|| HostError::unknown_host(&host_id))?
        };
        let dead_ends = [panel.window, panel.clip];
        release_panel(&mut panel);
        // Dropping the process closes its job object, which is what actually
        // kills it. Doing that outside the registry lock keeps a two-second
        // worst-case wait off every other panel's path.
        drop(panel.process.take());
        // The keyboard *after* the instance is gone, not before: until then the
        // released window is still on the desktop and may legitimately hold it.
        reclaim_focus(&handle, &dead_ends);
        Ok(())
    })
}

/// Is this host still alive? Cheap enough to poll.
#[tauri::command]
pub fn host_poll(app: AppHandle, host_id: String) -> Result<Liveness, HostError> {
    let registry = app.state::<HostRegistry>();
    let mut panels = lock(&registry)?;
    let panel = panels
        .get_mut(&host_id)
        .ok_or_else(|| HostError::unknown_host(&host_id))?;
    Ok(liveness(panel))
}

/// Put the keyboard in a hosted document.
///
/// Focusing the *panel* is enough: its window procedure hands the focus on to
/// the guest, which is also what makes a click on the panel's own surface do
/// the right thing. A click inside the guest never comes through here at all —
/// the guest focuses itself, measured in [`super::hosting_tests`].
#[tauri::command]
pub fn host_focus(app: AppHandle, host_id: String) -> Result<(), HostError> {
    let handle = app.clone();
    on_main(&app, move || {
        let registry = handle.state::<HostRegistry>();
        let panels = lock(&registry)?;
        let panel = panels
            .get(&host_id)
            .ok_or_else(|| HostError::unknown_host(&host_id))?;
        super::focus::focus(panel.window)
    })
}

/// Every panel this window owns. A leak is visible here and nowhere else.
#[tauri::command]
pub fn host_list(app: AppHandle) -> Result<Vec<HostSnapshot>, HostError> {
    let registry = app.state::<HostRegistry>();
    let panels = lock(&registry)?;
    Ok(panels
        .iter()
        .map(|(host_id, panel)| HostSnapshot {
            host_id: host_id.clone(),
            window: panel.window,
            guest: panel.guest.map(|guest| guest.guest),
            pid: panel.process.as_ref().map(GuestProcess::pid),
            layout: panel.coalescer.last(),
        })
        .collect())
}

/// Launch the synthetic guest and dock it. Debug builds only — this is the
/// fixture the whole mechanism is proven against, and it has no business in a
/// binary a user runs.
///
/// `async` on purpose: it waits for a process to start a window, and doing that
/// on the main thread is a frozen window for as long as it takes.
#[cfg(debug_assertions)]
#[tauri::command(async)]
pub fn host_open_guest(
    app: AppHandle,
    window: tauri::Window,
    host_id: String,
    rect: CssRect,
) -> Result<HostGeometry, HostError> {
    open_synthetic_guest(&app, &window, host_id, rect)
}

/// The body of [`host_open_guest`], reachable from the demo hook as well.
#[cfg(debug_assertions)]
pub(super) fn open_synthetic_guest(
    app: &AppHandle,
    window: &tauri::Window,
    host_id: String,
    rect: CssRect,
) -> Result<HostGeometry, HostError> {
    let scale = scale_factor(window)?;
    let parent = parent_window(window)?;
    let exe = guest::synthetic_guest_exe()?;
    let launched = guest::launch(
        &exe,
        &[],
        guest::SYNTHETIC_GUEST_CLASS,
        SYNTHETIC_GUEST_TIMEOUT,
    )?;
    // The fixture's strip is quoted at 100%, which is what a CSS pixel is.
    let inset_css = f64::from(guest::SYNTHETIC_GUEST_CAPTION);
    let guest_window = launched.window;
    let mut process = Some(launched.process);

    let handle = app.clone();
    let id = host_id.clone();
    let geometry = on_main(app, move || {
        let geometry = embed_into_panel(
            &handle,
            parent,
            id.clone(),
            guest_window,
            rect,
            scale,
            inset_css,
        )?;
        // Bind the process to the panel only once the embed has succeeded; a
        // refused embed drops it here, and dropping it is the kill.
        let registry = handle.state::<HostRegistry>();
        if let Ok(mut panels) = lock(&registry) {
            if let Some(panel) = panels.get_mut(&id) {
                panel.process = process.take();
            }
        }
        Ok(geometry)
    })?;

    emit(
        app,
        EMBEDDED_EVENT,
        EmbeddedPayload {
            host_id,
            geometry: geometry.clone(),
        },
    );
    Ok(geometry)
}

/// Wedge the synthetic guest's window procedure for `millis`. Debug builds
/// only. The one thing the fixture can do that a real application cannot be
/// asked to do on demand, and the reason hang isolation is measurable at all.
#[cfg(debug_assertions)]
#[tauri::command]
pub fn host_hang_guest(app: AppHandle, host_id: String, millis: u32) -> Result<(), HostError> {
    use windows::Win32::Foundation::{LPARAM, WPARAM};
    use windows::Win32::UI::WindowsAndMessaging::PostMessageW;

    let registry = app.state::<HostRegistry>();
    let panels = lock(&registry)?;
    let panel = panels
        .get(&host_id)
        .ok_or_else(|| HostError::unknown_host(&host_id))?;
    let guest = panel
        .guest
        .ok_or_else(|| HostError::window_gone("nothing is embedded in that panel"))?;
    // SAFETY: a plain Win32 post to a window handle the API validates.
    unsafe {
        PostMessageW(
            Some(guest.guest.hwnd()),
            guest::SYNTHETIC_GUEST_HANG,
            WPARAM(millis as usize),
            LPARAM(0),
        )
    }?;
    Ok(())
}

// ---- Shared internals ---------------------------------------------------------

/// Create (or re-create) a panel's windows and put `guest` inside them.
///
/// Runs on the main thread. Re-entrant for a detached host: the entry is kept
/// after a detach precisely so the same id can be docked again.
fn embed_into_panel(
    app: &AppHandle,
    parent: WindowId,
    host_id: String,
    guest: WindowId,
    rect: CssRect,
    scale: f64,
    inset_css: f64,
) -> Result<HostGeometry, HostError> {
    let registry = app.state::<HostRegistry>();
    let mut panels = lock(&registry)?;
    if panels
        .get(&host_id)
        .is_some_and(|panel| panel.guest.is_some())
    {
        return Err(HostError::embed_refused(format!(
            "{host_id} already has a window in it"
        )));
    }

    let plan = layout_for(rect, scale, inset_css)?;
    let window = class::create_panel(
        Some(parent.hwnd()),
        plan.panel,
        PanelState::new(host_id.clone()),
    )?;
    let clip = match class::create_clip(window, plan.clip) {
        Ok(clip) => clip,
        Err(err) => {
            class::destroy(WindowId::from_hwnd(window));
            return Err(err);
        }
    };
    let embedded = match embed::embed(clip, guest.hwnd(), plan.guest) {
        Ok(embedded) => embedded,
        Err(err) => {
            // Leave nothing behind on a refusal: an empty panel window over the
            // webview is a hole in the UI that nothing would ever fill.
            class::destroy(WindowId::from_hwnd(clip));
            class::destroy(WindowId::from_hwnd(window));
            return Err(err);
        }
    };
    class::set_guest(window, Some(embedded.guest));

    let mut coalescer = Coalescer::default();
    // The windows were created at the right place already, but the coalescer
    // has to know that, or the first real resize would be compared against
    // nothing and the second against a layout that never reached Win32.
    coalescer.next(plan);
    let panel = panels.entry(host_id.clone()).or_insert_with(|| Panel {
        window: WindowId(0),
        clip: WindowId(0),
        guest: None,
        process: None,
        coalescer: Coalescer::default(),
        caption_inset_css: inset_css,
    });
    panel.window = WindowId::from_hwnd(window);
    panel.clip = WindowId::from_hwnd(clip);
    panel.guest = Some(embedded);
    panel.caption_inset_css = inset_css;
    panel.coalescer = coalescer;

    Ok(HostGeometry {
        host_id,
        window: panel.window,
        guest: embedded.guest,
        layout: plan,
        scale,
    })
}

/// The window the keyboard falls back to when no hosted document can hold it:
/// the shell's own. `None` on a build or a moment with no main window, which is
/// a reason to leave the focus alone rather than an error.
fn app_window(app: &AppHandle) -> Option<WindowId> {
    let webview = app.get_webview_window("main")?;
    let hwnd = webview.as_ref().window().hwnd().ok()?;
    Some(WindowId::from_hwnd(hwnd))
}

/// Put the keyboard back in the webview if the window that held it has gone.
///
/// `dead_ends` are this host's own windows — alive or not, they host nothing
/// now, so focus sitting on one of them is focus on nothing.
///
/// Main thread only — focus is a property of an input queue, so this has to run
/// on the thread that owns the windows. Cheap and usually a no-op: see
/// [`super::focus::reclaim_if_stranded`] for what "stranded" means and why this
/// cannot steal focus from another application.
fn reclaim_focus(app: &AppHandle, dead_ends: &[WindowId]) {
    let Some(window) = app_window(app) else {
        return;
    };
    if super::focus::reclaim_if_stranded(window, dead_ends) {
        crate::backend::log(
            "office host: a hosted window left with the keyboard; handed it back to the webview",
        );
    }
}

/// Un-embed and destroy our windows, keeping whatever process there is.
///
/// Does not touch the focus: the caller does that once the lock is gone, so the
/// registry is not held across a `SetFocus`.
fn release_panel(panel: &mut Panel) {
    if let Some(embedded) = panel.guest.take() {
        if let Err(err) = embed::release(&embedded) {
            crate::backend::log(&format!("office host: releasing a guest failed: {err}"));
        }
    }
    class::set_guest(panel.window.hwnd(), None);
    class::destroy(panel.clip);
    class::destroy(panel.window);
    panel.clip = WindowId(0);
    panel.window = WindowId(0);
    panel.coalescer.invalidate();
}

/// Which hosted panels have lost their guest since the last sweep.
///
/// Takes and releases the lock itself, and reports each panel once: the caller
/// clears the handle, so a second sweep finds nothing to say.
pub(super) fn collect_lost(registry: &HostRegistry) -> Vec<String> {
    let Ok(mut panels) = lock(registry) else {
        return Vec::new();
    };
    panels
        .iter_mut()
        .filter(|(_, panel)| panel.guest.is_some())
        .filter_map(|(host_id, panel)| (liveness(panel) == Liveness::Gone).then(|| host_id.clone()))
        .collect()
}

fn liveness(panel: &mut Panel) -> Liveness {
    let window_alive = panel
        .guest
        .is_some_and(|embedded| guest::window_exists(embedded.guest));
    let process_alive = panel
        .process
        .as_mut()
        .map_or(window_alive, GuestProcess::is_running);
    if window_alive && process_alive {
        Liveness::Alive
    } else {
        Liveness::Gone
    }
}

pub(super) fn scale_factor(window: &tauri::Window) -> Result<f64, HostError> {
    window.scale_factor().map_err(|err| {
        HostError::new(
            HostErrorCode::Win32,
            format!("the window has no scale factor: {err}"),
        )
    })
}

fn parent_window(window: &tauri::Window) -> Result<WindowId, HostError> {
    let hwnd = window.hwnd().map_err(|err| {
        HostError::new(
            HostErrorCode::Unsupported,
            format!("this window has no HWND to host into: {err}"),
        )
    })?;
    Ok(WindowId::from_hwnd(hwnd))
}

type Panels<'a> = std::sync::MutexGuard<'a, std::collections::HashMap<String, Panel>>;

fn lock<'a>(registry: &'a HostRegistry) -> Result<Panels<'a>, HostError> {
    registry.panels.lock().map_err(|_| {
        HostError::new(
            HostErrorCode::Win32,
            "the host registry is poisoned; a window operation panicked",
        )
    })
}

/// An event the UI may or may not be listening for. A failure to deliver is a
/// log line, never an error on the command that caused it.
fn emit<P: Serialize + Clone>(app: &AppHandle, event: &str, payload: P) {
    if let Err(err) = app.emit(event, payload) {
        crate::backend::log(&format!("office host: {event} was not delivered: {err}"));
    }
}

/// One guest went away by itself. Called from the watchdog's sweep thread.
pub(super) fn on_guest_gone(app: &AppHandle, host_id: String) {
    let registry = app.state::<HostRegistry>();
    let mut dead_ends = Vec::new();
    if let Ok(mut panels) = lock(&registry) {
        if let Some(panel) = panels.get_mut(&host_id) {
            // The window is already destroyed; forget it so nothing tries to
            // restyle or focus a handle that is gone. Our own panel windows
            // stay until the owner closes the host, because the panel is still
            // on screen and something has to be in it.
            panel.guest = None;
            class::set_guest(panel.window.hwnd(), None);
            // Still there, and now hosting nothing: `DestroyWindow` promotes a
            // focused child's focus to its parent, so the keyboard may be
            // sitting on one of these two.
            dead_ends = vec![panel.window, panel.clip];
        }
    }
    // If the user was typing in that document when it died, the keyboard is now
    // somewhere that cannot use it. Reclaiming is input-queue work and this is
    // the watchdog's thread, so it has to be asked of the main one.
    let handle = app.clone();
    if let Err(err) = on_main(app, move || {
        reclaim_focus(&handle, &dead_ends);
        Ok(())
    }) {
        crate::backend::log(&format!(
            "office host: could not hand the keyboard back after a lost guest: {err}"
        ));
    }
    emit(
        app,
        LOST_EVENT,
        LostPayload {
            host_id,
            reason: LostReason::ProcessExited,
        },
    );
}

/// Tear every panel down. Runs on the main thread at exit, where it is the last
/// chance to put windows back and reap what we launched.
pub fn shutdown(app: &AppHandle) {
    let Some(registry) = app.try_state::<HostRegistry>() else {
        return;
    };
    let Ok(mut panels) = lock(&registry) else {
        return;
    };
    for (host_id, mut panel) in panels.drain() {
        release_panel(&mut panel);
        if let Some(mut process) = panel.process.take() {
            process.reap();
        }
        crate::backend::log(&format!("office host: {host_id} torn down at exit"));
    }
}
