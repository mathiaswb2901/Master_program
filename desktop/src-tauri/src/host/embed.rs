//! Turning somebody else's top-level window into our child, and putting it
//! back again.
//!
//! The order is the whole trick, and it is the order the `SetParent`
//! documentation asks for in its Remarks: **strip the styles first, reparent
//! second, then tell the frame it changed.** A window that is still
//! `WS_POPUP`/`WS_CAPTION` when `SetParent` runs keeps a non-client area it no
//! longer has room for; you get a caption bar drawn inside the panel, a resize
//! border that hit-tests over the document, and — on some applications — a
//! window that quietly refuses to move. Doing it the other way round was
//! measured in the spike that preceded this PR; doing it in this order gave a
//! guest client rectangle that matched the host rectangle exactly.
//!
//! Teardown is the same steps in reverse, and it matters just as much: an
//! un-parented window with our child styles still on it is a window with no
//! caption, no border, and no way for the user to move or close it.

use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::UI::WindowsAndMessaging::{
    GetParent, GetWindowLongPtrW, GetWindowRect, IsChild, IsWindow, SetParent, SetWindowLongPtrW,
    SetWindowPos, GWL_EXSTYLE, GWL_STYLE, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOZORDER,
    SWP_SHOWWINDOW, WS_BORDER, WS_CAPTION, WS_CHILD, WS_CLIPSIBLINGS, WS_DLGFRAME, WS_EX_APPWINDOW,
    WS_EX_CLIENTEDGE, WS_EX_DLGMODALFRAME, WS_EX_STATICEDGE, WS_EX_WINDOWEDGE, WS_MAXIMIZEBOX,
    WS_MINIMIZEBOX, WS_POPUP, WS_SYSMENU, WS_THICKFRAME,
};

use super::geometry::PhysicalRect;
use super::{HostError, WindowId};

/// Everything a top-level window has that a child window must not: the frame,
/// the caption, the system menu, the resize grip, and `WS_POPUP` itself, which
/// cannot coexist with `WS_CHILD`.
fn stripped_style_bits() -> u32 {
    (WS_POPUP
        | WS_CAPTION
        | WS_THICKFRAME
        | WS_MINIMIZEBOX
        | WS_MAXIMIZEBOX
        | WS_SYSMENU
        | WS_DLGFRAME
        | WS_BORDER)
        .0
}

/// Extended styles that put a window in the taskbar or draw an outer edge
/// around it. Everything else the application asked for is left alone —
/// layered, right-to-left, composited: none of our business.
fn stripped_ex_style_bits() -> u32 {
    (WS_EX_APPWINDOW | WS_EX_WINDOWEDGE | WS_EX_DLGMODALFRAME | WS_EX_CLIENTEDGE | WS_EX_STATICEDGE)
        .0
}

/// The style a hosted guest gets. Pure arithmetic on the bits, so it is
/// unit-tested without a window.
pub fn child_style(original: u32) -> u32 {
    (original & !stripped_style_bits()) | WS_CHILD.0 | WS_CLIPSIBLINGS.0
}

/// The extended style a hosted guest gets.
pub fn child_ex_style(original: u32) -> u32 {
    original & !stripped_ex_style_bits()
}

/// A guest that is currently our child, with everything needed to undo it.
///
/// Stored as plain integers rather than `HWND`/`RECT` so the registry holding
/// it stays `Send` (see [`WindowId`]).
#[derive(Debug, Clone, Copy)]
pub struct EmbeddedGuest {
    pub guest: WindowId,
    original_style: u32,
    original_ex_style: u32,
    original_parent: WindowId,
    /// Where the window was on the desktop before we took it, in screen
    /// pixels, so `detach` gives the user back the window they had.
    original_rect: PhysicalRect,
}

/// Reparent `guest` into `clip` and put it at `rect` (in the clip child's
/// client coordinates).
pub(super) fn embed(
    clip: HWND,
    guest: HWND,
    rect: PhysicalRect,
) -> Result<EmbeddedGuest, HostError> {
    // SAFETY: every call in this block is a plain Win32 call on handles that
    // are validated by the API itself — a stale or foreign handle fails, it
    // does not fault. The `RECT` outlives the call that fills it.
    unsafe {
        if !IsWindow(Some(guest)).as_bool() {
            return Err(HostError::window_gone(
                "the guest window no longer exists".to_string(),
            ));
        }
        // Refusing to swallow our own ancestry: reparenting the window that
        // contains the clip child *into* the clip child is an instant,
        // unrecoverable loop, and an HWND arriving over IPC is not trusted
        // input.
        if guest == clip || IsChild(guest, clip).as_bool() {
            return Err(HostError::embed_refused(
                "refusing to reparent a window that already contains the panel",
            ));
        }

        let original_style = GetWindowLongPtrW(guest, GWL_STYLE) as u32;
        let original_ex_style = GetWindowLongPtrW(guest, GWL_EXSTYLE) as u32;
        let original_parent = GetParent(guest)
            .map(WindowId::from_hwnd)
            .unwrap_or(WindowId(0));
        let mut screen = RECT::default();
        GetWindowRect(guest, &mut screen)?;

        // Styles first — see the module docs.
        SetWindowLongPtrW(guest, GWL_STYLE, child_style(original_style) as isize);
        SetWindowLongPtrW(
            guest,
            GWL_EXSTYLE,
            child_ex_style(original_ex_style) as isize,
        );
        SetParent(guest, Some(clip))?;
        // `SWP_FRAMECHANGED` is what makes the new styles take: without it the
        // window keeps the non-client area it was computed with, and the panel
        // shows a strip of dead caption.
        SetWindowPos(
            guest,
            None,
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )?;

        Ok(EmbeddedGuest {
            guest: WindowId::from_hwnd(guest),
            original_style,
            original_ex_style,
            original_parent,
            original_rect: PhysicalRect {
                x: screen.left,
                y: screen.top,
                width: (screen.right - screen.left).max(1),
                height: (screen.bottom - screen.top).max(1),
            },
        })
    }
}

