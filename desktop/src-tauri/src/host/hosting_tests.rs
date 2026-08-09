//! The mechanism, driven end to end against a real second process.
//!
//! These are the tests that need a **window station**: they register a class,
//! create windows, start the synthetic guest, reparent it, move it, focus it and
//! tear it down. Every one of them runs in `cargo test` on a normal Windows
//! desktop; the ones that additionally need to be the *foreground* application —
//! only the hang measurement does — are `#[ignore]`d and say so.
//!
//! They live inside the crate rather than in `tests/` on purpose: the mechanism
//! is `pub(super)` and should stay that way. Making `SetParent` public so a
//! test in another crate can call it would widen the shell's API for the
//! benefit of the test alone.
//!
//! Everything here is serialised through [`SERIAL`]. Window tests share one
//! desktop: two of them fighting over focus at the same time would measure each
//! other.

use std::io;
use std::sync::atomic::{AtomicBool, AtomicIsize, AtomicU32, Ordering};
use std::sync::{mpsc, Arc, Mutex, MutexGuard};
use std::thread::{self, ThreadId};
use std::time::{Duration, Instant};

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::Graphics::Gdi::InvalidateRect;
use windows::Win32::UI::WindowsAndMessaging::{
    CallWindowProcW, DispatchMessageW, GetClientRect, GetParent, GetWindowLongPtrW, GetWindowRect,
    IsHungAppWindow, IsWindow, PeekMessageW, PostMessageW, SendMessageTimeoutW, SetWindowLongPtrW,
    TranslateMessage, GWLP_WNDPROC, GWL_STYLE, MSG, PM_REMOVE, SMTO_ABORTIFHUNG, WM_APP, WM_CLOSE,
    WM_NULL, WM_PAINT, WM_PARENTNOTIFY, WS_CAPTION, WS_CHILD, WS_POPUP, WS_THICKFRAME,
};

use super::class::{self, PanelState};
use super::embed::{self, EmbeddedGuest};
use super::geometry::{host_layout, CssRect, HostLayout, PhysicalRect};
use super::guest::{self, GuestProcess};
use super::layout::{self, Coalescer};
use super::{commands, focus, main_thread, reaper, zorder};
use super::{HostError, HostErrorCode, HostRegistry, Panel, WindowId};

/// One desktop, one window test at a time.
static SERIAL: Mutex<()> = Mutex::new(());

/// How long the guest gets to start. Generous: a cold `target/debug` binary on
/// a busy machine is not instant.
const LAUNCH_TIMEOUT: Duration = Duration::from_secs(20);
/// How long any "the window manager should have caught up by now" wait runs
/// before the assertion behind it is allowed to fail.
const SETTLE_TIMEOUT: Duration = Duration::from_secs(3);

/// A panel window, its clip child and the guest inside them — the whole
/// arrangement `commands.rs` builds, assembled directly so the test can hold
/// each piece.
struct Fixture {
    panel: HWND,
    clip: HWND,
    /// A second window of ours with no guest in it, standing in for the
    /// webview. It cannot be the panel window: the panel deliberately forwards
    /// `WM_SETFOCUS` to its guest, so focusing it is a way of focusing the
    /// guest, not a way of taking focus away from it.
    elsewhere: HWND,
    guest_window: WindowId,
    /// `Option` because a close test hands the instance to the code under test
    /// — which is where the disposal now happens — and the fixture must then
    /// not reap it a second time.
    guest_process: Option<GuestProcess>,
    embedded: Option<EmbeddedGuest>,
    layout: HostLayout,
    /// What the guest itself said its `HWND` was.
    handshake: isize,
    /// **Declared last on purpose.** Struct fields drop in declaration order,
    /// so holding the lock in the *first* field released it before any of the
    /// teardown below it had run — windows still being destroyed and a guest
    /// still being reaped while the next test was already launching its own.
    /// Last means the desktop is quiet again before anyone else gets it.
    _serial: MutexGuard<'static, ()>,
}

impl Fixture {
    /// Launch a guest and dock it into a fresh panel.
    ///
    /// The panel window here is top-level rather than a child of a Tauri
    /// window: the arrangement below it — panel, clip child, reparented guest —
    /// is identical, and it is the arrangement under test.
    fn new(size: (i32, i32), caption_inset: i32) -> Self {
        Self::with_class(size, caption_inset, guest::SYNTHETIC_GUEST_CLASS)
    }

    /// Dock a guest that registers itself under `class`.
    ///
    /// The default class stands in for both Word (`OpusApp`) and Excel
    /// (`XLMAIN`); a test names one explicitly to prove the mechanism keys on
    /// nothing but the class string it is handed.
    fn with_class(size: (i32, i32), caption_inset: i32, class: &str) -> Self {
        let serial = SERIAL.lock().unwrap_or_else(|err| err.into_inner());
        let exe = guest::synthetic_guest_exe().expect("the synthetic guest binary should be built");
        let mut launched = guest::launch(&exe, &[class], class, LAUNCH_TIMEOUT)
            .expect("the synthetic guest should start and show a window");
        let handshake = read_handshake(&mut launched.process);

        let plan = host_layout(
            PhysicalRect {
                x: 80,
                y: 80,
                width: size.0,
                height: size.1,
            },
            caption_inset,
        );
        let panel = class::create_panel(None, plan.panel, PanelState::new("test".to_string()))
            .expect("the panel window should be created");
        let clip = class::create_clip(panel, plan.clip).expect("the clip child should be created");
        let elsewhere = class::create_panel(
            None,
            PhysicalRect {
                x: 40,
                y: 40,
                width: 200,
                height: 120,
            },
            PanelState::new("elsewhere".to_string()),
        )
        .expect("the stand-in window should be created");

        Self {
            panel,
            clip,
            elsewhere,
            guest_window: launched.window,
            guest_process: Some(launched.process),
            embedded: None,
            layout: plan,
            handshake,
            _serial: serial,
        }
    }

    /// The instance the fixture launched, while it still owns it.
    fn process(&mut self) -> &mut GuestProcess {
        self.guest_process
            .as_mut()
            .expect("the fixture still owns its guest process")
    }

    /// Hand the instance to the code under test. From here the fixture stops
    /// reaping it, so whatever takes it is responsible for the kill.
    fn take_process(&mut self) -> GuestProcess {
        self.guest_process
            .take()
            .expect("the fixture still owns its guest process")
    }

    /// Give up ownership of our two windows, for a test that hands them to the
    /// production teardown. Anything the fixture no longer owns it must not
    /// destroy a second time.
    fn disown_windows(&mut self) -> (WindowId, WindowId) {
        let ours = (
            WindowId::from_hwnd(self.panel),
            WindowId::from_hwnd(self.clip),
        );
        self.panel = HWND(std::ptr::null_mut());
        self.clip = HWND(std::ptr::null_mut());
        ours
    }

    fn embed(&mut self) {
        let embedded = embed::embed(self.clip, self.guest_window.hwnd(), self.layout.guest)
            .expect("the guest should be reparented into the clip child");
        class::set_guest(self.panel, Some(embedded.guest));
        self.embedded = Some(embedded);
        settle();
    }

    fn apply(&mut self, plan: HostLayout) {
        self.layout = plan;
        layout::apply(
            self.panel,
            self.clip,
            self.embedded.map(|embedded| embedded.guest.hwnd()),
            &plan,
        )
        .expect("a batched move should be accepted");
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(embedded) = self.embedded.take() {
            let _ = embed::release(&embedded);
        }
        class::destroy(WindowId::from_hwnd(self.clip));
        class::destroy(WindowId::from_hwnd(self.panel));
        class::destroy(WindowId::from_hwnd(self.elsewhere));
        if let Some(mut process) = self.guest_process.take() {
            process.reap();
        }
    }
}

// ---- helpers -----------------------------------------------------------------

/// Read the guest's own account of its window handle.
///
/// Stops at the handshake line, which closes the pipe — so the guest prints it
/// **last** and writes without panicking on a closed one. See the comment
/// beside those writes in `src/bin/workbench-guest.rs`.
fn read_handshake(process: &mut GuestProcess) -> isize {
    use std::io::{BufRead, BufReader};
    let stdout = process.take_stdout().expect("the guest's stdout is piped");
    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if let Some(value) = line.trim().strip_prefix(guest::SYNTHETIC_GUEST_HANDSHAKE) {
            if let Ok(handle) = value.trim().parse::<isize>() {
                return handle;
            }
        }
    }
    0
}

