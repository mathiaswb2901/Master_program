//! **Who is on top of a docked panel**, and the two different questions that
//! phrase hides.
//!
//! A panel window is created as a child of the Tauri window, which already has
//! the WebView2 widget tree in it. "Is the guest on top?" is therefore two
//! questions with two different answers, and asking only one of them is how a
//! z-order bug gets half-diagnosed:
//!
//! * **Hit-testing** — where would a click at this pixel go? [`hit_target`],
//!   i.e. `WindowFromPoint`, which is the same walk the mouse itself takes and
//!   therefore the honest answer for input. It is decided by Z order alone, and
//!   [`raise_to_top`] is what sets it right.
//! * **Painting** — what is actually on the screen at this pixel? [`pixel_at`],
//!   read from the screen device context after the compositor has had its say.
//!   Z order is only half of *this* one: a window without `WS_CLIPSIBLINGS`
//!   paints across the windows in front of it, and [`ensure_siblings_clip`] is
//!   what sets that right.
//!
//! **They really do disagree here.** `WRY_WEBVIEW` sits above every panel on
//! creation (children are created at the *bottom* of the sibling order) *and*
//! carries no `WS_CLIPSIBLINGS`. Fixing only the first leaves a docked Word
//! clickable and invisible, which is worse than leaving it buried — so the two
//! belong together, and [`report`] prints both answers side by side with the
//! sibling chain and the style bits that explain them.
//!
//! Nothing here is on a hot path: it is diagnostics plus two small window calls
//! made once per docked panel. It exists because "the webview covers the docked
//! panel" was asserted from a single `WindowFromPoint` call with no control
//! beside it — true, as it turned out, but for one and a half of the two reasons
//! that were actually in play.

use windows::Win32::Foundation::{HWND, POINT};
use windows::Win32::Graphics::Gdi::{GetDC, GetPixel, ReleaseDC, ScreenToClient, CLR_INVALID};
use windows::Win32::UI::WindowsAndMessaging::{
    ChildWindowFromPointEx, GetClassNameW, GetParent, GetWindow, GetWindowLongPtrW, GetWindowRect,
    IsChild, IsWindowVisible, SetWindowLongPtrW, SetWindowPos, WindowFromPoint, CWP_SKIPDISABLED,
    CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT, GWL_EXSTYLE, GWL_STYLE, GW_CHILD, GW_HWNDNEXT,
    HWND_TOP, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER,
    WS_CLIPSIBLINGS, WS_EX_NOREDIRECTIONBITMAP,
};

use super::geometry::PhysicalRect;
use super::{HostError, WindowId};

/// How far a sibling walk is allowed to go before it gives up.
///
/// The chain cannot loop — `GW_HWNDNEXT` is a linked list the window manager
/// owns — but this runs inside a log line, and a log line is never worth an
/// unbounded loop over somebody else's data structure.
const MAX_SIBLINGS: usize = 64;

/// One window in a sibling chain, described well enough to recognise in a log.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) struct Sibling {
    pub window: WindowId,
    pub class: String,
    pub visible: bool,
    pub rect: PhysicalRect,
    /// The style, because `WS_CLIPSIBLINGS` is what decides whether a sibling
    /// below this one in the Z order is allowed to be painted over.
    pub style: u32,
    /// The extended style, because `WS_EX_NOREDIRECTIONBITMAP` is the one bit
    /// that decides whether Z order says anything about what is *painted*.
    pub ex_style: u32,
}