/// Give the window back: original styles, original parent, original place.
///
/// Best-effort by design. A guest that died between the embed and here leaves
/// nothing to restore, and reporting that as a failure would turn "the user
/// closed Word" into an error the panel has to explain.
///
/// **Settles the geometry worker first.** Guest moves are queued on another
/// thread ([`super::mover`]), and one still in flight would land *after* the
/// `SetWindowPos` below — putting the user's window at a rectangle that was
/// only ever meaningful inside a clip child it has just left.
pub(super) fn release(embedded: &EmbeddedGuest) -> Result<(), HostError> {
    super::mover::mover().settle(embedded.guest);
    let guest = embedded.guest.hwnd();
    // SAFETY: as in `embed` — plain Win32 calls on a handle the API validates.
    unsafe {
        if !IsWindow(Some(guest)).as_bool() {
            return Ok(());
        }
        SetWindowLongPtrW(guest, GWL_STYLE, embedded.original_style as isize);
        SetWindowLongPtrW(guest, GWL_EXSTYLE, embedded.original_ex_style as isize);
        let parent = if embedded.original_parent.is_null() {
            None
        } else {
            Some(embedded.original_parent.hwnd())
        };
        SetParent(guest, parent)?;
        // Now in screen coordinates again, so this is the desktop position the
        // window had before we took it.
        SetWindowPos(
            guest,
            None,
            embedded.original_rect.x,
            embedded.original_rect.y,
            embedded.original_rect.width,
            embedded.original_rect.height,
            SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows::Win32::UI::WindowsAndMessaging::{
        WS_EX_LAYERED, WS_EX_NOREDIRECTIONBITMAP, WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_VSCROLL,
    };

    #[test]
    fn an_overlapped_window_becomes_a_child() {
        let original = (WS_OVERLAPPEDWINDOW | WS_VISIBLE).0;
        let child = child_style(original);
        assert_eq!(child & WS_CHILD.0, WS_CHILD.0);
        assert_eq!(child & WS_POPUP.0, 0);
        assert_eq!(child & WS_CAPTION.0, 0);
        assert_eq!(child & WS_THICKFRAME.0, 0);
        assert_eq!(child & WS_SYSMENU.0, 0);
        // Still on screen: hiding the window is the caller's decision, not a
        // side effect of restyling it.
        assert_eq!(child & WS_VISIBLE.0, WS_VISIBLE.0);
    }

    #[test]
    fn a_popup_never_stays_a_popup() {
        // `WS_CHILD` and `WS_POPUP` together is undefined behaviour as far as
        // the window manager is concerned, and popups are what Office dialogs
        // and several splash windows are.
        let child = child_style((WS_POPUP | WS_VISIBLE).0);
        assert_eq!(child & WS_POPUP.0, 0);
        assert_eq!(child & WS_CHILD.0, WS_CHILD.0);
    }

    #[test]
    fn the_applications_own_styles_survive() {
        // A scroll bar is the application's layout, not window chrome.
        let child = child_style((WS_OVERLAPPEDWINDOW | WS_VSCROLL).0);
        assert_eq!(child & WS_VSCROLL.0, WS_VSCROLL.0);
    }

    #[test]
    fn restyling_is_idempotent() {
        let once = child_style((WS_OVERLAPPEDWINDOW | WS_VISIBLE).0);
        assert_eq!(child_style(once), once);
        let once = child_ex_style((WS_EX_APPWINDOW | WS_EX_LAYERED).0);
        assert_eq!(child_ex_style(once), once);
    }

    #[test]
    fn siblings_are_clipped_so_two_panels_cannot_paint_over_each_other() {
        assert_eq!(
            child_style(0) & WS_CLIPSIBLINGS.0,
            WS_CLIPSIBLINGS.0,
            "without WS_CLIPSIBLINGS an overlapping panel paints into its neighbour"
        );
    }

    #[test]
    fn the_taskbar_button_and_the_outer_edge_go() {
        let child = child_ex_style((WS_EX_APPWINDOW | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE).0);
        assert_eq!(child & WS_EX_APPWINDOW.0, 0);
        assert_eq!(child & WS_EX_WINDOWEDGE.0, 0);
        assert_eq!(child & WS_EX_CLIENTEDGE.0, 0);
    }

    #[test]
    fn rendering_choices_the_application_made_are_left_alone() {
        // Word and Excel both use composited surfaces; stripping these would
        // change how the application draws, which is not ours to decide.
        let original = (WS_EX_LAYERED | WS_EX_NOREDIRECTIONBITMAP | WS_EX_APPWINDOW).0;
        let child = child_ex_style(original);
        assert_eq!(child & WS_EX_LAYERED.0, WS_EX_LAYERED.0);
        assert_eq!(
            child & WS_EX_NOREDIRECTIONBITMAP.0,
            WS_EX_NOREDIRECTIONBITMAP.0
        );
    }

    #[test]
    fn the_original_styles_are_what_teardown_restores() {
        // The restore is a straight write-back of what was read, so the
        // property that matters is that stripping never *adds* information we
        // would have to invent: strip(restore(x)) == strip(x).
        let original = (WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_VSCROLL).0;
        assert_eq!(child_style(original), child_style(child_style(original)));
        assert_ne!(child_style(original), original, "nothing was stripped");
    }
}
