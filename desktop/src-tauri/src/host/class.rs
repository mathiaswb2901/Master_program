//! The window class the panel and its clip child are made of, and the window
//! procedure that makes a hosted panel behave like part of the app.
//!
//! **This module is smaller than it was designed to be, and the measurement is
//! why.** The plan was `WM_PARENTNOTIFY`: the documented way for a parent to
//! hear that a child was destroyed or clicked, sent — the docs say — to the
//! parent and every ancestor. It is the obvious mechanism for "Word quit" and
//! for "the user clicked into the document", and it costs nothing.
//!
//! It does not arrive. For a window that was created top-level in **another
//! process** and then reparented in with `SetParent`, the panel receives no
//! `WM_PARENTNOTIFY` at all: not for a graceful `WM_CLOSE`/`DestroyWindow`, not
//! for a killed process, and not for a real mouse click driven with
//! `SendInput`. All three are asserted in [`super::hosting_tests`], the last of
//! them with the pointer actually moving. So:
//!
//! * **Destruction** is found by asking — a cheap sweep in
//!   [`super::watchdog`], which is the same signal the Python `HostBackend`
//!   models as `poll`. That method existing was a good call before any of this
//!   was written; it turns out to be the *only* signal there is.
//! * **Click-to-focus** needs no code. The click is delivered straight to the
//!   guest, whose own window procedure focuses itself — measured, and the
//!   keyboard follows. What is lost is only the *notification*: the webview
//!   never learns that a native child took the focus, which is a job for a
//!   focus hook and is deferred with the panel that would consume it.
//!
//! **A panel has to be put in front of the webview, twice, and neither half is
//! optional.** A panel window is created as a child of the Tauri window, which
//! already contains `WRY_WEBVIEW` — wry's host for WebView2 — covering the whole
//! client area. Two separate Win32 facts conspire to bury a docked document
//! under it, and fixing one without the other makes things worse rather than
//! better. Both are measured in [`super::hosting_tests`] and both were seen in
//! the running shell with a real Word docked ([`super::demo`]'s z-order report).
//!
//! 1. **A child window is created at the *bottom* of its siblings' Z order.**
//!    `CreateWindowEx`'s documentation says a new window goes "to the top of the
//!    Z order" without distinguishing children, and the folklore repeats it; for
//!    a `WS_CHILD` it is the other way round
//!    (`a_new_child_window_is_created_below_its_siblings`). So a panel created
//!    long after the webview lands underneath it, and `WindowFromPoint` over a
//!    docked document answers `Chrome_RenderWidgetHostHWND` — which is exactly
//!    what #127's in-shell verification saw. [`super::zorder::raise_to_top`] is
//!    the answer, and [`create_panel`] calls it for every panel that has a
//!    parent.
//!
//! 2. **`WRY_WEBVIEW` is created without `WS_CLIPSIBLINGS`**, so it paints
//!    across whatever is in front of it. Raising the panel alone therefore fixed
//!    hit-testing and *not* pixels: the document was reachable by the mouse and
//!    still invisible, which is the worst of the three states, because it leaves
//!    a window the user cannot see under their cursor.
//!    [`super::zorder::ensure_siblings_clip`] adds the bit, and with both halves
//!    a real Word is visible and clickable in the panel.
//!
//! **The probe matters as much as the fix.** `WindowFromPoint` is the honest
//! answer for *input* — it is the walk the mouse takes — but it says nothing
//! about what is painted, and it walks the whole desktop, so in a test it
//! answers with whatever application happens to be in front. Ask both questions:
//! [`super::zorder::child_hit`] for "who would a click in *this window* reach",
//! and [`super::zorder::pixel_at`] with [`super::zorder::paint_control`] for
//! "whose pixels are actually on the screen". A single `WindowFromPoint` call
//! with nothing beside it is how half of this bug stayed hidden.
//!
//! What is left here is real and needed. `WM_SETFOCUS` forwards focus to the
//! guest, so a panel focused programmatically — a `Ctrl+N` chord, a command, a
//! restored layout — puts the keyboard where the user expects it rather than on
//! a window that draws nothing. It forwards only to a window that is still a
//! *descendant of this panel*: the stored handle is cleared by a sweep and can
//! therefore be up to one sweep stale, and an HWND value Windows has recycled
//! would take a plain `SetFocus` without complaining. And the class brush
//! paints the panel surface in the moments before a guest covers it.