/// Put `window` at the top of its siblings' Z order, without moving, resizing
/// or activating it.
///
/// **Not a belt-and-braces call.** A `WS_CHILD` is created at the *bottom* of
/// its siblings — the opposite of what `CreateWindowEx`'s documentation implies
/// and of what the folklore says, measured in
/// `hosting_tests::a_new_child_window_is_created_below_its_siblings` — so
/// without this a panel docked into the shell is behind the entire webview and
/// no click ever reaches the document in it.
///
/// The newest panel ends up above the others, which is harmless: dockview lays
/// panels out disjointly, so two of them never contend for a pixel. `SWP_NOMOVE
/// | SWP_NOSIZE` keeps geometry entirely in [`super::layout`]'s hands, and
/// `SWP_NOACTIVATE` matters for the same reason [`super::class::set_visible`]
/// uses `SW_SHOWNA`: a panel that activated itself on the way up would take the
/// keyboard from whatever the user is typing in.
pub(super) fn raise_to_top(window: HWND) -> Result<(), HostError> {
    // SAFETY: a plain Win32 call; an invalid handle fails rather than faults.
    unsafe {
        SetWindowPos(
            window,
            Some(HWND_TOP),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )?;
    }
    Ok(())
}

/// Give every child of `parent` `WS_CLIPSIBLINGS`, and report how many needed
/// it.
///
/// **The other half of "the panel is on top", and the half that decides what is
/// on the screen.** Z order alone does not stop a window from drawing over the
/// windows above it: `WS_CLIPSIBLINGS` is what excludes their rectangles from
/// its update region, and a window without it paints across them regardless of
/// who is in front. `WRY_WEBVIEW` — the child wry parents WebView2 into — is
/// created *without* that bit (measured; the tree dump in [`report`] prints it),
/// so a docked panel raised above it was still painted over by the page: hit
/// tests reached the document, pixels did not, which is the worst of the three
/// possible states because it puts an invisible window under the user's cursor.
///
/// Only ever *adds* a bit, and only to windows in our own top-level window.
/// `SWP_FRAMECHANGED` with no move, size, Z change or activation is what makes a
/// style change take effect without being a layout change.
pub(super) fn ensure_siblings_clip(parent: HWND) -> usize {
    let mut fixed = 0;
    for sibling in children_top_first(parent) {
        if sibling.style & WS_CLIPSIBLINGS.0 != 0 {
            continue;
        }
        let window = sibling.window.hwnd();
        // SAFETY: plain Win32 calls on a child of a window this process owns.
        // A refused `SetWindowPos` leaves the style set and the frame stale,
        // which is why the result is checked rather than dropped.
        unsafe {
            SetWindowLongPtrW(
                window,
                GWL_STYLE,
                (sibling.style | WS_CLIPSIBLINGS.0) as isize,
            );
            if SetWindowPos(
                window,
                None,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
            .is_ok()
            {
                fixed += 1;
            }
        }
    }
    fixed
}

/// What a click at this **screen** point would hit.
///
/// `WindowFromPoint` is the same hit test the mouse takes, `WM_NCHITTEST` and
/// all: a window that answers `HTTRANSPARENT` is stepped over exactly as it
/// would be for a real click. That makes it the right probe for input — and the
/// wrong one for "is my window visible", which is [`pixel_at`].
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn hit_target(point: (i32, i32)) -> Option<WindowId> {
    // SAFETY: a plain Win32 call taking a value.
    let hit = unsafe {
        WindowFromPoint(POINT {
            x: point.0,
            y: point.1,
        })
    };
    (!hit.0.is_null()).then(|| WindowId::from_hwnd(hit))
}

/// Would a click at `point` reach `panel` — or anything nested inside it?
///
/// The guest is two levels down (panel → clip child → guest), so a hit on any
/// descendant is a hit that reaches the docked document.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn reaches(panel: HWND, point: (i32, i32)) -> bool {
    let Some(hit) = hit_target(point) else {
        return false;
    };
    // SAFETY: `IsChild` validates both handles itself.
    hit.hwnd() == panel || unsafe { IsChild(panel, hit.hwnd()) }.as_bool()
}

