//! The native close guard, as a state machine that can be tested without a
//! window.
//!
//! WebView2 ignores `beforeunload`, so a native close used to discard unsaved
//! buffers in silence. The shell holds `CloseRequested`, asks the UI, and lets
//! the window go only once the user has answered. Every transition that matters
//! lives here rather than inline in the event handler, because the interesting
//! part is not the Tauri wiring — it is the four ways this can go wrong:
//!
//! * the UI never armed the guard (a webview that never ran our code),
//! * the page navigated away from the one that armed it (a dev server that died
//!   mid-reload leaves an error page with no listener),
//! * the prompt was delivered to nobody (`emit` succeeds regardless — see
//!   `expired`),
//! * a confirm raced a cancel.
//!
//! Each of those ends in an unclosable window if it is got wrong, which is a
//! worse failure than the lost buffer the guard exists to prevent.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

/// What to do with a `CloseRequested` event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    /// Let the window go.
    Close,
    /// Hold the window and prompt the UI. `ticket` identifies this prompt so a
    /// watchdog for it cannot act on a later one.
    AskUi { ticket: u64 },
}

pub struct CloseGuard {
    /// Set by the UI (`shell_ready`); until then a close passes straight
    /// through, so a webview that never booted cannot trap the user.
    armed: AtomicBool,
    /// Set by `confirm`, spent by the close it triggers.
    confirmed: AtomicBool,
    /// Monotonic prompt id.
    tickets: AtomicU64,
    /// The ticket still waiting for the UI to acknowledge it; 0 = none.
    awaiting: AtomicU64,
}

impl CloseGuard {
    pub const fn new() -> Self {
        Self {
            armed: AtomicBool::new(false),
            confirmed: AtomicBool::new(false),
            tickets: AtomicU64::new(0),
            awaiting: AtomicU64::new(0),
        }
    }

    /// The UI is live and listening; from here on the guard applies.
    pub fn arm(&self) {
        self.armed.store(true, Ordering::SeqCst);
    }

    /// A navigation started. The document that armed the guard is gone and the
    /// one arriving has no listener yet — and may never get one, which is
    /// exactly the reload-onto-a-dead-dev-server case. The UI re-arms through
    /// `shell_ready` on every load.
    pub fn disarm(&self) {
        self.armed.store(false, Ordering::SeqCst);
        self.awaiting.store(0, Ordering::SeqCst);
    }

    /// The user answered "close".
    pub fn confirm(&self) {
        self.confirmed.store(true, Ordering::SeqCst);
        self.awaiting.store(0, Ordering::SeqCst);
    }

    /// The user answered "cancel". Clearing `confirmed` matters: a confirm that
    /// raced a cancel must not leave the *next* close unguarded.
    pub fn cancel(&self) {
        self.confirmed.store(false, Ordering::SeqCst);
        self.awaiting.store(0, Ordering::SeqCst);
    }

    /// The UI received a prompt. This is the only proof anyone is listening, so
    /// the watchdog stands down on it — not on the user's answer, which may
    /// take as long as they like once the modal is up.
    pub fn ack(&self) {
        self.awaiting.store(0, Ordering::SeqCst);
    }

    pub fn on_close_requested(&self) -> Decision {
        // `swap` and not `load`: the confirmation is spent on this close, so a
        // later one is guarded again.
        if self.confirmed.swap(false, Ordering::SeqCst) || !self.armed.load(Ordering::SeqCst) {
            return Decision::Close;
        }
        let ticket = self.tickets.fetch_add(1, Ordering::SeqCst) + 1;
        self.awaiting.store(ticket, Ordering::SeqCst);
        Decision::AskUi { ticket }
    }