use std::ffi::c_void;
use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::OnceLock;

use windows::core::PCWSTR;
use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::Graphics::Gdi::{CreateSolidBrush, HBRUSH};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Input::KeyboardAndMouse::SetFocus;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, GetWindowLongPtrW, IsChild, RegisterClassExW,
    SetWindowLongPtrW, ShowWindow, CREATESTRUCTW, CS_HREDRAW, CS_VREDRAW, GWLP_USERDATA, SW_HIDE,
    SW_SHOWNA, WINDOW_EX_STYLE, WM_NCCREATE, WM_NCDESTROY, WM_SETFOCUS, WNDCLASSEXW, WS_CHILD,
    WS_CLIPCHILDREN, WS_CLIPSIBLINGS, WS_OVERLAPPEDWINDOW, WS_VISIBLE,
};

use super::geometry::PhysicalRect;
use super::{HostError, HostErrorCode, WindowId};

/// One class serves the panel window and its clip child: their behaviour is the
/// same, and the difference — whether there is any per-window state at all — is
/// a null pointer in `GWLP_USERDATA`.
const CLASS_NAME: PCWSTR = windows::core::w!("WorkbenchOfficeHost");

/// DESIGN.md `--surface-panel`, dark theme (`#1A1D22`), as a Win32 `COLORREF`,
/// which is `0x00BBGGRR` rather than the `0xRRGGBB` a stylesheet writes.
///
/// It is visible only in the sliver of time before a guest covers the panel —
/// during a launch, and behind a guest that has gone away. Following the
/// webview's light/dark theme would mean plumbing the theme into the shell for
/// a few hundred milliseconds of surface; it is listed as deferred in the PR
/// rather than half-done here.
const PANEL_SURFACE: COLORREF = COLORREF(0x0022_1D1A);

/// Per-window state, owned by the window and freed in `WM_NCDESTROY`.
///
/// `AtomicIsize` rather than a `Cell`: the window procedure reads it on the
/// main thread and the watchdog clears it from its own, and one atomic is a
/// great deal less machinery than hopping a thread to write a pointer.
pub(super) struct PanelState {
    #[allow(dead_code)]
    host_id: String,
    guest: AtomicIsize,
}

impl PanelState {
    pub(super) fn new(host_id: String) -> Self {
        Self {
            host_id,
            guest: AtomicIsize::new(0),
        }
    }

    fn guest(&self) -> HWND {
        HWND(self.guest.load(Ordering::SeqCst) as *mut c_void)
    }
}

/// Register the class once per process.
fn class_atom() -> Result<u16, HostError> {
    static ATOM: OnceLock<Result<u16, String>> = OnceLock::new();
    ATOM.get_or_init(|| {
        // SAFETY: `GetModuleHandleW(None)` returns this module's instance and
        // `RegisterClassExW` reads the descriptor for the duration of the call.
        // The class name is a static wide literal.
        let atom = unsafe {
            let instance = GetModuleHandleW(None).map_err(|err| err.to_string())?;
            let class = WNDCLASSEXW {
                cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
                // Repaint the whole surface on a resize: the panel is a flat
                // colour, so there is nothing to preserve and a stale edge
                // during a drag is worse than a repaint.
                style: CS_HREDRAW | CS_VREDRAW,
                lpfnWndProc: Some(wnd_proc),
                hInstance: instance.into(),
                lpszClassName: CLASS_NAME,
                hbrBackground: HBRUSH(CreateSolidBrush(PANEL_SURFACE).0),
                ..Default::default()
            };
            RegisterClassExW(&class)
        };
        if atom == 0 {
            return Err(windows::core::Error::from_win32().to_string());
        }
        Ok(atom)
    })
    .clone()
    .map_err(|message| HostError::new(HostErrorCode::Win32, message))
}

