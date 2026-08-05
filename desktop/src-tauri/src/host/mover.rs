//! Guest geometry, issued from a thread that owns no window.
//!
//! **This is the hang containment**, and it exists because of a measurement
//! rather than a worry. Making a guest our child attaches its input queue to the
//! main thread's — that is what hands us focus routing for free (see
//! [`super::focus`]) — and the price is that a guest which stops pumping makes
//! every `SetWindowPos` against it *wait*. `SWP_ASYNCWINDOWPOS` does not save
//! us: it only posts when the two threads are on **different** input queues, and
//! being our child is precisely what put them on the same one. Measured in
//! [`super::hosting_tests::hang_isolation_measurement`] with the guest wedged:
//!
//! | 10 moves, guest hung | from the main thread | from here |
//! |---|---|---|
//! | our panel + clip child | 0.2 ms | — (never leaves the main thread) |
//! | the guest | ~10 s | ~1.5 ms |
//!
//! A thread that owns no window in the parent chain is attached to nothing, so
//! the same asynchronous call posts and returns. That is the whole trick, and
//! everything below is the bookkeeping needed to make it safe:
//!
//! * **Latest-wins coalescing.** A drag produces a frame per animation tick. The
//!   worker takes the whole pending map at once and applies one rectangle per
//!   window, so a storm that outruns the worker collapses instead of queueing.
//!   [`super::layout::Coalescer`] already drops frames that change nothing; this
//!   drops frames that have been *superseded*, which is a different property and
//!   the one that matters when the far side is slow.
//! * **[`Mover::settle`]**, the ordering guarantee. Handing a window back to the
//!   desktop is a `SetParent` plus a `SetWindowPos` to its old screen position —
//!   from the main thread. A move still queued here would land *after* that and
//!   put the user's window at a rectangle that meant something only while it was
//!   our child. So every release drops the pending move and waits for any pass
//!   already in flight, which costs microseconds because the worker never
//!   blocks.
//!
//! **Fire-and-forget, deliberately.** [`Mover::place`] returns nothing: a
//! `SetWindowPos` on a guest can only fail because the window is gone, which is
//! already the watchdog's question and not a reason to fail the drag frame that
//! noticed. Returning a result here would mean either blocking on the worker —
//! reintroducing exactly the coupling this module removes — or inventing a
//! second liveness authority.

