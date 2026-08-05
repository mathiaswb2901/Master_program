//! Where the keyboard goes.
//!
//! A hosted panel has to behave like the rest of the window: click into the
//! document and typing goes to the document; click back into the file tree and
//! typing goes to the webview. Both directions are one `SetFocus`, and neither
//! of them calls `AttachThreadInput`.
//!
//! **That is a claim, not an assumption.** `SetFocus` normally fails across
//! threads — the focus is a property of a thread's input queue, and you cannot
//! set it in a queue you are not attached to. The usual fix is
//! `AttachThreadInput`, which is a blunt instrument: it welds two threads'
//! input state together for as long as it is in force, and every guide to it
//! warns about deadlocks. The reason it is not needed here is that making a
//! window the *child* of a window owned by another thread already attaches
//! those threads' input queues — the same mechanism that creates this feature's
//! central risk (a hung guest freezing the host's input) also hands us focus
//! routing for free.
//!
//! `hosting_tests::focus_reaches_the_guest_without_attaching_input_queues`
//! measures it rather than repeating it: it embeds a guest owned by another
//! process, calls [`focus`], and reads the guest thread's own idea of where the
//! focus is back out with `GetGUIThreadInfo`.

use windows::Win32::Foundation::HWND;
use windows::Win32::UI::Input::KeyboardAndMouse::SetFocus;
use windows::Win32::UI::WindowsAndMessaging::{
    GetGUIThreadInfo, GetWindowThreadProcessId, GUITHREADINFO,
};

use super::{HostError, WindowId};

/// Hand the keyboard to `window`.
pub fn focus(window: WindowId) -> Result<(), HostError> {
    if window.is_null() {
        return Err(HostError::window_gone("no window to focus"));
    }
    // SAFETY: a plain Win32 call; an invalid handle fails rather than faults.
    let _ = unsafe { SetFocus(Some(window.hwnd())) };
    // `SetFocus` returns the *previously* focused window, and null is both
    // "there was none" and "it failed" — so the return value is not the
    // evidence. `focused_window_of` is.
    Ok(())
}

/// Which thread owns this window.
pub fn owning_thread(window: WindowId) -> u32 {
    if window.is_null() {
        return 0;
    }
    // SAFETY: a plain Win32 call; returns 0 for an invalid handle.
    unsafe { GetWindowThreadProcessId(window.hwnd(), None) }
}

/// What that thread thinks is focused.
///
/// The honest way to ask a question about another thread's focus: `GetFocus`
/// only ever answers for the caller's own queue, which would make any
/// cross-process assertion about focus a tautology.
pub fn focused_window_of(thread_id: u32) -> Option<WindowId> {
    let mut info = GUITHREADINFO {
        cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
        ..Default::default()
    };
    // SAFETY: `info` is a correctly sized, live `GUITHREADINFO` for the
    // duration of the call.
    unsafe { GetGUIThreadInfo(thread_id, &mut info) }.ok()?;
    let focused: HWND = info.hwndFocus;
    if focused.0.is_null() {
        None
    } else {
        Some(WindowId::from_hwnd(focused))
    }
}