/// Which of `parent`'s descendants a click at this **screen** point would land
/// in, or `None` when the point is over no child of it at all.
///
/// **The probe a test must use**, where [`hit_target`] is the probe a running
/// shell must use. `WindowFromPoint` walks the *whole desktop*: it is the truth
/// about a real click, and therefore also answers with somebody else's window
/// whenever another application happens to be in front — which on a shared test
/// machine is most of the time and has nothing to do with the code under test.
/// `ChildWindowFromPointEx` is scoped to one window's own tree, so it answers
/// the question a z-order test is actually asking: *within this window*, whose
/// pixel is this?
///
/// It descends as far as the tree goes, so a hit lands on the guest rather than
/// on the panel two levels above it. `CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT |
/// CWP_SKIPDISABLED` are the flags that make it agree with the mouse about what
/// does not count.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn child_hit(parent: HWND, point: (i32, i32)) -> Option<WindowId> {
    let mut current = parent;
    for _ in 0..MAX_SIBLINGS {
        let mut local = POINT {
            x: point.0,
            y: point.1,
        };
        // SAFETY: plain Win32 calls on a live window and a live `POINT`.
        if !unsafe { ScreenToClient(current, &mut local) }.as_bool() {
            return None;
        }
        // SAFETY: as above.
        let child = unsafe {
            ChildWindowFromPointEx(
                current,
                local,
                CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT | CWP_SKIPDISABLED,
            )
        };
        if child.0.is_null() || child == current {
            break;
        }
        current = child;
    }
    (current != parent).then(|| WindowId::from_hwnd(current))
}

/// The colour on the screen at this point, as Win32's `0x00BBGGRR`.
///
/// Read from the screen device context, so it is the composited desktop and not
/// any one window's idea of what it drew. `None` means the read failed — which
/// on a locked or remote session is a normal answer, not a finding.
///
/// **It answers for whatever is frontmost.** A shell window behind another
/// application reports that application's pixels, so a caller has to be looking
/// at its own window for the answer to mean anything.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn pixel_at(point: (i32, i32)) -> Option<u32> {
    // SAFETY: the screen DC is acquired and released in this block, and
    // `GetPixel` only reads through it.
    unsafe {
        let screen = GetDC(None);
        if screen.is_invalid() {
            return None;
        }
        let colour = GetPixel(screen, point.0, point.1);
        ReleaseDC(None, screen);
        (colour.0 != CLR_INVALID).then_some(colour.0)
    }
}

/// Every direct child of `parent`, **topmost first**.
///
/// `GW_CHILD` answers with the child highest in the z-order and `GW_HWNDNEXT`
/// walks downwards from there, so index 0 is what a click lands on first.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn children_top_first(parent: HWND) -> Vec<Sibling> {
    let mut chain = Vec::new();
    // SAFETY: plain Win32 calls; each handle comes from the call before it.
    let mut next = unsafe { GetWindow(parent, GW_CHILD) }.ok();
    while let Some(window) = next.filter(|hwnd| !hwnd.0.is_null()) {
        chain.push(Sibling {
            window: WindowId::from_hwnd(window),
            class: class_name(window),
            // SAFETY: as above.
            visible: unsafe { IsWindowVisible(window) }.as_bool(),
            rect: screen_rect_of(window),
            // SAFETY: as above.
            style: unsafe { GetWindowLongPtrW(window, GWL_STYLE) } as u32,
            // SAFETY: as above.
            ex_style: unsafe { GetWindowLongPtrW(window, GWL_EXSTYLE) } as u32,
        });
        if chain.len() >= MAX_SIBLINGS {
            break;
        }
        // SAFETY: as above.
        next = unsafe { GetWindow(window, GW_HWNDNEXT) }.ok();
    }
    chain
}

/// Where `window` sits in its parent's z-order, and how many siblings it has.
/// `None` when it is not in the chain at all.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn depth_in_z_order(window: HWND) -> Option<(usize, usize)> {
    // SAFETY: a plain Win32 call; a top-level window answers with an error,
    // which is the "no parent" answer.
    let parent = unsafe { GetParent(window) }.ok()?;
    let chain = children_top_first(parent);
    let index = chain
        .iter()
        .position(|sibling| sibling.window.hwnd() == window)?;
    Some((index, chain.len()))
}

/// `class (0x…)` — the shortest description that identifies a window in a log.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn describe(window: HWND) -> String {
    if window.0.is_null() {
        return "none".to_string();
    }
    format!("{} ({:#x})", class_name(window), window.0 as isize)
}