use std::collections::HashMap;
use std::sync::{Condvar, Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use super::geometry::PhysicalRect;
use super::WindowId;

/// How long [`Mover::settle`] waits for a pass that is already running.
///
/// Generous by three orders of magnitude: a pass is a handful of posted
/// `SetWindowPos` calls. It is a bound so that a worker wedged by something
/// nobody predicted degrades to "the window went back to the wrong place"
/// rather than to a frozen main thread.
const SETTLE_TIMEOUT: Duration = Duration::from_secs(2);

/// What the worker has been asked to do.
#[derive(Default)]
struct Queue {
    /// Where each window should be. Latest wins — a superseded rectangle is
    /// never applied, which is what keeps a storm bounded.
    pending: HashMap<WindowId, PhysicalRect>,
    /// A pass has taken the map and is applying it.
    busy: bool,
    /// Passes finished. [`Mover::settle`] waits for this to move rather than for
    /// a flag, so it cannot miss a pass that started and ended while it waited.
    completed: u64,
    /// Set by [`Mover::stop`], for the tests that spawn their own worker.
    stop: bool,
}

/// A worker thread and the queue it drains.
pub struct Mover {
    queue: Mutex<Queue>,
    wake: Condvar,
}

impl Mover {
    /// Start a worker that applies each move with `apply`.
    ///
    /// Parameterised so the coalescing and the ordering guarantee are unit-
    /// tested against a recording closure — with no window, no message loop and
    /// no desktop. The production instance is [`mover`].
    pub fn spawn<F>(name: &str, apply: F) -> &'static Self
    where
        F: Fn(WindowId, PhysicalRect) + Send + 'static,
    {
        // Leaked on purpose: the worker borrows it for the life of the thread,
        // and there is one per process (or one per test). An `Arc` would say
        // the same thing with a refcount nobody ever drops to zero.
        let mover: &'static Self = Box::leak(Box::new(Self {
            queue: Mutex::new(Queue::default()),
            wake: Condvar::new(),
        }));
        let spawned = thread::Builder::new()
            .name(name.to_string())
            .spawn(move || mover.run(apply));
        if spawned.is_err() {
            // Nothing to fall back to but the main thread, which is what this
            // module exists to keep out of the way of a hung guest. Say so.
            crate::backend::log("office host: the geometry worker could not be started");
        }
        mover
    }

    /// Put `window` at `rect`, eventually. Never blocks.
    pub fn place(&self, window: WindowId, rect: PhysicalRect) {
        if window.is_null() {
            return;
        }
        let mut queue = self.lock();
        queue.pending.insert(window, rect);
        drop(queue);
        self.wake.notify_one();
    }

    /// Forget any queued move for `window` and wait out a pass that may already
    /// hold one. Returns whether the wait succeeded.
    ///
    /// Called before a window is handed back to the desktop, which is the one
    /// moment at which a stale rectangle would be visible to the user: the
    /// coordinates it was queued with are relative to a clip child the window
    /// is about to stop being inside.
    pub fn settle(&self, window: WindowId) -> bool {
        let mut queue = self.lock();
        queue.pending.remove(&window);
        if !queue.busy {
            return true;
        }
        let target = queue.completed + 1;
        let deadline = Instant::now() + SETTLE_TIMEOUT;
        while queue.completed < target {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                crate::backend::log(
                    "office host: the geometry worker did not settle; a hosted window may have \
                     been put back at the wrong place",
                );
                return false;
            }
            let (next, _) = self
                .wake
                .wait_timeout(queue, remaining)
                .unwrap_or_else(|err| err.into_inner());
            queue = next;
        }
        true
    }

    /// End the worker. Tests only — the production instance runs for the life of
    /// the process, and a thread doing nothing but waiting on a condvar costs
    /// nothing to leave running.
    #[cfg(test)]
    pub fn stop(&self) {
        let mut queue = self.lock();
        queue.stop = true;
        drop(queue);
        self.wake.notify_all();
    }

    /// How many passes have finished. Test-only introspection: the coalescing
    /// claim is "a storm costs fewer passes than frames", which is not
    /// observable from the moves alone.
    #[cfg(test)]
    pub fn passes(&self) -> u64 {
        self.lock().completed
    }

    fn run<F>(&self, apply: F)
    where
        F: Fn(WindowId, PhysicalRect),
    {
        loop {
            let batch = {
                let mut queue = self.lock();
                while queue.pending.is_empty() && !queue.stop {
                    queue = self
                        .wake
                        .wait(queue)
                        .unwrap_or_else(|err| err.into_inner());
                }
                if queue.stop {
                    return;
                }
                queue.busy = true;
                // Everything queued so far, in one go: a window named twice
                // while the last pass ran is moved once, to where it ended up.
                std::mem::take(&mut queue.pending)
            };
            for (window, rect) in batch {
                apply(window, rect);
            }
            let mut queue = self.lock();
            queue.busy = false;
            queue.completed += 1;
            drop(queue);
            self.wake.notify_all();
        }
    }

    /// A poisoned queue is a panic in `apply`, which has already been reported
    /// by the panic handler. The state behind the lock is a map of rectangles
    /// with no invariant a panic could have broken, so recovering is strictly
    /// better than refusing to move any window ever again.
    fn lock(&self) -> MutexGuard<'_, Queue> {
        self.queue.lock().unwrap_or_else(|err| err.into_inner())
    }
}

/// The process-wide worker every hosted guest is moved by.
pub fn mover() -> &'static Mover {
    static MOVER: OnceLock<&'static Mover> = OnceLock::new();
    MOVER.get_or_init(|| Mover::spawn("workbench-host-geometry", apply_win32))
}