/// Create a panel window.
///
/// `parent` is the Tauri window. `None` makes a top-level window instead, which
/// is how [`super::hosting_tests`] drives the whole mechanism with no Tauri
/// window at all — the alternative would be a second copy of this code in the
/// test, which is a test of the copy.
pub(super) fn create_panel(
    parent: Option<HWND>,
    rect: PhysicalRect,
    state: PanelState,
) -> Result<HWND, HostError> {
    let atom = class_atom()?;
    let style = if parent.is_some() {
        WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS
    } else {
        WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_CLIPCHILDREN
    };
    // Handed to `WM_NCCREATE` through `lpParam` and owned by the window from
    // that moment; `WM_NCDESTROY` is what frees it.
    let state = Box::into_raw(Box::new(state));
    // SAFETY: the atom is a registered class, the state pointer is a live
    // `Box` this window takes ownership of, and every other argument is a
    // plain value.
    let created = unsafe {
        CreateWindowExW(
            WINDOW_EX_STYLE(0),
            PCWSTR(atom as usize as *const u16),
            windows::core::w!("Workbench hosted panel"),
            style,
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            parent,
            None,
            None,
            Some(state.cast::<c_void>()),
        )
    };
    match created {
        Ok(hwnd) if !hwnd.0.is_null() => {
            // **A child window is created at the *bottom* of its siblings' Z
            // order**, not the top — measured in
            // `hosting_tests::a_new_child_window_is_created_below_its_siblings`
            // against the stock `STATIC` class, so it is Windows' behaviour and
            // not ours. The Tauri window already contains `WRY_WEBVIEW`, so a
            // panel created later lands *underneath* the whole webview: nothing
            // of the document paints, and every click at the panel's own
            // rectangle is delivered to `Chrome_RenderWidgetHostHWND`.
            //
            // A top-level panel — the one the hosting tests build — has no
            // siblings worth the call, and asking would be a `SetWindowPos` on
            // the desktop's Z order for no reason.
            if let Some(parent) = parent {
                // **Raising is only half of it.** `WRY_WEBVIEW` is created
                // without `WS_CLIPSIBLINGS`, so it paints across whatever is in
                // front of it — a panel raised above it was hit-testable and
                // still invisible, which is worse than being behind, because it
                // puts a window the user cannot see under their cursor. Both
                // halves, or neither.
                let fixed = super::zorder::ensure_siblings_clip(parent);
                if fixed > 0 {
                    crate::backend::log(&format!(
                        "office host: {fixed} sibling window(s) of the shell were painting \
                         over their neighbours; given WS_CLIPSIBLINGS"
                    ));
                }
                if let Err(err) = super::zorder::raise_to_top(hwnd) {
                    crate::backend::log(&format!(
                        "office host: the panel window could not be raised above its \
                         siblings ({err}); a docked document will be behind the webview"
                    ));
                }
            }
            Ok(hwnd)
        }
        Ok(_) => {
            // SAFETY: the window was never created, so nothing took ownership.
            drop(unsafe { Box::from_raw(state) });
            Err(HostError::new(
                HostErrorCode::Win32,
                "CreateWindowExW returned a null window",
            ))
        }
        Err(err) => {
            // SAFETY: as above.
            drop(unsafe { Box::from_raw(state) });
            Err(err.into())
        }
    }
}

/// Create the clip child that the guest is parented into.
///
/// It carries no state of its own: the only decision either window makes is
/// where focus goes, and that is the panel's.
pub(super) fn create_clip(parent: HWND, rect: PhysicalRect) -> Result<HWND, HostError> {
    let atom = class_atom()?;
    // SAFETY: as in `create_panel`, with no state to own.
    let created = unsafe {
        CreateWindowExW(
            WINDOW_EX_STYLE(0),
            PCWSTR(atom as usize as *const u16),
            windows::core::w!("Workbench hosted panel viewport"),
            WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS,
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            Some(parent),
            None,
            None,
            None,
        )
    };
    match created {
        Ok(hwnd) if !hwnd.0.is_null() => Ok(hwnd),
        _ => Err(HostError::new(
            HostErrorCode::Win32,
            "the clip child could not be created",
        )),
    }
}

/// Tell a panel window which guest it is hosting, so focus that lands on the
/// panel can be handed on. Safe from any thread.
pub(super) fn set_guest(panel: HWND, guest: Option<WindowId>) {
    if let Some(state) = state_of(panel) {
        state
            .guest
            .store(guest.map_or(0, |id| id.0), Ordering::SeqCst);
    }
}

