//! The way out of a docked document — for a keyboard, not only for a mouse.
//!
//! **The trap.** Everything else in this module is about getting a real Word or
//! Excel window *into* a panel. The moment that succeeds, a keyboard problem
//! appears that has no analogue anywhere else in the app: the guest owns its own
//! `WndProc`, so once the user clicks into the document **every keystroke is
//! delivered to Word**. The webview never sees a `keydown` — not `Ctrl+K`, not
//! the `Alt` pane chords, not `Alt+M`, not one binding in the whole keymap,
//! because those are DOM listeners on a window that is no longer where the
//! keyboard is. Nothing in `ui/` can fix that; the DOM is not in the delivery
//! path at all. So before this module, a user who clicked into a docked document
//! could only leave it by *reaching for the mouse*. That is a hard keyboard trap
//! and it breaks two invariants DESIGN.md §6.8 states outright — chords reach
//! every surface, and nothing is reachable only by mouse.
//!
//! **The one mechanism that still works.** `RegisterHotKey` is evaluated by the
//! system *before* a keystroke is dispatched to any window, so the chord below
//! arrives as a `WM_HOTKEY` in our own message queue no matter who currently
//! holds the focus — including a hung guest, which is the case where every other
//! answer (a keyboard hook chained through the guest, a `SendMessage` asking it
//! politely) would be waiting on the very window that is stuck. It is the only
//! escape that survives the worst case, which is why it is the one built.
//!
//! ```text
//!  user presses Ctrl+Alt+Home   (focus is inside Word, anywhere on the desktop)
//!   └── the system matches a registered hotkey, and does not deliver the keys
//!       └── WM_HOTKEY posted to our message-only window
//!           └── the main thread's loop dispatches it (that loop is Tauri's)
//!               └── focus::focus(main window) — the keyboard is back in the app
//! ```
//!
//! **Why `Ctrl+Alt+Home`.** A `RegisterHotKey` chord is taken from the *whole
//! machine* while it is registered, so the choice is a small tax on every other
//! application. `Ctrl+Alt+Home` is unbound in Windows itself, in Word and in
//! Excel, `Home` is the "go back to the start" key in every one of them, and it
//! carries two modifiers so it cannot be typed by accident. The one known
//! collision is **inside an RDP session**, where `mstsc` binds it to focus the
//! connection bar; a Workbench running inside such a session takes the chord and
//! the connection bar keeps its other bindings. That is the deliberate trade,
//! recorded here rather than discovered later.
//!
//! **It is armed only while a document is docked** ([`super::commands`] keeps it
//! in step with the registry). There is no reason to take a machine-wide chord
//! away from every other application while no trap exists, and "armed exactly
//! while the thing it rescues you from is on screen" is also the honest answer to
//! "why did my chord stop working in that other app" — it did not, unless
//! Workbench has a document open.
//!
//! **A window of our own, not a subclass of Tauri's.** `WM_HOTKEY` goes to the
//! window named in `RegisterHotKey`, and seeing it means owning that window's
//! procedure. Subclassing the Tauri window with `SetWindowLongPtrW(GWLP_WNDPROC)`
//! would put our code in front of `tao`'s for every message it handles, forever,
//! to read one; a message-only window costs nothing, is invisible by
//! construction, can never appear in Alt+Tab or on a taskbar, and is destroyed
//! by [`disarm`] on the way out. Its messages are dispatched by the same loop as
//! everything else — `DispatchMessageW` routes by `hwnd`, and the panel windows
//! in [`super::class`] already rely on exactly that.
//!
//! **Main thread only**, like every other window call here: the hotkey belongs to
//! the thread that registered it, and the `WndProc` runs where its window was
//! created. [`super::main_thread`] is how off-thread callers reach it.

use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::OnceLock;

use windows::core::PCWSTR;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Input::KeyboardAndMouse::{
    RegisterHotKey, UnregisterHotKey, HOT_KEY_MODIFIERS, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT,
    VK_HOME,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, GetClassNameW, RegisterClassExW, HWND_MESSAGE,
    WINDOW_EX_STYLE, WM_HOTKEY, WNDCLASSEXW, WS_OVERLAPPED,
};

use super::{focus, HostError, HostErrorCode, WindowId};

/// The chord, as a person reads it.
///
/// Written in exactly one place and then quoted everywhere it is promised:
/// `ui/src/panels/OfficeHostPanel.tsx` (the hint under a docked document),
/// DESIGN.md §6.8 (the keymap), and the log line below. Changing it here is
/// changing a documented binding — those three go with it.
pub(super) const CHORD: &str = "Ctrl+Alt+Home";

/// The modifiers, with `MOD_NOREPEAT` so holding the chord down is one escape
/// rather than a stream of `SetFocus` calls.
const MODIFIERS: HOT_KEY_MODIFIERS = HOT_KEY_MODIFIERS(MOD_CONTROL.0 | MOD_ALT.0 | MOD_NOREPEAT.0);

