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
//! What is left here is real and needed. `WM_SETFOCUS` forwards focus to the
//! guest, so a panel focused programmatically — a `Ctrl+N` chord, a command, a
//! restored layout — puts the keyboard where the user expects it rather than on
//! a window that draws nothing. And the class brush paints the panel surface in
//! the moments before a guest covers it.

use std::ffi::c_void;
use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::OnceLock;

use windows::core::PCWSTR;
use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::Graphics::Gdi::{CreateSolidBrush, HBRUSH};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Input::KeyboardAndMouse::SetFocus;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, GetWindowLongPtrW, RegisterClassExW,
    SetWindowLongPtrW, CREATESTRUCTW, CS_HREDRAW, CS_VREDRAW, GWLP_USERDATA, WINDOW_EX_STYLE,
    WM_NCCREATE, WM_NCDESTROY, WM_SETFOCUS, WNDCLASSEXW, WS_CHILD, WS_CLIPCHILDREN,
    WS_CLIPSIBLINGS, WS_OVERLAPPEDWINDOW, WS_VISIBLE,
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
        Ok(hwnd) if !hwnd.0.is_null() => Ok(hwnd),
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
                if !guest.0.is_null() {
                    // SAFETY: a plain Win32 call; a stale handle fails rather
                    // than faults, and a stale handle means the guest has gone,
                    // which the watchdog is already about to notice.
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