/// `child < parent < …` up to the top-level window, so a hit deep inside
/// somebody else's widget tree says whose tree it is.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn chain_to_root(window: HWND) -> String {
    let mut parts = vec![describe(window)];
    let mut current = window;
    for _ in 0..MAX_SIBLINGS {
        // SAFETY: a plain Win32 call; the error is "no parent".
        let Ok(parent) = (unsafe { GetParent(current) }) else {
            break;
        };
        if parent.0.is_null() {
            break;
        }
        parts.push(describe(parent));
        current = parent;
    }
    parts.join(" < ")
}

pub(super) fn class_name(window: HWND) -> String {
    let mut buffer = [0u16; 256];
    // SAFETY: `GetClassNameW` writes at most `buffer.len()` units into it.
    let length = unsafe { GetClassNameW(window, &mut buffer) };
    if length <= 0 {
        return String::new();
    }
    String::from_utf16_lossy(&buffer[..length as usize])
}

/// Where a window is on the desktop, in physical pixels.
#[cfg_attr(not(any(test, debug_assertions)), allow(dead_code))]
pub(super) fn screen_rect_of(window: HWND) -> PhysicalRect {
    let mut rect = windows::Win32::Foundation::RECT::default();
    // SAFETY: a plain Win32 call filling a live `RECT`.
    let _ = unsafe { GetWindowRect(window, &mut rect) };
    PhysicalRect {
        x: rect.left,
        y: rect.top,
        width: rect.right - rect.left,
        height: rect.bottom - rect.top,
    }
}

/// Print everything known about one point over a docked panel.
///
/// One call, both questions, plus the sibling chain that explains the answer —
/// so a bug report about z-order carries its own evidence instead of a single
/// `WindowFromPoint` line with nothing to check it against.
#[cfg(debug_assertions)]
pub(super) fn report(tag: &str, top_level: HWND, panel: HWND, point: (i32, i32)) {
    let log = |line: String| crate::backend::log(&format!("office host zorder [{tag}]: {line}"));

    log(format!(
        "probing ({},{}) over panel {}",
        point.0,
        point.1,
        describe(panel)
    ));
    match depth_in_z_order(panel) {
        Some((index, total)) => log(format!(
            "the panel is #{} of {total} children of {} (0 = topmost)",
            index + 1,
            describe(top_level)
        )),
        None => log("the panel is not in its parent's child chain".to_string()),
    }
    for (index, sibling) in children_top_first(top_level).iter().enumerate() {
        log(format!(
            "  z{index}: {} ({:#x}) visible={} clipsiblings={} noredirectionbitmap={} rect={:?}",
            sibling.class,
            sibling.window.0,
            sibling.visible,
            sibling.style & WS_CLIPSIBLINGS.0 != 0,
            sibling.ex_style & WS_EX_NOREDIRECTIONBITMAP.0 != 0,
            sibling.rect
        ));
        if sibling.window.hwnd() == panel {
            continue;
        }
        // One level of somebody else's widget tree, because whether its pixels
        // come from the redirection surface or from a composition visual is not
        // a property of the sibling we can see from here.
        for (depth, line) in subtree(sibling.window.hwnd(), 3) {
            log(format!("      {:indent$}{line}", "", indent = depth * 2));
        }
    }
    match hit_target(point) {
        Some(hit) => log(format!(
            "desktop hit -> {} | reaches the panel = {}",
            chain_to_root(hit.hwnd()),
            reaches(panel, point)
        )),
        None => log("desktop hit -> nothing at all".to_string()),
    }
    match child_hit(top_level, point) {
        Some(hit) => log(format!("in-window hit -> {}", chain_to_root(hit.hwnd()))),
        None => log("in-window hit -> no child of the shell window".to_string()),
    }
    match pixel_at(point) {
        Some(colour) => log(format!(
            "screen px -> {colour:#08x} (0x00BBGGRR; the guest body is 0xffffff)"
        )),
        None => log("screen px -> unreadable".to_string()),
    }
}