/// Our id for the hotkey. Scoped to the window it is registered on, so it only
/// has to be unique among ours — there is one. `pub(super)` because it is also
/// the `WPARAM` the system sends, and a test posts exactly that.
pub(super) const HOTKEY_ID: i32 = 1;

/// The message-only window that owns the hotkey, or 0 when the escape is not
/// armed. Written and read on the main thread only; an atomic because the
/// `WndProc` is a plain `fn` and this is how it finds its own state without a
/// `GWLP_USERDATA` dance for a single number.
static ESCAPE_WINDOW: AtomicIsize = AtomicIsize::new(0);

/// Where the keyboard goes when the chord fires: the shell's own window, which
/// is what [`super::commands::reclaim_focus`] already treats as "back in the
/// app". 0 means nothing has aimed it yet, and the chord then does nothing
/// rather than guessing.
static TARGET: AtomicIsize = AtomicIsize::new(0);

/// Take the chord from the system and aim it at `fallback`.
///
/// Idempotent: called on every embed, and an escape that is already armed is
/// only re-aimed. A refusal is returned *and* logged — the chord being taken by
/// another application is a real thing that happens, and the user's evidence for
/// it is the log line, since the panel's hint cannot ask.
///
/// **Main thread only.**
pub(super) fn arm(fallback: WindowId) -> Result<(), HostError> {
    if fallback.is_null() {
        return Err(HostError::window_gone(
            "no window to hand the keyboard back to",
        ));
    }
    TARGET.store(fallback.0, Ordering::SeqCst);
    if is_armed() {
        return Ok(());
    }

    let window = create_message_window()?;
    // SAFETY: a plain Win32 call on a window this thread just created. It fails
    // rather than faulting, and `ERROR_HOTKEY_ALREADY_REGISTERED` is the failure
    // that matters — another application owns this chord.
    if let Err(err) =
        unsafe { RegisterHotKey(Some(window), HOTKEY_ID, MODIFIERS, VK_HOME.0.into()) }
    {
        // SAFETY: destroying a window this thread created moments ago.
        let _ = unsafe { DestroyWindow(window) };
        crate::backend::log(&format!(
            "office host: {CHORD} could not be registered ({err}); a docked document has no \
             keyboard escape in this session — the affordance under the document still works"
        ));
        return Err(HostError::new(
            HostErrorCode::Win32,
            format!("{CHORD} could not be registered: {err}"),
        ));
    }
    ESCAPE_WINDOW.store(window.0 as isize, Ordering::SeqCst);
    crate::backend::log(&format!(
        "office host: {CHORD} armed — it takes the keyboard out of a docked document"
    ));
    Ok(())
}

/// Give the chord back to the system and forget the target.
///
/// Idempotent, and called from every path that ends a hosting. Nothing is docked
/// afterwards, so nothing needs rescuing — and a chord we no longer need is one
/// every other application on the machine gets back.
///
/// **Main thread only.** `UnregisterHotKey` answers to the registering thread and
/// `DestroyWindow` to the creating one, which here are the same thread.
pub(super) fn disarm() {
    let window = ESCAPE_WINDOW.swap(0, Ordering::SeqCst);
    TARGET.store(0, Ordering::SeqCst);
    if window == 0 {
        return;
    }
    let hwnd = HWND(window as *mut core::ffi::c_void);
    // SAFETY: both are plain Win32 calls on a window this thread created; each
    // validates its own handle and fails rather than faulting.
    unsafe {
        let _ = UnregisterHotKey(Some(hwnd), HOTKEY_ID);
        let _ = DestroyWindow(hwnd);
    }
    crate::backend::log(&format!(
        "office host: {CHORD} released — nothing is docked, so the chord is the machine's again"
    ));
}

/// Is the chord ours right now?
pub(super) fn is_armed() -> bool {
    ESCAPE_WINDOW.load(Ordering::SeqCst) != 0
}

/// The window the escape is aimed at, or `None`. Exists so a test can tell
/// "the chord fired and had nowhere to go" apart from "the chord never fired".
#[cfg(test)]
pub(super) fn target() -> Option<WindowId> {
    let target = TARGET.load(Ordering::SeqCst);
    (target != 0).then_some(WindowId(target))
}

/// The window that owns the hotkey, for a test that wants to post to it.
#[cfg(test)]
pub(super) fn window() -> Option<WindowId> {
    let window = ESCAPE_WINDOW.load(Ordering::SeqCst);
    (window != 0).then_some(WindowId(window))
}

/// Try to take **the same chord** for `window`, under an id of the caller's own.
///
/// Test-only, and the only honest way to check that [`arm`] did something: an
/// `Ok` from `RegisterHotKey` is a claim about our own call, while this is the
/// system's answer to "is this chord taken". `Err` here means it is ours.
#[cfg(test)]
pub(super) fn try_take_the_chord(window: WindowId, id: i32) -> windows::core::Result<()> {
    // SAFETY: a plain Win32 call on a live window; it fails rather than faulting.
    unsafe { RegisterHotKey(Some(window.hwnd()), id, MODIFIERS, VK_HOME.0.into()) }
}

