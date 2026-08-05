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

use std::sync::atomic::{AtomicIsize, AtomicU32, Ordering};
use std::sync::{Mutex, MutexGuard};
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
use super::{focus, Panel, WindowId};

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
    guest_process: GuestProcess,
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
        let serial = SERIAL.lock().unwrap_or_else(|err| err.into_inner());
        let exe = guest::synthetic_guest_exe().expect("the synthetic guest binary should be built");
        let mut launched = guest::launch(&exe, &[], guest::SYNTHETIC_GUEST_CLASS, LAUNCH_TIMEOUT)
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
            guest_process: launched.process,
            embedded: None,
            layout: plan,
            handshake,
            _serial: serial,
        }
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
        self.guest_process.reap();
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
    fixture.guest_process.reap();
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
    fixture.guest_process.reap();

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
    fixture.guest_process.reap();
    assert!(
        !fixture.guest_process.is_running(),
        "the guest survived its job"
    );
    assert!(!guest::window_exists(guest_window));
}

/// **The measurement that decides a product default.**
///
/// Windows attaches the input queues of a thread that owns a parent window and
/// a thread that owns its child, so a guest that stops pumping can, in
/// principle, take the host's input down with it. The owner has ruled that
/// native hosting only becomes the default once that is proven contained; this
/// PR does not try to contain it, it measures it.
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
    //    resize lands while the guest is hung, with the two halves of
    //    `layout::apply` timed apart.
    let mut ours = Duration::ZERO;
    let mut with_guest = Duration::ZERO;
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
    }
    println!("hang: 10 moves of our own two windows: {ours:?}");
    println!("hang: 10 moves including the hung guest: {with_guest:?}");
    assert!(
        ours < Duration::from_millis(200),
        "the panel and its clip child cannot be moved while the guest is hung"
    );
    // Locked in deliberately. `SWP_ASYNCWINDOWPOS` is documented to post the
    // request "if the calling thread and the thread that owns the window are
    // attached to different input queues" — and making the guest our child
    // attached them, so it does not post and the call waits. If this assertion
    // ever fails because the number got *small*, the containment work below is
    // no longer needed and this test should say so instead.
    assert!(
        with_guest > Duration::from_secs(1),
        "moving a hung guest is no longer slow — SWP_ASYNCWINDOWPOS now posts \
         across an attached queue, and host::layout can stop working around it"
    );

    // 5. **The containment path, measured.** A thread that owns none of the
    //    windows in the parent chain is attached to no input queue, so the same
    //    asynchronous call from there should post rather than wait. This is not
    //    fixed in this PR — it is the evidence for how it will be.
    let guest = fixture.guest_window;
    let plan = fixture.layout;
    let probe = std::thread::spawn(move || {
        let started = Instant::now();
        // SAFETY: a plain Win32 call on a live window, from a thread that owns
        // no window in its chain.
        let accepted = unsafe {
            windows::Win32::UI::WindowsAndMessaging::SetWindowPos(
                guest.hwnd(),
                None,
                plan.guest.x,
                plan.guest.y,
                plan.guest.width - 3,
                plan.guest.height,
                layout::GUEST,
            )
        }
        .is_ok();
        (started.elapsed(), accepted)
    });
    let (unattached, accepted) = probe.join().unwrap_or((Duration::MAX, false));
    println!("hang: the same move from an unattached worker thread: {unattached:?} (accepted: {accepted})");
    assert!(accepted, "the unattached move was refused");
    assert!(
        unattached < Duration::from_millis(100),
        "moving a hung guest from an unattached thread blocks too — the worker \
         thread plan does not contain this and something else must"
    );

    // Wait out the hang so the fixture tears down against a live guest.
    pump_for(HANG);
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
