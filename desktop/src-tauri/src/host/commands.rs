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

use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use super::class::{self, PanelState};
use super::escape;
use super::geometry::CssRect;
use super::guest::{self, GuestProcess};
use super::layout::{self, Coalescer};
use super::main_thread::{on_main, on_main_within};
use super::reaper;
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
        // That window is on the desktop now, where Alt+Tab is the way out of it
        // — the escape is for a window the user cannot leave, and this is no
        // longer one. It stays armed if another document is still docked.
        release_escape_if_idle(&handle.state::<HostRegistry>());
        Ok(())
    })
}

/// Close the instance we launched, and be certain it is gone.
///
/// **Certain, but not on this thread.** The window work below has to run here —
/// the panel and its clip child belong to the main thread — while being certain
/// an instance is gone means *waiting* for a process to die, which used to
/// happen here too and froze the window for up to two seconds per closed panel.
/// The kill is issued here (a `CloseHandle`, instant) and the wait is
/// [`super::reaper`]'s; the keyboard follows the wait, because that is the step
/// whose correct answer depends on the instance really being gone.
#[tauri::command]
pub fn host_close(app: AppHandle, host_id: String) -> Result<(), HostError> {
    let handle = app.clone();
    on_main(&app, move || {
        let registry = handle.state::<HostRegistry>();
        close_panel(&registry, &host_id, |process, dead_ends| {
            let Some(process) = process else {
                // Nothing of ours to wait for: the window we were handed is
                // already back on the desktop, so the keyboard question is
                // answerable now.
                reclaim_focus(&handle, &dead_ends);
                return;
            };
            let after = handle.clone();
            reaper::reap(process, move || {
                let inner = after.clone();
                if let Err(err) = on_main(&after, move || {
                    reclaim_focus(&inner, &dead_ends);
                    Ok(())
                }) {
                    crate::backend::log(&format!(
                        "office host: could not hand the keyboard back after closing a panel: {err}"
                    ));
                }
            });
        })
    })
}

