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
use windows::Win32::UI::Input::KeyboardAndMouse::{GetFocus, SetFocus};
use windows::Win32::UI::WindowsAndMessaging::{
    GetGUIThreadInfo, GetWindowThreadProcessId, IsWindow, GUITHREADINFO,
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

/// Where the keyboard is, as **this thread's** input queue sees it.
///
/// Queue-local, and that is exactly the scope wanted: a guest parented into one
/// of our windows has had its queue attached to ours (see above), so this
/// answers for a hosted document too. `None` is the interesting answer — it
/// means nothing in this application holds the keyboard at all.
///
/// Must be called from the thread that owns the windows, i.e. the main thread.
pub fn focused_here() -> Option<WindowId> {
    // SAFETY: a plain Win32 call with no arguments.
    let focused = unsafe { GetFocus() };
    (!focused.0.is_null()).then(|| WindowId::from_hwnd(focused))
}

/// Hand the keyboard back to `fallback` when the window holding it can no
/// longer do anything with it. Returns whether it acted.
///
/// A hosted document that goes away takes the keyboard with it: the user was
/// typing in it when it crashed, or when they closed the panel it was in.
/// Nothing puts the focus back by itself, and the only way out for the user is
/// to reach for the mouse. So both paths that end a hosting — the watchdog's
/// `commands::on_guest_gone` and `commands::release_panel`'s callers — call
/// this afterwards. Three ways it is stranded, all measured in
/// `hosting_tests` rather than assumed:
///
/// * **nothing holds it.** Destroying a focused *top-level* window leaves the
///   queue's focus null.
/// * **a handle that no longer names a window holds it** — the same, with a
///   stale value left behind, and one Windows may since have recycled onto some
///   unrelated window. Not somewhere to leave a caret.
/// * **one of `dead_ends` holds it.** `DestroyWindow` on a focused *child*
///   promotes the focus to its parent, so a guest that dies inside a panel
///   hands the keyboard to our own clip child — a window that draws nothing, is
///   now hosting nothing, and has no guest left to forward `WM_SETFOCUS` to.
///   Alive, and every bit as dead an end as a destroyed window.
///
/// **A plain `SetFocus`, never `SetForegroundWindow`.** Focus belongs to an
/// input queue and only the *active* queue receives keystrokes, so this cannot
/// take the keyboard away from an application the user has switched to; at
/// worst it decides where the caret lands when they come back. A document that
/// crashes while the user is in another window must not pull ours to the front.
pub fn reclaim_if_stranded(fallback: WindowId, dead_ends: &[WindowId]) -> bool {
    if fallback.is_null() {
        return false;
    }
    let stranded = match focused_here() {
        None => true,
        Some(window) => {
            // SAFETY: `IsWindow` validates the handle itself.
            !unsafe { IsWindow(Some(window.hwnd())) }.as_bool() || dead_ends.contains(&window)
        }
    };
    stranded && focus(fallback).is_ok()
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