/// Give back what [`try_take_the_chord`] took.
#[cfg(test)]
pub(super) fn release_the_chord(window: WindowId, id: i32) {
    // SAFETY: as above.
    let _ = unsafe { UnregisterHotKey(Some(window.hwnd()), id) };
}

/// The chord fired: put the keyboard back in the app.
///
/// A plain `SetFocus` through the existing seam, with everything
/// [`super::focus`] says about it applying unchanged — in particular that it
/// cannot pull this window in front of an application the user has switched to.
/// The chord is machine-wide, so it *will* fire while Workbench is in the
/// background; what happens then is that the caret is waiting in the right place
/// when they come back, which is the correct outcome and not a stolen focus.
///
/// It is not tested through a return value: what it did is a fact about the
/// input queue, and
/// `hosting_tests::a_hotkey_message_takes_the_keyboard_out_of_a_docked_document`
/// reads that back instead of taking this function's word for it.
fn hand_back() {
    let target = WindowId(TARGET.load(Ordering::SeqCst));
    if target.is_null() {
        crate::backend::log(&format!(
            "office host: {CHORD} fired with nothing to hand the keyboard to"
        ));
        return;
    }
    if let Err(err) = focus::focus(target) {
        crate::backend::log(&format!("office host: {CHORD} could not hand back: {err}"));
        return;
    }
    // The class of whatever ended up with it, because "the keyboard came back"
    // and "the keyboard came back *to the webview*" are different claims and a
    // bug report needs to say which one happened. It is what the running-shell
    // verification for this change read: `Chrome_WidgetWin_1`, the WebView2
    // widget — so `SetFocus` on the shell's own window really does put the
    // keyboard back inside the page, not merely on the frame around it.
    crate::backend::log(&format!(
        "office host: {CHORD} handed the keyboard back to {}",
        described(focus::focused_here())
    ));
}

/// A focused window as a log line: its class and handle, or that there is none.
fn described(window: Option<WindowId>) -> String {
    let Some(window) = window else {
        return "nothing (the queue reports no focus)".to_string();
    };
    let mut name = [0u16; 128];
    // SAFETY: `GetClassNameW` writes at most `name.len()` units into a live
    // buffer and validates the handle itself.
    let written = unsafe { GetClassNameW(window.hwnd(), &mut name) };
    let class = if written > 0 {
        String::from_utf16_lossy(&name[..written as usize])
    } else {
        "?".to_string()
    };
    format!("{class} ({:#x})", window.0)
}

/// The window class the escape window is made of. Registered once per process,
/// like [`super::class`]'s, and for the same reason: a class is a process-wide
/// registration and re-registering it fails.
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
                lpfnWndProc: Some(wnd_proc),
                hInstance: instance.into(),
                lpszClassName: windows::core::w!("WorkbenchKeyboardEscape"),
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

/// A message-only window: no pixels, no z-order, no taskbar, no Alt+Tab — a
/// message queue endpoint and nothing else.
fn create_message_window() -> Result<HWND, HostError> {
    let atom = class_atom()?;
    // SAFETY: the atom names a registered class and every other argument is a
    // plain value; `HWND_MESSAGE` as the parent is what makes this message-only.
    let created = unsafe {
        CreateWindowExW(
            WINDOW_EX_STYLE(0),
            PCWSTR(atom as usize as *const u16),
            windows::core::w!("Workbench keyboard escape"),
            WS_OVERLAPPED,
            0,
            0,
            0,
            0,
            Some(HWND_MESSAGE),
            None,
            None,
            None,
        )
    };
    match created {
        Ok(hwnd) if !hwnd.0.is_null() => Ok(hwnd),
        Ok(_) => Err(HostError::new(
            HostErrorCode::Win32,
            "the keyboard escape window could not be created",
        )),
        Err(err) => Err(err.into()),
    }
}

/// The escape window's procedure. One message matters.
unsafe extern "system" fn wnd_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if message == WM_HOTKEY && wparam.0 as i32 == HOTKEY_ID {
        hand_back();
        return LRESULT(0);
    }
    // SAFETY: the default handling for a window this module created.
    unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_chord_is_two_modifiers_and_never_repeats() {
        // A one-modifier chord would be typeable by accident inside a document,
        // and a repeating one turns a held key into a stream of `SetFocus`.
        assert_eq!(MODIFIERS.0 & MOD_CONTROL.0, MOD_CONTROL.0);
        assert_eq!(MODIFIERS.0 & MOD_ALT.0, MOD_ALT.0);
        assert_eq!(MODIFIERS.0 & MOD_NOREPEAT.0, MOD_NOREPEAT.0);
        // The name the UI and DESIGN.md quote has to be the chord that is
        // actually registered, or the hint under the document is a lie.
        assert_eq!(CHORD, "Ctrl+Alt+Home");
        assert_eq!(u32::from(VK_HOME.0), 0x24);
    }

    #[test]
    fn an_escape_with_nowhere_to_go_is_refused_rather_than_aimed_at_nothing() {
        let err = arm(WindowId(0)).expect_err("a null fallback cannot be armed");
        assert_eq!(err.code, HostErrorCode::WindowGone);
    }
}