/// One `SetWindowPos`, from a thread attached to no input queue.
///
/// `SWP_ASYNCWINDOWPOS` is still on the flags ([`super::layout::GUEST`]) and now
/// does what its documentation says, because the condition it is conditional on
/// finally holds here.
fn apply_win32(window: WindowId, rect: PhysicalRect) {
    use windows::Win32::UI::WindowsAndMessaging::SetWindowPos;

    // SAFETY: a plain Win32 call on a handle the API validates itself; a window
    // that has been destroyed fails the call rather than faulting.
    let moved = unsafe {
        SetWindowPos(
            window.hwnd(),
            None,
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            super::layout::GUEST,
        )
    };
    if let Err(err) = moved {
        // Almost always "the guest just died"; the watchdog is what turns that
        // into a lifecycle answer, so this is a log line and nothing more.
        crate::backend::log(&format!("office host: moving a guest failed: {err}"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    fn rect(width: i32) -> PhysicalRect {
        PhysicalRect {
            x: 0,
            y: 0,
            width,
            height: 100,
        }
    }

    /// A recording worker: every applied move on a channel, with an optional
    /// delay so a test can hold a pass open and place frames behind it.
    ///
    /// **Reports before it sleeps**, so receiving a move means the worker is
    /// *inside* that pass with `delay` still to run. Sleeping first would make
    /// every "and now, while it is busy…" step in these tests a race the worker
    /// usually wins, which is how the coalescing assertion first went flaky.
    fn recording(name: &str, delay: Duration) -> (&'static Mover, mpsc::Receiver<(WindowId, i32)>) {
        let (tx, rx) = mpsc::channel();
        let mover = Mover::spawn(name, move |window, rect| {
            let _ = tx.send((window, rect.width));
            thread::sleep(delay);
        });
        (mover, rx)
    }

    #[test]
    fn a_move_reaches_the_worker() {
        let (mover, rx) = recording("test-basic", Duration::ZERO);
        mover.place(WindowId(1), rect(400));
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)), Ok((WindowId(1), 400)));
        mover.stop();
    }

    #[test]
    fn a_null_window_is_never_queued() {
        let (mover, rx) = recording("test-null", Duration::ZERO);
        mover.place(WindowId(0), rect(400));
        mover.place(WindowId(7), rect(410));
        // The real one arrives; the null one was dropped rather than posted to
        // a handle Win32 would have to reject.
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)), Ok((WindowId(7), 410)));
        mover.stop();
    }

    /// **The property a hung guest needs.** Frames that pile up behind a slow
    /// apply collapse to the last rectangle, so a drag cannot build a backlog
    /// the user then watches replay.
    #[test]
    fn frames_that_pile_up_behind_a_slow_move_collapse_to_the_last_one() {
        let (mover, rx) = recording("test-coalesce", Duration::from_millis(150));
        // The first frame takes the worker; the next hundred queue behind it.
        mover.place(WindowId(3), rect(500));
        // Wait until that pass is really in flight, or the frames below would
        // simply join it and the test would prove nothing.
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)), Ok((WindowId(3), 500)));
        for width in 600..700 {
            mover.place(WindowId(3), rect(width));
        }
        // Whatever the scheduler did with those hundred frames, the drag ends
        // where it stopped and costs a handful of moves rather than a hundred.
        let mut applied = Vec::new();
        while let Ok((_, width)) = rx.recv_timeout(Duration::from_millis(500)) {
            applied.push(width);
        }
        assert_eq!(
            applied.last().copied(),
            Some(699),
            "the drag did not end where it stopped: {applied:?}"
        );
        assert!(
            applied.len() <= 3,
            "100 queued frames cost {} moves: {applied:?}",
            applied.len()
        );
        mover.stop();
    }

    /// Two panels are two windows: coalescing must be per window, or dragging a
    /// splitter between two hosted documents would move only one of them.
    #[test]
    fn two_windows_in_one_pass_both_move() {
        let (mover, rx) = recording("test-two", Duration::from_millis(80));
        mover.place(WindowId(1), rect(300));
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)), Ok((WindowId(1), 300)));
        mover.place(WindowId(1), rect(310));
        mover.place(WindowId(2), rect(320));
        let mut seen = vec![
            rx.recv_timeout(Duration::from_secs(2)).expect("first"),
            rx.recv_timeout(Duration::from_secs(2)).expect("second"),
        ];
        seen.sort_by_key(|(window, _)| window.0);
        assert_eq!(seen, vec![(WindowId(1), 310), (WindowId(2), 320)]);
        mover.stop();
    }

    /// **The ordering guarantee.** After `settle`, nothing this module queued is
    /// still on its way to that window — which is what lets the main thread put
    /// it back on the desktop without a stale rectangle landing afterwards.
    #[test]
    fn settle_drops_a_queued_move_and_waits_out_the_one_in_flight() {
        let (mover, rx) = recording("test-settle", Duration::from_millis(150));
        mover.place(WindowId(9), rect(800));
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)), Ok((WindowId(9), 800)));

        // One frame queued behind the pass that is running, then a release.
        mover.place(WindowId(9), rect(900));
        assert!(mover.settle(WindowId(9)), "settle timed out");
        assert!(
            rx.recv_timeout(Duration::from_millis(400)).is_err(),
            "a move landed after the window had been released"
        );
        mover.stop();
    }

    #[test]
    fn settling_an_idle_worker_is_immediate() {
        let (mover, _rx) = recording("test-settle-idle", Duration::ZERO);
        let started = Instant::now();
        assert!(mover.settle(WindowId(4)));
        assert!(started.elapsed() < Duration::from_millis(200));
        mover.stop();
    }

    #[test]
    fn settle_leaves_other_windows_alone() {
        // Releasing one panel must not cancel the other's pending frame.
        let (mover, rx) = recording("test-settle-other", Duration::from_millis(120));
        mover.place(WindowId(1), rect(200));
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)), Ok((WindowId(1), 200)));
        mover.place(WindowId(1), rect(210));
        mover.place(WindowId(2), rect(220));
        assert!(mover.settle(WindowId(1)));
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)), Ok((WindowId(2), 220)));
        mover.stop();
    }

    #[test]
    fn a_storm_costs_fewer_passes_than_frames() {
        let (mover, rx) = recording("test-storm", Duration::from_millis(20));
        for width in 0..200 {
            mover.place(WindowId(5), rect(400 + width));
        }
        // The last frame always lands, however many were dropped on the way.
        let mut last = 0;
        while let Ok((_, width)) = rx.recv_timeout(Duration::from_millis(500)) {
            last = width;
        }
        assert_eq!(last, 599);
        assert!(
            mover.passes() < 200,
            "200 frames cost {} passes; nothing was coalesced",
            mover.passes()
        );
        mover.stop();
    }
}