/// Run the message loop for a while. Our windows need it to service their own
/// messages, and the asynchronous `SetWindowPos` needs the other thread to get
/// a turn.
fn pump_for(duration: Duration) {
    let deadline = Instant::now() + duration;
    while Instant::now() < deadline {
        pump_once();
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn pump_once() {
    let mut message = MSG::default();
    // SAFETY: the standard non-blocking message loop, on this thread's own queue.
    unsafe {
        while PeekMessageW(&mut message, None, 0, 0, PM_REMOVE).as_bool() {
            let _ = TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
}

fn settle() {
    pump_for(Duration::from_millis(120));
}

/// Pump until `check` holds, or give up. Returns whether it held — every caller
/// asserts on that, so a timeout is a failure with the assertion's own message.
fn wait_until(mut check: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + SETTLE_TIMEOUT;
    while Instant::now() < deadline {
        pump_once();
        if check() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    check()
}

fn window_rect(window: HWND) -> RECT {
    let mut rect = RECT::default();
    // SAFETY: a plain Win32 call on a live window.
    let _ = unsafe { GetWindowRect(window, &mut rect) };
    rect
}

fn client_size(window: HWND) -> (i32, i32) {
    let mut rect = RECT::default();
    // SAFETY: as above.
    let _ = unsafe { GetClientRect(window, &mut rect) };
    (rect.right - rect.left, rect.bottom - rect.top)
}

fn style_of(window: HWND) -> u32 {
    // SAFETY: as above.
    unsafe { GetWindowLongPtrW(window, GWL_STYLE) as u32 }
}

fn parent_of(window: HWND) -> Option<HWND> {
    // SAFETY: as above; a top-level window with no owner reports an error,
    // which is exactly the "no parent" answer.
    unsafe { GetParent(window) }
        .ok()
        .filter(|hwnd| !hwnd.0.is_null())
}

fn exists(window: WindowId) -> bool {
    // SAFETY: `IsWindow` validates the handle itself.
    unsafe { IsWindow(Some(window.hwnd())).as_bool() }
}

// ---- the tests ---------------------------------------------------------------

#[test]
fn a_guest_window_is_found_by_class_and_pid() {
    // The finder is what will identify Word's `OpusApp` frame, so it is checked
    // against the only other source of truth there is: the guest's own report
    // of its handle.
    let fixture = Fixture::new((900, 600), 0);
    assert_ne!(fixture.handshake, 0, "the guest never printed its handle");
    assert_eq!(
        fixture.guest_window.0, fixture.handshake,
        "the class+pid search found a different window than the guest created"
    );
    // And the search is specific: nothing of that class belongs to *our* pid.
    assert!(guest::find_window(std::process::id(), guest::SYNTHETIC_GUEST_CLASS).is_none());
}

#[test]
fn an_xlmain_frame_docks_the_way_words_opusapp_frame_does() {
    // Excel's top-level frame is `XLMAIN` where Word's is `OpusApp`. Native
    // hosting keys on *neither*: `launch` finds the frame by the class it is
    // told, and `embed` reparents the HWND that came back and strips its styles
    // — the same code either way. A guest that registers itself as `XLMAIN` is
    // therefore found and docked exactly as Word is, which is the whole claim of
    // extending hosting to Excel: the frame class differs, the docking does not.
    let mut fixture = Fixture::with_class((820, 560), 0, "XLMAIN");
    assert_eq!(
        guest::find_window(fixture.process().pid(), "XLMAIN"),
        Some(fixture.guest_window),
        "the XLMAIN frame was not found by its own class and pid",
    );

    let guest_hwnd = fixture.guest_window.hwnd();
    assert_eq!(
        style_of(guest_hwnd) & WS_CHILD.0,
        0,
        "the XLMAIN guest should start as a top-level window",
    );

    fixture.embed();

    let after = style_of(guest_hwnd);
    assert_eq!(
        after & WS_CHILD.0,
        WS_CHILD.0,
        "the XLMAIN guest is not a child"
    );
    assert_eq!(after & WS_CAPTION.0, 0, "the caption survived the restyle");
    assert_eq!(after & WS_THICKFRAME.0, 0, "the resize frame survived");
    assert_eq!(
        parent_of(guest_hwnd),
        Some(fixture.clip),
        "the XLMAIN guest is not inside the clip child",
    );
}

#[test]
fn embedding_strips_the_frame_and_reparents() {
    let mut fixture = Fixture::new((820, 560), 0);
    let guest_hwnd = fixture.guest_window.hwnd();
    let before = style_of(guest_hwnd);
    assert_eq!(
        before & WS_CHILD.0,
        0,
        "the guest should start as a top-level window"
    );

    fixture.embed();

    let after = style_of(guest_hwnd);
    assert_eq!(after & WS_CHILD.0, WS_CHILD.0, "the guest is not a child");
    assert_eq!(after & WS_POPUP.0, 0, "WS_POPUP survived the restyle");
    assert_eq!(after & WS_CAPTION.0, 0, "the caption survived the restyle");
    assert_eq!(after & WS_THICKFRAME.0, 0, "the resize frame survived");
    assert_eq!(
        parent_of(guest_hwnd),
        Some(fixture.clip),
        "the guest is not inside the clip child"
    );
}

#[test]
fn the_hosted_window_matches_the_panel_rectangle_exactly() {
    // The spike's measurement, kept honest: after the restyle the guest has no
    // non-client area at all, so its *client* size is the size we asked for —
    // not "close enough", exactly.
    let mut fixture = Fixture::new((760, 520), 0);
    fixture.embed();

    let expected = (fixture.layout.guest.width, fixture.layout.guest.height);
    let guest_hwnd = fixture.guest_window.hwnd();
    assert!(
        wait_until(|| client_size(guest_hwnd) == expected),
        "the guest client area settled at {:?}, expected {expected:?}",
        client_size(guest_hwnd)
    );

    // And it sits where the clip child is, to the pixel.
    let clip = window_rect(fixture.clip);
    let guest = window_rect(guest_hwnd);
    assert_eq!((guest.left, guest.top), (clip.left, clip.top));
}

#[test]
fn a_self_drawn_caption_is_offset_out_of_the_panel() {
    // The guest paints a strip at the top of its own client area, exactly as
    // Word does. Hosting must show none of it.
    let inset = guest::SYNTHETIC_GUEST_CAPTION;
    let mut fixture = Fixture::new((700, 480), inset);
    fixture.embed();
    let guest_hwnd = fixture.guest_window.hwnd();

    assert!(wait_until(|| {
        let clip = window_rect(fixture.clip);
        let guest = window_rect(guest_hwnd);
        guest.top == clip.top - inset && guest.bottom == clip.bottom
    }));

    let clip = window_rect(fixture.clip);
    let guest = window_rect(guest_hwnd);
    // Pushed up by exactly the strip's height...
    assert_eq!(guest.top, clip.top - inset);
    // ...and grown by the same amount, so the *visible* part is still a full
    // panel of document rather than a panel with a dead band at the bottom.
    assert_eq!(guest.bottom, clip.bottom);
    assert_eq!(
        client_size(guest_hwnd).1,
        fixture.layout.panel.height + inset
    );
}

/// **The cross-DPI regression.** A document embedded while the window is on a
/// 200% monitor, then the window dragged to a 100% one: dockview lays the panel
/// out again and `host_set_bounds` runs with the new scale factor.
///
/// Both halves of the layout have to come back through *that* factor. The panel
/// used to keep the caption inset it had already scaled at embed time, so after
/// the move the guest was offset by twice what it should be and the strip the
/// clip child exists to hide came back into view. Measured on the real windows,
/// because "the strip is visible again" is a claim about where Win32 actually
/// put the guest.
#[test]
fn a_panel_that_changes_dpi_re_derives_its_caption_inset() {
    let mut fixture = Fixture::new((640, 460), 0);
    fixture.embed();
    let guest_hwnd = fixture.guest_window.hwnd();
    let clip_hwnd = fixture.clip;

    // The registry entry the command layer keeps for this panel, with the
    // inset in the CSS pixels the UI sent.
    let panel = Panel {
        window: WindowId::from_hwnd(fixture.panel),
        clip: WindowId::from_hwnd(fixture.clip),
        guest: None,
        process: None,
        coalescer: Coalescer::default(),
        caption_inset_css: 14.0,
    };
    // One rectangle, in the CSS pixels dockview measures in. Moving a window
    // between monitors does not change it; the scale factor is what changes.
    let rect = CssRect {
        x: 60.0,
        y: 60.0,
        width: 320.0,
        height: 240.0,
    };

    // 200%, then 100%, then back — a monitor move has no preferred direction.
    for (scale, expected) in [(2.0, 28), (1.0, 14), (2.0, 28)] {
        fixture.apply(
            panel
                .layout_at(rect, scale)
                .expect("a layout at this scale factor"),
        );
        assert!(
            wait_until(|| { window_rect(guest_hwnd).top == window_rect(clip_hwnd).top - expected }),
            "at scale {scale} the guest sits {} px above the clip, expected {expected}",
            window_rect(clip_hwnd).top - window_rect(guest_hwnd).top
        );
    }
}

/// The stale-handle race the watchdog necessarily leaves open: a guest dies,
/// Windows recycles its `HWND` *value* for some unrelated window, and a
/// `WM_SETFOCUS` reaches the panel before the next sweep clears the handle.
///
/// `elsewhere` stands in for the window that inherited the number — live, ours,
/// and not a descendant of the panel, which is the only thing that tells it
/// apart from a real guest. A bare non-null check would hand it the keyboard.
#[test]
fn a_panel_does_not_forward_focus_to_a_window_that_is_not_its_guest() {
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let elsewhere = WindowId::from_hwnd(fixture.elsewhere);
    class::set_guest(fixture.panel, Some(elsewhere));

    focus::focus(WindowId::from_hwnd(fixture.panel)).expect("focusing the panel is accepted");
    pump_for(Duration::from_millis(300));

    assert_ne!(
        focus::focused_here(),
        Some(elsewhere),
        "the panel handed the keyboard to a window that is not its guest"
    );
}

/// A hosted document that goes away under the caret must not take the keyboard
/// with it.
///
/// Windows clears the focus when it destroys the focused window, and the panel
/// it was in is now empty — so without an explicit reclaim the user's only way
/// back to a keyboard is the mouse. This is what `commands::on_guest_gone` does
/// about it, and the same call covers the close/detach path where we destroy
/// the panel ourselves.
#[test]
fn a_guest_that_disappears_does_not_strand_the_keyboard() {
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_window = fixture.guest_window;
    let guest_thread = focus::owning_thread(guest_window);

    focus::focus(guest_window).expect("focusing the guest is accepted");
    assert!(
        wait_until(|| focus::focused_window_of(guest_thread) == Some(guest_window)),
        "the keyboard never reached the guest, so this test would prove nothing"
    );

    fixture.embedded = None; // the window is about to die; nothing to release
                             // SAFETY: a plain post to the guest's window, whose default handling
                             // is `DestroyWindow` — the application quitting from its own menu.
    unsafe {
        let _ = PostMessageW(Some(guest_window.hwnd()), WM_CLOSE, WPARAM(0), LPARAM(0));
    }
    assert!(
        wait_until(|| !exists(guest_window)),
        "the guest window survived WM_CLOSE"
    );
    settle();

    // Where the keyboard actually ends up here is Win32's business — measured,
    // it is the clip child, because `DestroyWindow` promotes a focused child's
    // focus to its parent. What matters is that it is not somewhere the user
    // can type, and in particular not already where the reclaim would put it.
    let elsewhere = WindowId::from_hwnd(fixture.elsewhere);
    assert_ne!(
        focus::focused_here(),
        Some(elsewhere),
        "the keyboard was already on the fallback; this test would prove nothing"
    );

    let dead_ends = [
        WindowId::from_hwnd(fixture.panel),
        WindowId::from_hwnd(fixture.clip),
    ];
    assert!(
        focus::reclaim_if_stranded(elsewhere, &dead_ends),
        "the reclaim declined a keyboard left on {:?}",
        focus::focused_here()
    );
    assert_eq!(
        focus::focused_here(),
        Some(elsewhere),
        "the keyboard was not handed back to a window the user can type in"
    );
}

/// The same, on the path where *we* tear everything down: `host_close` releases
/// the guest, destroys our two windows and then reaps the instance we launched.
///
/// The order is what this pins. Reclaiming before the reap finds the released
/// window alive on the desktop and — correctly — leaves the keyboard alone,
/// which strands it a moment later when the process dies. **Detach is not this
/// case**: there the document stays open on the desktop, and a window the user
/// can still see is entitled to keep the keyboard.
#[test]
fn closing_a_focused_panel_does_not_strand_the_keyboard() {
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_window = fixture.guest_window;
    let guest_thread = focus::owning_thread(guest_window);
    focus::focus(guest_window).expect("focusing the guest is accepted");
    assert!(
        wait_until(|| focus::focused_window_of(guest_thread) == Some(guest_window)),
        "the keyboard never reached the guest, so this test would prove nothing"
    );

    // Exactly what `host_close` does, in the same order.
    let dead_ends = [
        WindowId::from_hwnd(fixture.panel),
        WindowId::from_hwnd(fixture.clip),
    ];
    let embedded = fixture.embedded.take().expect("embedded");
    embed::release(&embedded).expect("releasing should succeed");
    class::destroy(WindowId::from_hwnd(fixture.clip));
    class::destroy(WindowId::from_hwnd(fixture.panel));
    fixture.clip = HWND(std::ptr::null_mut());
    fixture.panel = HWND(std::ptr::null_mut());
    fixture.process().reap();
    assert!(
        wait_until(|| !exists(guest_window)),
        "the guest window outlived its job object"
    );
    settle();

    // What `host_close` does with the keyboard, once the instance is gone.
    let elsewhere = WindowId::from_hwnd(fixture.elsewhere);
    focus::reclaim_if_stranded(elsewhere, &dead_ends);

    // Asserted as the outcome rather than as "the reclaim fired", because where
    // Windows *would* have put the focus is session-dependent: destroying an
    // active top-level window activates another of the same thread on an
    // interactive desktop, and leaves the focus null where nothing of ours was
    // active. Either way the keyboard must end up on a live window that is not
    // one of the two we just destroyed.
    let focused = focus::focused_here();
    assert!(
        focused.is_some_and(|window| exists(window) && !dead_ends.contains(&window)),
        "the keyboard was left on {focused:?} after the panel closed under it"
    );
}

#[test]
fn a_resize_storm_moves_the_guest_once_per_real_change() {
    let mut fixture = Fixture::new((800, 600), 0);
    fixture.embed();
    let guest_hwnd = fixture.guest_window.hwnd();

    // Two hundred frames of a drag that is not actually moving.
    let mut coalescer = Coalescer::default();
    let still = host_layout(fixture.layout.panel, 0);
    coalescer.next(still);
    let started = Instant::now();
    for _ in 0..200 {
        if let Some(plan) = coalescer.next(still) {
            fixture.apply(plan);
        }
    }
    assert_eq!(
        coalescer.applied, 1,
        "a still drag reached Win32 more than once"
    );
    assert_eq!(coalescer.skipped, 200);

    // Then two hundred frames that really move. The batching keeps this cheap
    // enough that a drag is not felt; the number is deliberately loose, because
    // this is a "not pathological" bound and not a benchmark.
    let started_moving = Instant::now();
    for step in 0..200 {
        let mut rect = fixture.layout.panel;
        rect.width = 600 + step;
        fixture.apply(host_layout(rect, 0));
    }
    let elapsed = started_moving.elapsed();
    assert!(
        elapsed < Duration::from_secs(2),
        "200 batched moves took {elapsed:?}"
    );

    let expected = (fixture.layout.guest.width, fixture.layout.guest.height);
    assert!(
        wait_until(|| client_size(guest_hwnd) == expected),
        "the guest ended a resize storm at {:?}, expected {expected:?} (storm took {:?})",
        client_size(guest_hwnd),
        started.elapsed()
    );
}

#[test]
fn focus_reaches_the_guest_without_attaching_input_queues() {
    // The claim in `focus.rs`, measured. No `AttachThreadInput` is called
    // anywhere in this crate — `SetFocus` across the process boundary works
    // because the parent/child relationship already attached the queues.
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_thread = focus::owning_thread(fixture.guest_window);
    assert_ne!(guest_thread, 0);
    assert_ne!(
        guest_thread,
        // SAFETY: a plain Win32 call with no arguments.
        unsafe { windows::Win32::System::Threading::GetCurrentThreadId() },
        "the guest must be owned by another thread or this proves nothing"
    );

    focus::focus(fixture.guest_window).expect("focusing the guest should be accepted");
    assert!(
        wait_until(|| focus::focused_window_of(guest_thread) == Some(fixture.guest_window)),
        "the guest thread reports focus on {:?}, not on the guest",
        focus::focused_window_of(guest_thread)
    );

    // And back. Taking focus off the guest has to work as reliably as putting
    // it there — this is the leg that decides whether a user can click from a
    // hosted document back into the file tree and type.
    focus::focus(WindowId::from_hwnd(fixture.elsewhere)).expect("focusing elsewhere is accepted");
    assert!(
        wait_until(|| focus::focused_window_of(guest_thread) != Some(fixture.guest_window)),
        "the guest kept the focus after it was handed back"
    );
}
#[test]
fn focusing_a_panel_hands_the_keyboard_to_the_guest() {
    // The panel window draws nothing a keyboard can reach, so focus that lands
    // on it belongs to the guest. This is the path a `Ctrl+N` panel chord, a
    // command, or a restored layout goes through.
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_thread = focus::owning_thread(fixture.guest_window);
    assert_eq!(
        class::guest_of(fixture.panel),
        Some(fixture.guest_window),
        "the panel window never learned which guest it is hosting"
    );

    focus::focus(WindowId::from_hwnd(fixture.elsewhere)).expect("park the focus elsewhere");
    assert!(
        wait_until(|| focus::focused_window_of(guest_thread) != Some(fixture.guest_window)),
        "the focus never left the guest, so this test would prove nothing"
    );

    focus::focus(WindowId::from_hwnd(fixture.panel)).expect("focusing the panel is accepted");
    assert!(
        wait_until(|| focus::focused_window_of(guest_thread) == Some(fixture.guest_window)),
        "focusing the panel did not put the keyboard in the guest"
    );
}

/// The graceful path: the application closes itself, as Word does from its own
/// File menu, and destroys its window while still running.
///
/// **`WM_PARENTNOTIFY` does not arrive**, which is the measurement the whole
/// watchdog design rests on.
#[test]
fn a_guest_that_quits_is_found_only_by_asking() {
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_window = fixture.guest_window;
    fixture.embedded = None; // the window is about to die; nothing to release
    watch_parent_notify(fixture.panel);

    // SAFETY: a plain post to the guest's window. `WM_CLOSE` is postable, and
    // its default handling is `DestroyWindow`.
    unsafe {
        let _ = PostMessageW(Some(guest_window.hwnd()), WM_CLOSE, WPARAM(0), LPARAM(0));
    }

    assert!(
        wait_until(|| !exists(guest_window)),
        "the guest window survived WM_CLOSE"
    );
    assert_eq!(
        parent_notify_count(),
        0,
        "WM_PARENTNOTIFY now arrives for a reparented cross-process guest - the \
         watchdog could be demoted to a backstop"
    );
}

/// The ugly path: the process goes away underneath us — a crash, or the job
/// object closing — and nothing gets a chance to be polite.
#[test]
fn a_guest_that_is_killed_is_found_only_by_asking() {
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_window = fixture.guest_window;
    fixture.embedded = None;
    watch_parent_notify(fixture.panel);
    fixture.process().reap();

    assert!(
        wait_until(|| !exists(guest_window)),
        "the guest window outlived its process"
    );
    assert_eq!(parent_notify_count(), 0, "see the test above");
    // The fact the Protocol's `poll` reports, and the only signal there is.
    assert!(!guest::window_exists(guest_window));
}

/// **Measured with the pointer actually moving.** A real click into a hosted
/// guest, to answer two questions at once: does the keyboard follow the click
/// (the thing a user feels), and does `WM_PARENTNOTIFY` reach the panel (the
/// thing click-to-focus *would* have been built on).
///
/// `#[ignore]`: it needs this process to be the foreground application and it
/// moves the real mouse pointer.
#[test]
#[ignore = "moves the real mouse pointer and needs the foreground; run with --ignored"]
fn a_real_click_into_a_hosted_guest() {
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_0, INPUT_MOUSE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE, MOUSEINPUT,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SetForegroundWindow, SM_CXSCREEN, SM_CYSCREEN,
    };

    let mut fixture = Fixture::new((700, 500), 0);
    fixture.embed();
    let guest_thread = focus::owning_thread(fixture.guest_window);
    focus::focus(WindowId::from_hwnd(fixture.elsewhere)).expect("park the focus");
    settle();
    watch_parent_notify(fixture.panel);

    // SAFETY: plain Win32 calls; `SendInput` is handed a correctly sized array.
    let foreground = unsafe { SetForegroundWindow(fixture.panel) }.as_bool();
    println!("real click: SetForegroundWindow -> {foreground}");
    settle();

    let clip = window_rect(fixture.clip);
    let (target_x, target_y) = ((clip.left + clip.right) / 2, (clip.top + clip.bottom) / 2);
    // SAFETY: as above.
    let (screen_w, screen_h) = unsafe {
        (
            GetSystemMetrics(SM_CXSCREEN).max(1),
            GetSystemMetrics(SM_CYSCREEN).max(1),
        )
    };
    let normalize = |value: i32, span: i32| (value * 65535 / span).clamp(0, 65535);
    let mouse = |flags| INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx: normalize(target_x, screen_w),
                dy: normalize(target_y, screen_h),
                mouseData: 0,
                dwFlags: MOUSEEVENTF_ABSOLUTE | flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let inputs = [
        mouse(MOUSEEVENTF_MOVE),
        mouse(MOUSEEVENTF_LEFTDOWN),
        mouse(MOUSEEVENTF_LEFTUP),
    ];
    // SAFETY: `inputs` is a live array of correctly sized `INPUT` values.
    let sent = unsafe { SendInput(&inputs, std::mem::size_of::<INPUT>() as i32) };
    println!("real click: SendInput sent {sent}/3 events at ({target_x},{target_y})");

    let focused =
        wait_until(|| focus::focused_window_of(guest_thread) == Some(fixture.guest_window));
    println!(
        "real click: guest focused = {focused}, WM_PARENTNOTIFY count = {}",
        parent_notify_count()
    );
    // The keyboard following the click is the part the user feels, and it is
    // the guest's own window procedure that does it, not ours.
    assert!(
        focused,
        "a real click did not put the keyboard in the guest"
    );
    assert_eq!(
        parent_notify_count(),
        0,
        "WM_PARENTNOTIFY now arrives for clicks into a reparented guest - a \
         `focused` event could be built on it after all"
    );
}

// ---- z-order: who is on top of a docked panel ---------------------------------

/// **The Win32 fact the whole z-order fix exists for, measured rather than
/// recited**: a child window is created at the *bottom* of its siblings' Z
/// order, not the top.
///
/// The folklore — and `CreateWindowEx`'s own documentation, which says a new
/// window is "added to the top of the Z order" without distinguishing children
/// from top-level windows — says the opposite, and believing it is how a docked
/// panel ended up underneath the webview: the panel is created long after
/// `WRY_WEBVIEW`, so *later* had to mean *above* for hosting to work, and it
/// does not.
///
/// Deliberately raw `CreateWindowExW` on the stock `STATIC` class rather than
/// our own [`class::create_panel`]: this is a claim about Windows, and running it
/// through the code that now compensates for it would test the compensation
/// instead. Plain children of a window of ours, so nothing else on the desktop
/// is involved.
#[test]
fn a_new_child_window_is_created_below_its_siblings() {
    use windows::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DestroyWindow, WINDOW_EX_STYLE, WS_CHILD, WS_VISIBLE,
    };

    let _serial = SERIAL.lock().unwrap_or_else(|err| err.into_inner());
    let root = class::create_panel(
        None,
        PhysicalRect {
            x: 60,
            y: 60,
            width: 400,
            height: 300,
        },
        PanelState::new("zorder-root".to_string()),
    )
    .expect("root window");
    // SAFETY: two stock `STATIC` children of a window this thread owns, both
    // destroyed below.
    let child = |name: windows::core::PCWSTR| unsafe {
        CreateWindowExW(
            WINDOW_EX_STYLE(0),
            windows::core::w!("STATIC"),
            name,
            WS_CHILD | WS_VISIBLE,
            0,
            0,
            200,
            120,
            Some(root),
            None,
            None,
            None,
        )
        .expect("a STATIC child")
    };
    let first = child(windows::core::w!("first"));
    let second = child(windows::core::w!("second"));

    let chain = zorder::children_top_first(root);
    assert_eq!(chain.len(), 2, "expected exactly the two children");
    assert_eq!(
        chain[0].window,
        WindowId::from_hwnd(first),
        "a child created *later* came out on top — Windows has changed its mind \
         about where CreateWindowEx puts a child, and `class::create_panel` no \
         longer needs to raise the panel it creates"
    );
    assert_eq!(zorder::depth_in_z_order(first), Some((0, 2)));
    assert_eq!(zorder::depth_in_z_order(second), Some((1, 2)));

    // SAFETY: our own windows, destroyed once.
    unsafe {
        let _ = DestroyWindow(second);
        let _ = DestroyWindow(first);
    }
    class::destroy(WindowId::from_hwnd(root));
}

/// **The bug, reproduced without a webview.**
///
/// The shell's arrangement is a top-level window whose entire client area is
/// already covered by somebody else's child — `WRY_WEBVIEW`, created when the
/// window was — and a panel docked into it long afterwards. Two things then go
/// wrong at once, and the test insists on both, because fixing only the first
/// makes a docked document *worse*: reachable by the mouse and still invisible.
///
/// * Given the fact above, the panel lands **underneath** that sibling, so a
///   click at its own centre never reaches it.
/// * `WRY_WEBVIEW` carries **no `WS_CLIPSIBLINGS`**, so even from in front the
///   panel is painted over. The stand-in has the bit stripped for exactly that
///   reason — with it left on, this test passes with half the fix in place.
///
/// Measured in the running shell first (`WORKBENCH_HOST_DEMO=word:…`, a real
/// Word docked, the demo's z-order report), then reproduced here so it stays
/// fixed. The stand-in for the webview is one of our own windows because what
/// matters is that it is a full-size sibling that already exists and does not
/// clip, not what it draws.
///
/// **`ChildWindowFromPointEx`, not `WindowFromPoint`** — see
/// [`super::zorder::child_hit`]: this test's windows are not the foreground, so
/// the desktop-wide probe would answer with whatever the machine happens to have
/// in front and the test would fail for reasons that are not this bug.
#[test]
fn a_panel_docked_into_a_window_that_already_has_a_webview_is_the_one_a_click_reaches() {
    use windows::Win32::UI::WindowsAndMessaging::{SetWindowLongPtrW, WS_CLIPSIBLINGS};

    let _serial = SERIAL.lock().unwrap_or_else(|err| err.into_inner());
    let root = class::create_panel(
        None,
        PhysicalRect {
            x: 80,
            y: 80,
            width: 800,
            height: 600,
        },
        PanelState::new("zorder-shell".to_string()),
    )
    .expect("the stand-in shell window");
    let (root_width, root_height) = client_size(root);
    // The webview: created with the window, covering all of it.
    let webview = class::create_panel(
        Some(root),
        PhysicalRect {
            x: 0,
            y: 0,
            width: root_width,
            height: root_height,
        },
        PanelState::new("webview stand-in".to_string()),
    )
    .expect("the stand-in webview");
    // ...and, like the real one, not clipping the siblings in front of it.
    // SAFETY: a plain style write on a window this thread created.
    unsafe {
        SetWindowLongPtrW(
            webview,
            GWL_STYLE,
            (style_of(webview) & !WS_CLIPSIBLINGS.0) as isize,
        );
    }
    assert_eq!(
        style_of(webview) & WS_CLIPSIBLINGS.0,
        0,
        "the stand-in still clips its siblings, so it does not stand in for WRY_WEBVIEW"
    );

    // The panel: created afterwards, over part of it — a docked document.
    let panel = class::create_panel(
        Some(root),
        PhysicalRect {
            x: 120,
            y: 90,
            width: 400,
            height: 300,
        },
        PanelState::new("panel".to_string()),
    )
    .expect("the panel window");
    let clip = class::create_clip(
        panel,
        PhysicalRect {
            x: 0,
            y: 0,
            width: 400,
            height: 300,
        },
    )
    .expect("the clip child");
    settle();

    let rect = zorder::screen_rect_of(panel);
    let point = (rect.x + rect.width / 2, rect.y + rect.height / 2);
    let hit = zorder::child_hit(root, point);
    let reached = hit.is_some_and(|window| {
        window == WindowId::from_hwnd(panel) || window == WindowId::from_hwnd(clip)
    });
    assert!(
        reached,
        "a click in the middle of the docked panel landed on {:?}, not on the \
         panel ({:?}) or its clip child ({:?}) — the panel is underneath the \
         webview, so the document in it is neither visible nor clickable",
        hit.map(|window| zorder::describe(window.hwnd())),
        WindowId::from_hwnd(panel),
        WindowId::from_hwnd(clip),
    );
    assert_eq!(
        zorder::depth_in_z_order(panel),
        Some((0, 2)),
        "the panel is not the topmost child of the window it was docked into"
    );
    assert_eq!(
        zorder::depth_in_z_order(webview),
        Some((1, 2)),
        "the webview stand-in was expected to end up below the panel"
    );
    assert_ne!(
        style_of(webview) & WS_CLIPSIBLINGS.0,
        0,
        "the sibling still paints across whatever is in front of it, so the panel is \
         clickable and invisible — worse than being behind it"
    );

    class::destroy(WindowId::from_hwnd(clip));
    class::destroy(WindowId::from_hwnd(panel));
    class::destroy(WindowId::from_hwnd(webview));
    class::destroy(WindowId::from_hwnd(root));
}

// ---- counting WM_PARENTNOTIFY ------------------------------------------------
//
// Subclassing the panel window is the only way to observe a message the
// production code deliberately no longer handles. Test-only, and sound because
// window tests are serialised: one subclass is live at a time.

static PARENT_NOTIFY_COUNT: AtomicU32 = AtomicU32::new(0);
static ORIGINAL_WND_PROC: AtomicIsize = AtomicIsize::new(0);

fn watch_parent_notify(panel: HWND) {
    PARENT_NOTIFY_COUNT.store(0, Ordering::SeqCst);
    // SAFETY: replacing a window procedure on a window this thread owns, with
    // one that chains to the original through `CallWindowProcW`.
    let previous =
        unsafe { SetWindowLongPtrW(panel, GWLP_WNDPROC, counting_wnd_proc as *const () as isize) };
    ORIGINAL_WND_PROC.store(previous, Ordering::SeqCst);
}

fn parent_notify_count() -> u32 {
    PARENT_NOTIFY_COUNT.load(Ordering::SeqCst)
}

type RawWndProc = unsafe extern "system" fn(HWND, u32, WPARAM, LPARAM) -> LRESULT;

unsafe extern "system" fn counting_wnd_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if message == WM_PARENTNOTIFY {
        PARENT_NOTIFY_COUNT.fetch_add(1, Ordering::SeqCst);
    }
    let original = ORIGINAL_WND_PROC.load(Ordering::SeqCst);
    // SAFETY: `original` is whatever `SetWindowLongPtrW(GWLP_WNDPROC)` returned
    // for this window, which is exactly what `CallWindowProcW` expects back.
    unsafe {
        CallWindowProcW(
            Some(std::mem::transmute::<isize, RawWndProc>(original)),
            hwnd,
            message,
            wparam,
            lparam,
        )
    }
}

// ---- the close paths, and the thread they run on ------------------------------

/// One registry with one panel in it, holding the fixture's windows and — if
/// asked — the instance it launched.
///
/// The fixture stops owning whatever goes in here: the code under test is what
/// destroys these windows and ends this process, and a second teardown from
/// `Fixture::drop` would be destroying handles that already went.
fn registry_with(
    fixture: &mut Fixture,
    host_id: &str,
    take_process: bool,
) -> (HostRegistry, WindowId, WindowId) {
    let (panel_window, clip_window) = fixture.disown_windows();
    let process = take_process.then(|| fixture.take_process());
    let registry = HostRegistry::new();
    registry
        .panels
        .lock()
        .unwrap_or_else(|err| err.into_inner())
        .insert(
            host_id.to_string(),
            Panel {
                window: panel_window,
                clip: clip_window,
                guest: fixture.embedded.take(),
                process,
                coalescer: Coalescer::default(),
                caption_inset_css: 0.0,
            },
        );
    (registry, panel_window, clip_window)
}

/// **The close-ack watchdog's teardown, and which thread its Win32 calls run
/// on.**
///
/// When the UI never acknowledges a close prompt, `lib.rs` closes the window
/// anyway — from a thread it spawned for the purpose, in exactly the situation
/// the watchdog exists for. What that thread must not do is tear the hosted
/// panels down itself: handing a guest back is `SetWindowLongPtrW` + `SetParent`
/// on another process's window, our two windows go with `DestroyWindow`, and
/// Win32 does not let a thread destroy a window that another thread created. It
/// used to do exactly that, in the one scenario where the main thread is most
/// likely to be busy.
///
/// The seam is stood up by hand rather than through a Tauri app: **this** thread
/// created the windows and drains the queue, another plays the watchdog. What is
/// asserted is where the teardown's Win32 calls ran — and then that the teardown
/// really happened, so routing it politely into a no-op cannot pass.
#[test]
fn a_teardown_asked_for_off_thread_runs_its_win32_where_the_windows_live() {
    let mut fixture = Fixture::new((640, 480), 0);
    let guest_hwnd = fixture.guest_window.hwnd();
    let original_style = style_of(guest_hwnd);
    fixture.embed();
    assert_ne!(
        style_of(guest_hwnd),
        original_style,
        "nothing was embedded, so a teardown would prove nothing"
    );

    let owner = thread::current().id();
    // No process: this test is about the window calls, and the fixture keeps
    // the instance so it is reaped exactly once, at the end.
    let (registry, panel_window, clip_window) = registry_with(&mut fixture, "watchdog", false);
    let registry = Arc::new(registry);

    let ran_on: Arc<Mutex<Option<ThreadId>>> = Arc::new(Mutex::new(None));
    let (tx, rx) = mpsc::channel::<Box<dyn FnOnce() + Send + 'static>>();
    let watchdog = {
        let registry = Arc::clone(&registry);
        let ran_on = Arc::clone(&ran_on);
        thread::spawn(move || {
            let asked_from = thread::current().id();
            let outcome = main_thread::dispatch_within(
                move |posted| {
                    tx.send(posted).map_err(|_| {
                        HostError::new(HostErrorCode::MainThread, "nobody is draining the queue")
                    })
                },
                SETTLE_TIMEOUT * 3,
                move || {
                    *ran_on.lock().unwrap_or_else(|err| err.into_inner()) =
                        Some(thread::current().id());
                    commands::tear_down_all(&registry);
                    Ok(())
                },
            );
            (asked_from, outcome)
        })
    };

    // This thread owns the windows, so this thread runs the work — which is
    // what a message loop servicing `run_on_main_thread` amounts to.
    let deadline = Instant::now() + SETTLE_TIMEOUT;
    let mut serviced = false;
    while Instant::now() < deadline && !serviced {
        match rx.try_recv() {
            Ok(work) => {
                work();
                serviced = true;
            }
            Err(_) => {
                pump_once();
                thread::sleep(Duration::from_millis(5));
            }
        }
    }
    assert!(
        serviced,
        "the teardown was never handed to the thread that owns the windows"
    );

    let (asked_from, outcome) = watchdog.join().expect("the watchdog thread finished");
    outcome.expect("the teardown completed");
    assert_ne!(
        asked_from, owner,
        "the stand-in watchdog ran on the owning thread, so this proves nothing"
    );
    assert_eq!(
        *ran_on.lock().unwrap_or_else(|err| err.into_inner()),
        Some(owner),
        "the teardown's Win32 calls ran on {asked_from:?}, not on the thread that owns \
         the windows"
    );

    // And it was a real teardown, not a routed no-op.
    settle();
    assert!(
        !exists(panel_window),
        "the panel window survived the teardown"
    );
    assert!(!exists(clip_window), "the clip child survived the teardown");
    assert_eq!(
        parent_of(guest_hwnd),
        None,
        "the guest was not handed back to the desktop"
    );
    assert_eq!(
        style_of(guest_hwnd),
        original_style,
        "the guest was left wearing the child styles it was hosted with"
    );
}

/// **Closing a panel must not wait for the instance to die.**
///
/// `host_close` runs on the main thread — the windows are the main thread's —
/// and it used to drop the guest process there. That `Drop` reaped, and reaping
/// is a kill followed by a poll until the process is really gone, bounded at two
/// seconds. So closing one docked document froze the whole window for as long as
/// the instance took to go: nothing at all for the synthetic guest, and the far
/// end of that bound for a real Office instance in the middle of a save.
///
/// Made deterministic by taking the guest's **job object** away. Closing the job
/// is what kills it, so a guest without one survives its own reap and the wait
/// runs to the full bound — a slow-dying instance, without having to find an
/// Office that is genuinely slow to die. The guard puts the job back at the end,
/// so nothing is left on the desktop.
#[test]
fn closing_a_panel_does_not_wait_for_a_slow_dying_guest() {
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_window = fixture.guest_window;
    let job = fixture.process().detach_job_for_test();

    // **The control**, taken first and on the same instance: the wait this used
    // to do inline is real. Without it, a fast close below would be
    // indistinguishable from a guest that simply died at once.
    let started = Instant::now();
    fixture.process().reap_within(Duration::from_millis(400));
    let inline = started.elapsed();
    println!("close: the disposal `host_close` used to do inline cost {inline:?}");
    assert!(
        inline >= Duration::from_millis(350),
        "the control returned in {inline:?}, so this guest is dying after all and the \
         measurement below would prove nothing"
    );

    let (registry, panel_window, clip_window) = registry_with(&mut fixture, "slow", true);

    // The production body of `host_close`, disposing of the instance exactly as
    // the command does.
    let started = Instant::now();
    commands::close_panel(&registry, "slow", |process, _dead_ends| {
        if let Some(process) = process {
            reaper::reap(process, || {});
        }
    })
    .expect("the panel closes");
    let elapsed = started.elapsed();
    println!(
        "close: `host_close`'s body returned in {elapsed:?} with a guest that cannot be killed"
    );
    assert!(
        elapsed < Duration::from_millis(250),
        "closing a panel held its thread for {elapsed:?} while an instance died — the \
         control says that wait is worth {inline:?}"
    );
    assert!(!exists(panel_window), "the panel window survived the close");
    assert!(!exists(clip_window), "the clip child survived the close");

    // Nothing leaked: the instance is still ours to end, and ending it works.
    drop(job);
    assert!(
        wait_until(|| !exists(guest_window)),
        "the slow-dying guest outlived the test"
    );
}

/// The other half of moving the wait off the main thread: the **kill** did not
/// move with it.
///
/// A normal guest, with its job object where it belongs. Closing the panel must
/// still end the instance we launched — that is the whole difference between
/// `close` and `detach` — and it must still empty the registry.
#[test]
fn closing_a_panel_still_ends_the_instance_it_launched() {
    let mut fixture = Fixture::new((640, 480), 0);
    fixture.embed();
    let guest_window = fixture.guest_window;
    let (registry, panel_window, clip_window) = registry_with(&mut fixture, "owned", true);

    commands::close_panel(&registry, "owned", |process, _dead_ends| {
        if let Some(process) = process {
            reaper::reap(process, || {});
        }
    })
    .expect("the panel closes");

    assert!(
        wait_until(|| !exists(guest_window)),
        "the guest outlived a close that was supposed to end it"
    );
    assert!(!exists(panel_window), "the panel window survived the close");
    assert!(!exists(clip_window), "the clip child survived the close");
    assert!(
        registry
            .panels
            .lock()
            .unwrap_or_else(|err| err.into_inner())
            .is_empty(),
        "the registry still holds the panel that was closed"
    );
}

/// **A reaper thread that never starts must not take the keyboard with it.**
///
/// `reap` moves the wait off the main thread, and the step that depends on the
/// wait — handing the keyboard back to the webview — goes with it. Thread and
/// handle exhaustion is real pressure for a shell juggling several hosted Office
/// instances, and `thread::Builder::spawn` drops the closure it could not run:
/// the shape this guards against is a failed spawn quietly swallowing the
/// continuation, leaving a user with a closed panel and a keyboard that never
/// came back, and nothing but a log line to say so.
///
/// The refusal is injected rather than provoked — starving the test runner of
/// threads to observe this would be a worse test than naming the case.
#[test]
fn a_reaper_thread_that_never_starts_still_hands_the_keyboard_back() {
    let mut fixture = Fixture::new((640, 480), 0);
    let guest_window = fixture.guest_window;
    let process = fixture.take_process();

    let reclaimed = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&reclaimed);
    reaper::reap_with(
        process,
        Box::new(move || flag.store(true, Ordering::SeqCst)),
        |_body| Err(io::Error::other("no thread for you")),
    );

    assert!(
        reclaimed.load(Ordering::SeqCst),
        "the continuation was dropped with the thread that could not start — for `host_close` \
         that is the keyboard reclaim, so the caret would be stranded with no way back"
    );
    // And the instance still ends: the kill never depended on the thread.
    assert!(
        wait_until(|| !exists(guest_window)),
        "the guest outlived a close whose reaper thread failed to start"
    );
}

/// The other half of that fallback: **it is bounded, and it is short.**
///
/// The inline wait runs on the caller, which for `host_close` is the main
/// thread — the thread this whole module exists to keep out of a two-second
/// poll. So the fallback answers to its own much shorter number. Made
/// deterministic the same way the timing test above is: a guest with its job
/// object taken away cannot be killed, so the wait runs to the full bound
/// instead of ending the moment the instance does.
#[test]
fn the_fallback_wait_is_bounded_far_below_the_reapers_own() {
    let mut fixture = Fixture::new((640, 480), 0);
    let guest_window = fixture.guest_window;
    let job = fixture.process().detach_job_for_test();
    let process = fixture.take_process();

    let reclaimed = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&reclaimed);
    let started = Instant::now();
    reaper::reap_with(
        process,
        Box::new(move || flag.store(true, Ordering::SeqCst)),
        |_body| Err(io::Error::other("no thread for you")),
    );
    let elapsed = started.elapsed();
    println!("reaper: the inline fallback held its caller for {elapsed:?}");

    assert!(
        reclaimed.load(Ordering::SeqCst),
        "the continuation did not run after the fallback gave up on the instance"
    );
    assert!(
        elapsed >= Duration::from_millis(100),
        "the fallback returned in {elapsed:?} without waiting for an instance that cannot die, \
         so it would hand the keyboard back while the window is still on the desktop"
    );
    assert!(
        elapsed < Duration::from_secs(1),
        "the fallback held its caller for {elapsed:?} — that is the reaper's own two-second \
         bound leaking back onto the main thread, which is the freeze this module removed"
    );

    // Nothing leaked: the instance is still ours to end, and ending it works.
    drop(job);
    assert!(
        wait_until(|| !exists(guest_window)),
        "the slow-dying guest outlived the test"
    );
}