    /// True exactly once, when `ticket` is still unacknowledged: nobody
    /// answered, so the caller must stop holding the window.
    ///
    /// This has to exist because `emit` cannot tell us. Tauri delivers events by
    /// evaluating `const fn = window['…']; fn && fn({…})` in the webview, which
    /// succeeds for any live page — including one with no listener at all. An
    /// `Err` from `emit` therefore never means "the UI did not get it", and a
    /// guard whose only escape hatch is that `Err` is a guard with no escape
    /// hatch.
    pub fn expired(&self, ticket: u64) -> bool {
        self.awaiting
            .compare_exchange(ticket, 0, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ticket_of(decision: Decision) -> u64 {
        match decision {
            Decision::AskUi { ticket } => ticket,
            Decision::Close => panic!("expected the guard to ask the UI"),
        }
    }

    #[test]
    fn an_unarmed_guard_closes_immediately() {
        let guard = CloseGuard::new();
        assert_eq!(guard.on_close_requested(), Decision::Close);
    }

    #[test]
    fn an_armed_guard_asks_the_ui() {
        let guard = CloseGuard::new();
        guard.arm();
        assert_eq!(guard.on_close_requested(), Decision::AskUi { ticket: 1 });
    }

    #[test]
    fn a_confirmation_is_spent_on_one_close() {
        let guard = CloseGuard::new();
        guard.arm();
        guard.confirm();
        // The `window.close()` that `confirm_close` calls comes back here.
        assert_eq!(guard.on_close_requested(), Decision::Close);
        // A window that survived it (another close path, a second window) is
        // guarded again rather than closing silently.
        assert!(matches!(guard.on_close_requested(), Decision::AskUi { .. }));
    }

    #[test]
    fn a_cancel_leaves_the_next_close_guarded() {
        let guard = CloseGuard::new();
        guard.arm();
        ticket_of(guard.on_close_requested());
        guard.cancel();
        assert!(matches!(guard.on_close_requested(), Decision::AskUi { .. }));
    }

    #[test]
    fn a_confirm_that_raced_a_cancel_does_not_leak_through() {
        let guard = CloseGuard::new();
        guard.arm();
        guard.confirm();
        guard.cancel();
        assert!(matches!(guard.on_close_requested(), Decision::AskUi { .. }));
    }

    #[test]
    fn a_prompt_nobody_answered_expires_once() {
        let guard = CloseGuard::new();
        guard.arm();
        let ticket = ticket_of(guard.on_close_requested());
        assert!(guard.expired(ticket));
        // Only the first watchdog gets to act.
        assert!(!guard.expired(ticket));
    }

    #[test]
    fn an_acknowledged_prompt_never_expires() {
        let guard = CloseGuard::new();
        guard.arm();
        let ticket = ticket_of(guard.on_close_requested());
        guard.ack();
        // The modal is up; the user may take minutes over it.
        assert!(!guard.expired(ticket));
    }

    #[test]
    fn answering_stands_the_watchdog_down() {
        for answer in [CloseGuard::confirm, CloseGuard::cancel] {
            let guard = CloseGuard::new();
            guard.arm();
            let ticket = ticket_of(guard.on_close_requested());
            answer(&guard);
            assert!(!guard.expired(ticket));
        }
    }

    #[test]
    fn a_second_close_while_the_modal_is_open_supersedes_the_first() {
        let guard = CloseGuard::new();
        guard.arm();
        let first = ticket_of(guard.on_close_requested());
        let second = ticket_of(guard.on_close_requested());
        assert_ne!(first, second);
        // The stale watchdog must not close a window whose newer prompt is
        // still live.
        assert!(!guard.expired(first));
        assert!(guard.expired(second));
    }

    #[test]
    fn a_navigation_disarms_the_guard() {
        let guard = CloseGuard::new();
        guard.arm();
        // Ctrl+R onto a dev server that is no longer there: the new document
        // never calls `shell_ready`, so nothing would answer the prompt.
        guard.disarm();
        assert_eq!(guard.on_close_requested(), Decision::Close);
        // …and it applies again as soon as the UI comes back.
        guard.arm();
        assert!(matches!(guard.on_close_requested(), Decision::AskUi { .. }));
    }

    #[test]
    fn a_navigation_stands_a_pending_watchdog_down() {
        let guard = CloseGuard::new();
        guard.arm();
        let ticket = ticket_of(guard.on_close_requested());
        guard.disarm();
        // The next close passes through on its own; nothing needs forcing.
        assert!(!guard.expired(ticket));
    }
}
