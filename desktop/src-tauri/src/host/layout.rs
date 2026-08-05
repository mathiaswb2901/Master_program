//! Moving three windows at once, without thrashing.
//!
//! Dragging a dockview splitter produces a resize event per animation frame,
//! and each one wants to move the panel window, its clip child and the guest.
//! Nine separate `SetWindowPos` calls per frame is nine separate rounds of
//! `WM_WINDOWPOSCHANGING`/`WM_SIZE`/`WM_PAINT` — and three of them cross a
//! process boundary. Two things stop that from being felt:
//!
//! * **`BeginDeferWindowPos`** batches our own two windows into one atomic
//!   transaction, so they arrive together and an intermediate state is never
//!   painted.
//! * **[`Coalescer`]** drops the frames that would change nothing. A resize
//!   storm that ends where it started is one apply, not two hundred.
//!
//! **The guest is moved separately, and that is a measured decision rather than
//! an oversight.** `SWP_ASYNCWINDOWPOS` is what keeps a busy — or hung — guest
//! from stalling a drag: it posts the request to the owning thread instead of
//! sending it. `DeferWindowPos` **rejects that flag**: adding it fails the call
//! with `ERROR_INVALID_PARAMETER`, which is not in the documentation and is
//! asserted in `hosting_tests::deferwindowpos_rejects_the_asynchronous_flag`.
//! So the split follows the boundary that matters anyway: our two windows,
//! owned by this thread, go in one batch; the one window owned by another
//! process gets its own asynchronous call. (Mixing a parent and its child in a
//! single batch turns out to be fine — that part of the folklore is wrong, and
//! the same test says so.)
//!
//! **And `SWP_ASYNCWINDOWPOS` does not do what we need it to.** Its
//! documentation is conditional: the request is posted "if the calling thread
//! and the thread that owns the window are attached to **different input
//! queues**". Making the guest our child is precisely what attaches those two
//! queues — the same mechanism that hands us focus routing for free — so the
//! condition is false and the call waits. Measured against a deliberately hung
//! guest (`hosting_tests::hang_isolation_measurement`):
//!
//! | 10 moves, guest hung | time |
//! |---|---|
//! | panel + clip child (ours) | **0.2 ms** |
//! | the same, including the guest | **10.0 s** |
//! | the guest alone, from a thread that owns no window in the chain | **0.15 ms** |
//!
//! So the flag is kept — it is correct, it costs nothing, and it does help for
//! any guest whose queue is not attached — but it is not the containment it
//! looks like. The last row is: a worker thread that owns none of these windows
//! is attached to nothing, and the same call from there returns immediately.
//! Moving guest geometry onto such a thread is the fix, and it is deliberately
//! **not** in this PR: hang isolation is its own piece of work, and this one's
//! job was to measure it honestly.
//!
//! The rest of the flags: `SWP_NOCOPYBITS` skips preserving the old client
//! bits, which is wasted work when everything is about to repaint;
//! `SWP_NOACTIVATE` and `SWP_NOZORDER` keep a resize from stealing focus or
//! shuffling the panel in front of its neighbours.

use windows::Win32::Foundation::HWND;
use windows::Win32::UI::WindowsAndMessaging::{
    BeginDeferWindowPos, DeferWindowPos, EndDeferWindowPos, SetWindowPos, HDWP,
    SET_WINDOW_POS_FLAGS, SWP_ASYNCWINDOWPOS, SWP_NOACTIVATE, SWP_NOCOPYBITS, SWP_NOZORDER,
};

use super::geometry::{HostLayout, PhysicalRect};
use super::HostError;

/// Which layout frames are worth a Win32 call.
///
/// Deliberately a plain value type with no window in it, so the "a resize storm
/// must not thrash" property is a unit test rather than something you have to
/// take on faith while watching a drag.
#[derive(Debug, Default)]
pub struct Coalescer {
    last: Option<HostLayout>,
    /// How many layouts reached Win32.
    pub applied: u64,
    /// How many were recognised as no-ops.
    pub skipped: u64,
}

impl Coalescer {
    /// The layout to apply, or `None` when it would change nothing.
    pub fn next(&mut self, layout: HostLayout) -> Option<HostLayout> {
        if self.last == Some(layout) {
            self.skipped += 1;
            return None;
        }
        self.last = Some(layout);
        self.applied += 1;
        Some(layout)
    }