/// The body of [`host_close`]: take the panel out of the registry, give the
/// guest window back, destroy ours, and hand whatever we launched to `finish`.
///
/// **Main thread only** — every call it makes is a window call.
///
/// `finish` is passed in rather than called directly so that the timing claim
/// this shape exists for ("closing a panel never waits for a process to die")
/// is measurable in [`super::hosting_tests`], which has real windows and a real
/// guest but no Tauri app to hang a keyboard reclaim off.
pub(super) fn close_panel(
    registry: &HostRegistry,
    host_id: &str,
    finish: impl FnOnce(Option<GuestProcess>, [WindowId; 2]),
) -> Result<(), HostError> {
    let mut panel = {
        let mut panels = lock(registry)?;
        panels
            .remove(host_id)
            .ok_or_else(|| HostError::unknown_host(host_id))?
    };
    let dead_ends = [panel.window, panel.clip];
    release_panel(&mut panel);
    // Outside the registry lock, as it always was: nothing another panel's
    // command needs is held while an instance is being seen off. The escape goes
    // with the last docked document — it is here rather than in `host_close` so
    // that every caller of this body, tests included, releases the chord.
    release_escape_if_idle(registry);
    finish(panel.process.take(), dead_ends);
    Ok(())
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

/// What the panel is allowed to *promise* about the way out of a docked
/// document.
///
/// The chord [`super::escape`] registers is taken from the **whole machine**, so
/// `RegisterHotKey` can be refused: another application already owns it, which
/// inside an RDP session `mstsc` really does (it binds `Ctrl+Alt+Home` to the
/// connection bar). [`arm_escape`] treats that as a degrade rather than a failed
/// embed — the document still opens and the *Return to Workbench* button still
/// works — but the panel used to print "`Ctrl+Alt+Home` brings it back" either
/// way, and the only trace of the refusal was a backend log line the user has no
/// way to read. This is that answer, in the shape the panel asks for it.
#[derive(Debug, Clone, Copy, Serialize)]
pub struct EscapeState {
    /// Is the chord ours right now? `false` means the hint must not name it —
    /// there is no hotkey registered, and the button is the only way back.
    pub armed: bool,
    /// The chord as a person reads it, quoted from the registration rather than
    /// spelled a second time: [`super::escape::CHORD`] is where it is written.
    pub chord: &'static str,
}

/// Is there a keyboard way out of a docked document *right now*?
///
/// A read of one atomic, so unlike its neighbours it needs neither the registry
/// nor a hop to the main thread: the question is about a machine-wide
/// registration, not about a panel. The UI asks it whenever a document docks or
/// undocks, and that re-ask matters — arming is idempotent and retried by every
/// embed, so a chord that was taken when the first document docked can become
/// ours by the time the second one does.
#[tauri::command]
pub fn host_escape_state() -> EscapeState {
    EscapeState {
        armed: escape::is_armed(),
        chord: escape::CHORD,
    }
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
    let exe = guest::synthetic_guest_exe()?;
    open_guest(
        app,
        window,
        host_id,
        rect,
        &exe,
        &[],
        guest::SYNTHETIC_GUEST_CLASS,
        // The fixture's strip is quoted at 100%, which is what a CSS pixel is.
        f64::from(guest::SYNTHETIC_GUEST_CAPTION),
        SYNTHETIC_GUEST_TIMEOUT,
    )
}

/// Launch **any** application, find its frame by class, and dock it — the whole
/// of what a hosted document is, with the choice of application left to the
/// caller.
///
/// The synthetic guest and a real `WINWORD.EXE` differ here by four arguments
/// and nothing else, which is the claim `guest.rs` makes and this is where it is
/// cashed in: the demo hook can dock a real Word through the same code the
/// Python service drives, so "the moat works" can be verified against Office
/// rather than only against the fixture.
///
/// Debug builds only: it launches processes named by whoever calls it.
#[cfg(debug_assertions)]
#[allow(clippy::too_many_arguments)]
pub(super) fn open_guest(
    app: &AppHandle,
    window: &tauri::Window,
    host_id: String,
    rect: CssRect,
    exe: &std::path::Path,
    args: &[&str],
    class: &str,
    inset_css: f64,
    timeout: Duration,
) -> Result<HostGeometry, HostError> {
    let scale = scale_factor(window)?;
    let parent = parent_window(window)?;
    let launched = guest::launch(exe, args, class, timeout)?;
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
    // From this instant the panel contains a window that takes every keystroke
    // aimed at it and hands the webview none of them, so this is the instant the
    // keyboard escape has to exist. Both callers reach it here rather than
    // separately, and a re-embed after a detach re-arms.
    arm_escape(app);

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
pub(super) fn reclaim_focus(app: &AppHandle, dead_ends: &[WindowId]) {
    let Some(window) = app_window(app) else {
        return;
    };
    if super::focus::reclaim_if_stranded(window, dead_ends) {
        crate::backend::log(
            "office host: a hosted window left with the keyboard; handed it back to the webview",
        );
    }
}

/// Arm the keyboard escape, because a window that swallows keystrokes has just
/// gone into a panel.
///
/// Called from [`embed_into_panel`] with the registry lock held, which is safe
/// precisely because [`super::escape`] owns no registry: the answer to "is
/// something docked" is not in question here — one just was.
///
/// A refusal is a log line and not a failed embed. The chord being unavailable
/// (another application already registered it) makes the keyboard escape worse,
/// not the document unopenable, and the visible affordance under the panel is
/// still there. What a refusal must *not* do is stay invisible: the panel reads
/// [`host_escape_state`] on every dock and stops naming a chord that is not
/// ours, so the hint degrades to the button instead of promising a keystroke
/// nothing would answer.
fn arm_escape(app: &AppHandle) {
    let Some(fallback) = app_window(app) else {
        // No main window is a build or a moment with nowhere to hand the
        // keyboard back to — the same reason `reclaim_focus` leaves it alone.
        return;
    };
    if let Err(err) = escape::arm(fallback) {
        crate::backend::log(&format!(
            "office host: a document is docked without a keyboard escape: {err}"
        ));
    }
}

/// Give the chord back once nothing is docked any more.
///
/// The chord [`super::escape`] registers is taken from the **whole machine**
/// while it is armed, so it is armed exactly while a window that can swallow the
/// keyboard is inside a panel. Every path that *removes* a guest — detach, close,
/// a guest that went away by itself — ends here, and the last one out releases
/// it.
///
/// **Main thread only, and never with the registry lock held** — it takes the
/// lock itself to answer the one question it has.
pub(super) fn release_escape_if_idle(registry: &HostRegistry) {
    let docked = match lock(registry) {
        Ok(panels) => panels.values().any(|panel| panel.guest.is_some()),
        // A poisoned registry means a window operation panicked. Leaving the
        // chord exactly as it is beats guessing in either direction.
        Err(_) => return,
    };
    if !docked {
        escape::disarm();
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
        // One fewer document that can swallow the keyboard — and if it was the
        // last, the chord goes back to the machine.
        release_escape_if_idle(&handle.state::<HostRegistry>());
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

/// How long a teardown asked for from another thread waits for the main one.
///
/// **The tradeoff, stated where the bound lives.** [`tear_down_all`] is nothing
/// but `SetParent`, `SetWindowLongPtrW` and `DestroyWindow` on windows the main
/// thread created, and Win32 is not flexible about that: a window can only be
/// destroyed by the thread that created it, and the rest is undefined rather
/// than merely discouraged. So a teardown that cannot reach the main thread
/// inside this bound is **abandoned, not performed here**. Three things make
/// that the safe half of the trade:
///
/// * The work is not cancelled. It stays queued for the main thread, ahead of
///   the window close that follows it, so a thread that was merely busy still
///   tears the panels down in the right order once it comes back.
/// * If it never comes back, neither does the close — `Window::close` is a
///   message to the same event loop — so nothing gets destroyed underneath a
///   guest either. The worst case is a shell the user ends from Task Manager,
///   at which point the job objects reap what we launched and a surviving
///   Office window is one the user can still see, save and close.
/// * A guest left docked in a window that is going away is recoverable. Win32
///   calls from a thread that does not own the window are not.
///
/// Two seconds is the same number [`super::main_thread::on_main`] gives every
/// other window call: long enough for a busy event loop, short enough that a
/// close does not feel stuck.
const TEARDOWN_TIMEOUT: Duration = Duration::from_secs(2);

/// Tear every hosted panel down, **on the thread that owns their windows**,
/// from wherever this is called.
///
/// Called on three paths that already are the main thread (the close guard's
/// two, and `RunEvent::Exit`), where the hop below is free — and on one that is
/// not: the close-ack watchdog in `lib.rs`, a thread spawned to close the window
/// when the UI never answers. That thread used to run the teardown itself. It
/// asks for it now, and gives up rather than doing it here; see
/// [`TEARDOWN_TIMEOUT`] for what "gives up" costs.
///
/// Returns whether the teardown ran.
pub fn shutdown(app: &AppHandle) -> bool {
    let handle = app.clone();
    let asked = on_main_within(app, TEARDOWN_TIMEOUT, move || {
        if let Some(registry) = handle.try_state::<HostRegistry>() {
            tear_down_all(&registry);
        }
        Ok(())
    });
    match asked {
        Ok(()) => true,
        Err(err) => {
            crate::backend::log(&format!(
                "office host: {err}; the teardown stays queued for it and nothing was torn \
                 down from here — still docked: {}",
                still_docked(app)
            ));
            false
        }
    }
}

/// The teardown itself. **Main thread only** — see [`shutdown`], which is how
/// every caller reaches it.
pub(super) fn tear_down_all(registry: &HostRegistry) {
    // Unconditionally, and before the drain: this is every path that ends the
    // window, so whatever it leaves behind, it does not leave a machine-wide
    // chord registered to a window that is about to stop existing. Called
    // directly rather than through `release_escape_if_idle`, which wants the
    // lock the line below takes.
    escape::disarm();
    let Ok(mut panels) = lock(registry) else {
        return;
    };
    for (host_id, mut panel) in panels.drain() {
        release_panel(&mut panel);
        if let Some(process) = panel.process.take() {
            // Killed here and waited for elsewhere, for the same reason
            // `host_close` does it: this is the main thread. At exit the wait
            // may not outlive the process, and does not need to — the kill has
            // already been issued, and a job object outlives us anyway.
            reaper::reap(process, || {});
        }
        crate::backend::log(&format!("office host: {host_id} torn down at exit"));
    }
}

/// What a teardown that gave up left behind, for the log line that reports it.
///
/// `try_lock` rather than `lock`: the case this runs in is a main thread that is
/// not answering, and one holding the registry while it does so is exactly the
/// shape that would turn a bounded give-up into an unbounded one.
fn still_docked(app: &AppHandle) -> String {
    let Some(registry) = app.try_state::<HostRegistry>() else {
        return "nothing (no registry)".to_string();
    };
    let Ok(panels) = registry.panels.try_lock() else {
        return "unknown (the registry is busy)".to_string();
    };
    if panels.is_empty() {
        return "nothing".to_string();
    }
    panels.keys().cloned().collect::<Vec<_>>().join(", ")
}