#[test]
fn teardown_gives_the_window_back_and_leaves_nothing_behind() {
    let mut fixture = Fixture::new((720, 540), 0);
    let guest_hwnd = fixture.guest_window.hwnd();
    let original_style = style_of(guest_hwnd);
    let original_rect = window_rect(guest_hwnd);
    fixture.embed();
    assert_ne!(style_of(guest_hwnd), original_style);

    let embedded = fixture.embedded.take().expect("embedded");
    embed::release(&embedded).expect("releasing should succeed");
    settle();

    assert_eq!(
        style_of(guest_hwnd),
        original_style,
        "the guest was left without a caption or a frame — unmovable and unclosable"
    );
    assert_eq!(parent_of(guest_hwnd), None, "the guest is still our child");
    let restored = window_rect(guest_hwnd);
    assert_eq!(
        (restored.left, restored.top),
        (original_rect.left, original_rect.top),
        "the guest did not go back where it was"
    );

    // Our own windows go, and nothing of ours survives them.
    let panel = WindowId::from_hwnd(fixture.panel);
    let clip = WindowId::from_hwnd(fixture.clip);
    class::destroy(clip);
    class::destroy(panel);
    settle();
    assert!(!exists(panel), "the panel window leaked");
    assert!(!exists(clip), "the clip child leaked");
    fixture.panel = HWND(std::ptr::null_mut());
    fixture.clip = HWND(std::ptr::null_mut());

    // And the process really is reapable, which is the job object's whole job.
    let guest_window = fixture.guest_window;
    fixture.process().reap();
    assert!(
        !fixture.process().is_running(),
        "the guest survived its job"
    );
    assert!(!guest::window_exists(guest_window));
}