    /// Forget what was applied. Called when the window set changes — a guest
    /// arriving or leaving means the next layout must reach Win32 even if the
    /// panel rectangle has not moved a pixel.
    pub fn invalidate(&mut self) {
        self.last = None;
    }

    pub fn last(&self) -> Option<HostLayout> {
        self.last
    }
}

/// Flags for our own windows, moved as one batch on the thread that owns them.
pub(super) const OURS: SET_WINDOW_POS_FLAGS =
    SET_WINDOW_POS_FLAGS(SWP_NOCOPYBITS.0 | SWP_NOACTIVATE.0 | SWP_NOZORDER.0);
/// Flags for the guest: the same, plus the asynchronous post that keeps another
/// process's message loop off ours.
pub(super) const GUEST: SET_WINDOW_POS_FLAGS = SET_WINDOW_POS_FLAGS(OURS.0 | SWP_ASYNCWINDOWPOS.0);

/// Apply one layout: the panel and its clip child together, then the guest
/// asynchronously.
pub(super) fn apply(
    panel: HWND,
    clip: HWND,
    guest: Option<HWND>,
    layout: &HostLayout,
) -> Result<(), HostError> {
    // SAFETY: the HDWP is threaded through every call and consumed exactly
    // once by `EndDeferWindowPos`; a failed `DeferWindowPos` invalidates it and
    // frees its resources, which is why the `?` abandons it rather than ending
    // it. The `SetWindowPos` afterwards is a plain call on a validated handle.
    unsafe {
        let mut batch = BeginDeferWindowPos(2)?;
        batch = defer(batch, panel, layout.panel)?;
        batch = defer(batch, clip, layout.clip)?;
        EndDeferWindowPos(batch)?;

        if let Some(guest) = guest {
            SetWindowPos(
                guest,
                None,
                layout.guest.x,
                layout.guest.y,
                layout.guest.width,
                layout.guest.height,
                GUEST,
            )?;
        }
    }
    Ok(())
}

/// # Safety
/// `batch` must be a live `HDWP` that has not yet been ended.
unsafe fn defer(batch: HDWP, window: HWND, rect: PhysicalRect) -> Result<HDWP, HostError> {
    let next = unsafe {
        DeferWindowPos(
            batch,
            window,
            None,
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            OURS,
        )?
    };
    Ok(next)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::host::geometry::host_layout;

    fn layout(width: i32) -> HostLayout {
        host_layout(
            PhysicalRect {
                x: 0,
                y: 0,
                width,
                height: 500,
            },
            0,
        )
    }

    #[test]
    fn the_first_layout_is_always_applied() {
        let mut coalescer = Coalescer::default();
        assert!(coalescer.next(layout(800)).is_some());
        assert_eq!(coalescer.applied, 1);
    }

    #[test]
    fn a_resize_storm_that_changes_nothing_costs_one_call() {
        // What a dockview drag looks like when the pointer stops moving but the
        // events keep arriving: two hundred identical rectangles.
        let mut coalescer = Coalescer::default();
        for _ in 0..200 {
            coalescer.next(layout(800));
        }
        assert_eq!(coalescer.applied, 1);
        assert_eq!(coalescer.skipped, 199);
    }

    #[test]
    fn every_frame_that_really_moves_is_applied() {
        let mut coalescer = Coalescer::default();
        for width in 400..600 {
            assert!(
                coalescer.next(layout(width)).is_some(),
                "a real move was dropped"
            );
        }
        assert_eq!(coalescer.applied, 200);
        assert_eq!(coalescer.skipped, 0);
    }

    #[test]
    fn a_move_back_and_forth_is_not_coalesced_away() {
        // Only the *last* applied layout is compared, so an A-B-A drag applies
        // three times. Anything cleverer would need to know whether the window
        // is still where we last put it, which we do not.
        let mut coalescer = Coalescer::default();
        coalescer.next(layout(800));
        coalescer.next(layout(900));
        coalescer.next(layout(800));
        assert_eq!(coalescer.applied, 3);
    }

    #[test]
    fn a_guest_arriving_forces_the_next_layout_through() {
        // The panel has not moved, but there is a window in it now that has
        // never been positioned.
        let mut coalescer = Coalescer::default();
        coalescer.next(layout(800));
        coalescer.invalidate();
        assert!(coalescer.next(layout(800)).is_some());
        assert_eq!(coalescer.applied, 2);
        assert_eq!(coalescer.skipped, 0);
    }
}