/// Which guest a panel window believes it is hosting.
///
/// The window procedure's view of the world, readable from outside it — so a
/// test can tell "focusing did nothing" apart from "the panel never learned
/// which window to hand the focus to", which look identical from outside.
/// Nothing in the running shell needs to ask, so it exists only under test.
#[cfg(test)]
pub(super) fn guest_of(panel: HWND) -> Option<WindowId> {
    let state = state_of(panel)?;
    let guest = state.guest.load(Ordering::SeqCst);
    (guest != 0).then_some(WindowId(guest))
}

/// Destroy one of our windows. Safe to call on a window that is already gone.
/// Show or hide a panel window, and with it everything inside it.
///
/// `SW_HIDE`/`SW_SHOWNA` rather than `SW_SHOW`: the panel comes back because a
/// tab was selected, and a window that *activated* itself on the way would take
/// the keyboard from whatever the user is typing in. Children follow their
/// parent's visibility, so the guest needs no separate call — and does not get
/// one, because its own `WS_VISIBLE` bit belongs to the application.
pub(super) fn set_visible(window: WindowId, visible: bool) {
    if window.is_null() {
        return;
    }
    // SAFETY: `ShowWindow` validates the handle and fails rather than faulting.
    let _ = unsafe { ShowWindow(window.hwnd(), if visible { SW_SHOWNA } else { SW_HIDE }) };
}

pub(super) fn destroy(window: WindowId) {
    if window.is_null() {
        return;
    }
    // SAFETY: `DestroyWindow` validates the handle itself and fails rather than
    // faulting on one that has already been destroyed.
    let _ = unsafe { DestroyWindow(window.hwnd()) };
}

/// The state a window owns, if it owns any.
fn state_of<'a>(hwnd: HWND) -> Option<&'a PanelState> {
    // SAFETY: the pointer in `GWLP_USERDATA` is either null or the `Box` set in
    // `WM_NCCREATE` and freed in `WM_NCDESTROY`; between those two messages it
    // is live, and the returned reference never outlives the call that took it.
    let raw = unsafe { GetWindowLongPtrW(hwnd, GWLP_USERDATA) } as *const PanelState;
    unsafe { raw.as_ref() }
}

/// The panel window procedure. Runs on the main thread, inside Tauri's event
/// loop, for both the panel window and its clip child.
unsafe extern "system" fn wnd_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    match message {
        WM_NCCREATE => {
            // SAFETY: for `WM_NCCREATE` the system passes a `CREATESTRUCTW`,
            // whose `lpCreateParams` is exactly what `CreateWindowExW` was
            // given — here, a `Box<PanelState>` or null.
            let create = unsafe { (lparam.0 as *const CREATESTRUCTW).as_ref() };
            if let Some(create) = create {
                if !create.lpCreateParams.is_null() {
                    unsafe {
                        SetWindowLongPtrW(hwnd, GWLP_USERDATA, create.lpCreateParams as isize)
                    };
                }
            }
            unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
        }
        WM_SETFOCUS => {
            // Focus that lands on the panel belongs to the guest: the panel
            // window draws nothing a keyboard can reach. This is what makes a
            // programmatic "focus that panel" put the caret in the document.
            if let Some(state) = state_of(hwnd) {
                let guest = state.guest();
                // `IsChild`, and not merely a non-null handle. The stored value
                // is cleared by the watchdog on its *next* sweep, so it can
                // name a window that died up to half a second ago — and Windows
                // recycles HWND values, so by then an unrelated window may
                // legitimately answer to that number. `SetFocus` would then
                // succeed and put the keyboard somewhere arbitrary, which is
                // worse than doing nothing. Our guest is a descendant of this
                // panel; a recycled handle is not.
                //
                // SAFETY: plain Win32 calls. `IsChild` validates both handles
                // and answers FALSE for one that no longer exists, so the
                // `SetFocus` beyond it is reached only for a live descendant.
                if !guest.0.is_null() && unsafe { IsChild(hwnd, guest) }.as_bool() {
                    let _ = unsafe { SetFocus(Some(guest)) };
                }
            }
            LRESULT(0)
        }
        WM_NCDESTROY => {
            // SAFETY: takes back the `Box` handed over in `WM_NCCREATE`, once:
            // the slot is cleared first, so a later message cannot see it.
            let raw = unsafe { SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0) } as *mut PanelState;
            if !raw.is_null() {
                drop(unsafe { Box::from_raw(raw) });
            }
            unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
        }
        _ => unsafe { DefWindowProcW(hwnd, message, wparam, lparam) },
    }
}