/// **The measurement that decides a product default, now taken after the fix.**
///
/// Windows attaches the input queues of a thread that owns a parent window and
/// a thread that owns its child, so a guest that stops pumping used to make
/// every resize frame cost ~1 s. The owner ruled that native hosting only
/// becomes the default once that is proven contained. This test is that proof:
/// it wedges a guest and then times **the production path** (`layout::apply`,
/// which hands guest geometry to `host::mover`) against a direct
/// `SetWindowPos` from this thread, which is what the code used to do.
///
/// The direct call is kept deliberately. Without it the containment number
/// would be indistinguishable from "the guest was not actually hung", and the
/// whole finding would rest on the fixture having done its job.
///
/// `#[ignore]` because the last part needs this process to be the foreground
/// application, and because it deliberately spends ten seconds inside a hang.
/// Run it with `cargo test -- --ignored --nocapture` on a real desktop; the
/// findings are printed, and the PR body records what they were.
#[test]
#[ignore = "needs an interactive desktop session and spends ~10s hung; run with --ignored"]
fn hang_isolation_measurement() {
    const HANG: Duration = Duration::from_secs(15);
    let mut fixture = Fixture::new((800, 600), 0);
    fixture.embed();
    let guest_hwnd = fixture.guest_window.hwnd();

    // Wedge the guest's window procedure.
    // SAFETY: a plain post to the guest's window.
    unsafe {
        let _ = PostMessageW(
            Some(guest_hwnd),
            guest::SYNTHETIC_GUEST_HANG,
            WPARAM(HANG.as_millis() as usize),
            LPARAM(0),
        );
    }
    pump_for(Duration::from_millis(400));

    // 1. A *sent* message to a hung window blocks for the whole timeout. This
    //    is why nothing in the host sends one, and it is the cost an accidental
    //    `SendMessage` anywhere in this code would carry.
    let started = Instant::now();
    // SAFETY: a plain Win32 call; the timeout is what bounds it.
    let answered = unsafe {
        SendMessageTimeoutW(
            guest_hwnd,
            WM_NULL,
            WPARAM(0),
            LPARAM(0),
            SMTO_ABORTIFHUNG,
            200,
            None,
        )
    };
    println!(
        "hang: SendMessageTimeout returned {} after {:?}",
        answered.0,
        started.elapsed()
    );

    // 2. Our own message loop keeps running: posted messages are dispatched and
    //    the panel still paints.
    let mut dispatched = 0;
    let mut painted = false;
    // SAFETY: plain posts to our own window and a plain invalidate.
    unsafe {
        for _ in 0..50 {
            let _ = PostMessageW(Some(fixture.panel), WM_APP, WPARAM(0), LPARAM(0));
        }
        let _ = InvalidateRect(Some(fixture.panel), None, true);
    }
    let started = Instant::now();
    while started.elapsed() < Duration::from_secs(1) {
        let mut message = MSG::default();
        // SAFETY: this thread's own queue.
        unsafe {
            while PeekMessageW(&mut message, None, 0, 0, PM_REMOVE).as_bool() {
                match message.message {
                    WM_APP => dispatched += 1,
                    WM_PAINT => painted = true,
                    _ => {}
                }
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    println!(
        "hang: {dispatched}/50 posted messages dispatched, panel repainted = {painted}, \
         while the guest was hung"
    );
    assert!(
        dispatched >= 50,
        "the host's own message loop stalled behind the guest"
    );

    // 3. What Windows itself thinks. `IsHungAppWindow` is the judgement behind
    //    a ghost window and "(Not Responding)" in the title bar.
    let hung_guest = wait_until_hung(guest_hwnd);
    // SAFETY: a plain Win32 call.
    let hung_panel = unsafe { IsHungAppWindow(fixture.panel).as_bool() };
    println!("hang: IsHungAppWindow(guest)={hung_guest} IsHungAppWindow(panel)={hung_panel}");
    assert!(
        !hung_panel,
        "the host window was judged hung because its guest was"
    );

    // 4. **The finding that decides the product default.** Where the cost of a
    //    resize lands while the guest is hung, timed through the code that
    //    actually runs on a drag frame.
    let mut ours = Duration::ZERO;
    let mut with_guest = Duration::ZERO;
    let mut last = fixture.layout;
    for step in 0..10 {
        let mut rect = fixture.layout.panel;
        rect.width = 700 + step;
        let plan = host_layout(rect, 0);

        let started = Instant::now();
        layout::apply(fixture.panel, fixture.clip, None, &plan).expect("our own two windows");
        ours += started.elapsed();

        let started = Instant::now();
        layout::apply(
            fixture.panel,
            fixture.clip,
            Some(fixture.guest_window.hwnd()),
            &plan,
        )
        .expect("with the guest");
        with_guest += started.elapsed();
        fixture.layout = plan;
        last = plan;
    }
    println!("hang: 10 moves of our own two windows: {ours:?}");
    println!("hang: 10 moves including the hung guest, contained: {with_guest:?}");
    assert!(
        ours < Duration::from_millis(200),
        "the panel and its clip child cannot be moved while the guest is hung"
    );
    // **The containment, asserted.** Before `host::mover`, this line took ~10 s
    // for these ten frames because `layout::apply` issued the guest's
    // `SetWindowPos` from this thread, whose input queue the guest is attached
    // to. It is now handed to a worker that owns no window, and the frame does
    // not wait for it.
    assert!(
        with_guest < Duration::from_millis(200),
        "a hung guest still costs {with_guest:?} for ten resize frames — the \
         geometry worker is not containing it"
    );

    // 5. **The control.** The same move issued straight from this thread, which
    //    is what the code did before. It must still be slow, or the numbers
    //    above would only be saying that the fixture failed to hang.
    let started = Instant::now();
    // SAFETY: a plain Win32 call on a live window.
    let direct = unsafe {
        windows::Win32::UI::WindowsAndMessaging::SetWindowPos(
            fixture.guest_window.hwnd(),
            None,
            last.guest.x,
            last.guest.y,
            last.guest.width - 3,
            last.guest.height,
            layout::GUEST,
        )
    };
    let attached = started.elapsed();
    println!("hang: one direct move from the main thread (the old path): {attached:?}");
    assert!(
        direct.is_ok(),
        "the control move was refused, so it measured nothing"
    );
    assert!(
        attached > Duration::from_millis(400),
        "a direct move on a hung guest returned in {attached:?} — either the \
         guest is not hung (so the containment number proves nothing) or \
         SWP_ASYNCWINDOWPOS now posts across an attached queue and host::mover \
         is no longer needed"
    );

    // 6. **Containment must not mean dropping the move.** A worker that
    //    swallowed every frame would post the same numbers as one that works,
    //    so wait the hang out and check that a contained frame really lands.
    pump_for(HANG);
    let mut rect = last.panel;
    rect.width = 654;
    let plan = host_layout(rect, 0);
    layout::apply(
        fixture.panel,
        fixture.clip,
        Some(fixture.guest_window.hwnd()),
        &plan,
    )
    .expect("a frame after the hang");
    fixture.layout = plan;
    let guest_hwnd = fixture.guest_window.hwnd();
    let expected = (plan.guest.width, plan.guest.height);
    let landed = wait_until(|| client_size(guest_hwnd) == expected);
    println!("hang: a contained frame issued after the hang landed = {landed}");
    assert!(
        landed,
        "the guest settled at {:?}, not the {expected:?} the contained frame \
         asked for — the worker is dropping moves, not deferring them",
        client_size(guest_hwnd)
    );
}
fn wait_until_hung(window: HWND) -> bool {
    let deadline = Instant::now() + Duration::from_secs(7);
    while Instant::now() < deadline {
        pump_once();
        // SAFETY: a plain Win32 call.
        if unsafe { IsHungAppWindow(window).as_bool() } {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}
/// Two pieces of folklore about `DeferWindowPos`, both measured here because
/// getting either wrong is a silent failure: the batch simply refuses, and a
/// panel stops following its splitter.
///
/// * `SWP_ASYNCWINDOWPOS` **is rejected** — it is a `SetWindowPos` flag, and
///   `DeferWindowPos` fails with `ERROR_INVALID_PARAMETER` when it is present.
///   The documentation does not say so. This is why `layout.rs` moves the guest
///   with its own asynchronous `SetWindowPos` instead of putting it in the
///   batch: asynchrony is exactly what a cross-process window needs.
/// * Mixing a **parent and its child** in one batch is fine, contrary to the
///   usual advice. Worth knowing, because it is what lets the panel and its
///   clip child move as one transaction.
#[test]
fn deferwindowpos_rejects_the_asynchronous_flag() {
    use windows::Win32::UI::WindowsAndMessaging::{
        BeginDeferWindowPos, DeferWindowPos, EndDeferWindowPos, SET_WINDOW_POS_FLAGS,
    };

    let _serial = SERIAL.lock().unwrap_or_else(|err| err.into_inner());
    let rect = PhysicalRect {
        x: 0,
        y: 0,
        width: 300,
        height: 200,
    };
    let root = class::create_panel(
        None,
        PhysicalRect {
            x: 40,
            y: 40,
            width: 900,
            height: 700,
        },
        PanelState::new("root".to_string()),
    )
    .expect("root window");
    let child = class::create_panel(Some(root), rect, PanelState::new("child".to_string()))
        .expect("child window");
    let clip = class::create_clip(child, rect).expect("clip window");

    let batch = |windows: &[HWND], flags: SET_WINDOW_POS_FLAGS| -> Result<(), String> {
        // SAFETY: the HDWP is threaded through and consumed once; a failure
        // abandons it, which is what the API asks for.
        unsafe {
            let mut hdwp =
                BeginDeferWindowPos(windows.len() as i32).map_err(|err| err.to_string())?;
            for window in windows {
                hdwp = DeferWindowPos(hdwp, *window, None, 0, 0, 250, 150, flags)
                    .map_err(|err| err.to_string())?;
            }
            EndDeferWindowPos(hdwp).map_err(|err| err.to_string())
        }
    };

    assert!(
        batch(&[child], layout::GUEST).is_err(),
        "SWP_ASYNCWINDOWPOS was accepted by DeferWindowPos; layout.rs can put          the guest back in the batch"
    );
    assert!(
        batch(&[child], layout::OURS).is_ok(),
        "the plain batch failed"
    );
    assert!(
        batch(&[child, clip], layout::OURS).is_ok(),
        "a parent and its child cannot share a batch after all"
    );

    class::destroy(WindowId::from_hwnd(clip));
    class::destroy(WindowId::from_hwnd(child));
    class::destroy(WindowId::from_hwnd(root));
}