/// Somebody else's widget tree, `depth` levels down, as `(depth, line)` pairs.
///
/// Only the two bits that matter for "whose pixels win": `WS_CLIPSIBLINGS`, and
/// `WS_EX_NOREDIRECTIONBITMAP` — a window carrying the latter puts nothing in
/// the top-level window's redirection surface and is composited from its own
/// visual tree instead, which is a different layer entirely from the one our
/// GDI panel paints into.
#[cfg(debug_assertions)]
pub(super) fn subtree(root: HWND, depth: usize) -> Vec<(usize, String)> {
    let mut lines = Vec::new();
    if depth == 0 {
        return lines;
    }
    for child in children_top_first(root) {
        lines.push((
            0,
            format!(
                "{} ({:#x}) clipsiblings={} noredirectionbitmap={} rect={:?}",
                child.class,
                child.window.0,
                child.style & WS_CLIPSIBLINGS.0 != 0,
                child.ex_style & WS_EX_NOREDIRECTIONBITMAP.0 != 0,
                child.rect
            ),
        ));
        for (deeper, line) in subtree(child.window.hwnd(), depth - 1) {
            lines.push((deeper + 1, line));
        }
    }
    lines
}

/// **The control for the painting half**, and the only way to tell two very
/// different failures apart.
///
/// "The panel rectangle shows the webview" has two possible causes and they need
/// opposite fixes: either the panel is *behind* the webview (a Z-order bug, ours
/// to fix with [`raise_to_top`]) or the panel is in front and the webview's
/// pixels are composited over it anyway (a compositing property of WebView2,
/// which no `SetWindowPos` can touch). Hiding the sibling and reading the same
/// pixel again separates them: if the colour changes, the panel was painting all
/// along and something above it was winning; if it does not, the panel is not
/// painting at all.
///
/// Hides every child of `top_level` that is neither the panel nor inside it,
/// samples, and puts them back — a flicker in a debug demo, and the only
/// non-destructive way to ask.
#[cfg(debug_assertions)]
pub(super) fn paint_control(tag: &str, top_level: HWND, panel: HWND, point: (i32, i32)) {
    use windows::Win32::UI::WindowsAndMessaging::{ShowWindow, SW_HIDE, SW_SHOWNA};

    let log = |line: String| crate::backend::log(&format!("office host zorder [{tag}]: {line}"));
    let others: Vec<HWND> = children_top_first(top_level)
        .iter()
        .map(|sibling| sibling.window.hwnd())
        .filter(|window| {
            // SAFETY: `IsChild` validates both handles itself.
            *window != panel && !unsafe { IsChild(panel, *window) }.as_bool()
        })
        .collect();
    if others.is_empty() {
        log("paint control: the panel has no siblings to hide".to_string());
        return;
    }

    let before = pixel_at(point);
    for window in &others {
        // SAFETY: `ShowWindow` validates the handle and fails rather than faults.
        let _ = unsafe { ShowWindow(*window, SW_HIDE) };
    }
    std::thread::sleep(std::time::Duration::from_millis(600));
    let hidden = pixel_at(point);
    for window in others.iter().rev() {
        // SAFETY: as above. `SW_SHOWNA` so restoring does not steal activation.
        let _ = unsafe { ShowWindow(*window, SW_SHOWNA) };
    }
    log(format!(
        "paint control: {} sibling(s) hidden - the pixel went {before:x?} -> {hidden:x?}",
        others.len()
    ));
    match (before, hidden) {
        (Some(before), Some(hidden)) if before == hidden => log(
            "paint control: unchanged - either the panel was already the one being painted, \
             or it is not painting there at all; the `screen px` line above says which"
                .to_string(),
        ),
        (Some(_), Some(_)) => log(
            "paint control: changed - the panel *was* painting and a sibling's pixels were \
             winning, which Z order alone does not decide"
                .to_string(),
        ),
        _ => log("paint control: the screen could not be read".to_string()),
    }
}

// The tests for this module live in [`super::hosting_tests`]: everything here
// that is worth asserting needs real windows, and window tests share one desktop
// and are serialised through the lock that lives there.
