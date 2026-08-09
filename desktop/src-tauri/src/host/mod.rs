//! Native window hosting: a real child window docked inside a panel rectangle.
//!
//! This is the Rust half of the Office host — the mechanism that will
//! eventually put a live Word window inside a dockview panel. It is built and
//! proven here against a **synthetic guest** ([`guest`]) rather than against
//! Word, because every hard part is independent of who owns the guest window:
//! class registration, style stripping, `SetParent`, geometry batching, focus
//! routing and teardown behave the same whether the window belongs to
//! `WINWORD.EXE` or to a 200-line test fixture. Proving it against a window we
//! create ourselves means the whole mechanism runs on any machine, in
//! `cargo test`, with no Office installed — and when Word arrives the only new
//! variable is Word.
//!
//! ```text
//!  Tauri window (main thread, owns the message loop)
//!  └── panel window        WS_CHILD, one per hosted document, our class
//!      └── clip child      WS_CHILD, the viewport
//!          └── guest       another process's top-level window, restyled
//!                          WS_CHILD and offset up by its own caption height
//! ```
//!
//! **Three windows, not one.** The panel window is the stable rectangle: the
//! layout writes to it, its WndProc hears `WM_PARENTNOTIFY` for the guest, and
//! it paints the panel surface while a launch is still in flight. The clip
//! child is the viewport whose only job is to cut off the strip of self-drawn
//! chrome the guest is offset by. Keeping those two apart means the offset
//! never leaks into the rectangle the rest of the code reasons about.
//!
//! **Everything Win32 that owns a window happens on the main thread**, because
//! that is the thread that owns the Tauri window and pumps its messages.
//! Creating our panel window anywhere else would put its WndProc on a thread
//! with no pump and attach yet another input queue to the pile.
//! [`main_thread::on_main`] enforces it, and — rather than trusting the
//! documented claim that a synchronous `#[tauri::command]` already runs there —
//! measures it: the first hop logs which path it took.
//!
//! **With exactly two deliberate exceptions, and neither of them touches a
//! window this thread owns.**
//!
//! *Guest geometry* ([`mover`]). Because a reparented guest's input queue is
//! attached to the main thread's, a wedged guest made every resize frame cost
//! ~1 s. Moving it from a thread that owns *no* window in the chain — and so is
//! attached to nothing — costs ~0.15 ms instead. That worker touches no window
//! of ours, creates none, and pumps nothing; it only ever posts `SetWindowPos`
//! at a handle. See [`mover`] for the measurement and for the ordering guarantee
//! that keeps a released window from being moved after it has gone back to the
//! desktop.
//!
//! *Waiting for a killed instance* ([`reaper`]). Reaping a guest is a job handle
//! closing — instant, and it stays on the main thread — followed by a poll until
//! the process is really gone, which is bounded at two seconds and used to run
//! inside `host_close`. Closing one docked document therefore froze the window
//! for as long as the instance took to die. The wait moved; the window calls
//! did not, and the one step that depends on the answer (handing the keyboard
//! back) comes back through [`main_thread::on_main`] when it is known.
//!
//! Everything else runs on the main thread, and the paths that *end* the window
//! are no exception: the close-ack watchdog in `lib.rs` runs on a thread of its
//! own and asks for the teardown rather than performing it (see
//! [`commands::shutdown`]).
//!
//! **What a docked window costs the keyboard.** A guest owns its own window
//! procedure, so from the moment the user clicks into it every keystroke is
//! Word's — the webview's `keydown` listeners, and with them the entire keymap,
//! are simply not in the delivery path. Nothing in `ui/` can answer that. The
//! only mechanism that still reaches us is a system-level hotkey, and [`escape`]
//! is it: `Ctrl+Alt+Home`, registered while a document is docked and released
//! with the last one.
//!
//! **What the guest process costs us.** A guest is launched into a Windows Job
//! Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and reaped by closing that
//! job, never by asking politely. This is not belt-and-braces: the spike that
//! preceded this PR measured a real Word surviving `Application.Quit` and dying
//! only when the job handle closed. See [`guest::GuestProcess`].
//!
//! **The seam this lines up with** is `HostBackend` in
//! `server/src/workbench_server/services/office_host/backend.py`: `launch`,
//! `embed`, `set_bounds`, `detach`, `close`, `poll`. The commands in
//! [`commands`] are that vocabulary, one call each, so the Python service can
//! drive this without a translation layer in between.

mod class;
/// Public so `tauri::generate_handler!` in `lib.rs` can name the commands by
/// their defining path — the macros it expands to live beside them, and a
/// re-export does not carry a macro.
pub mod commands;
#[cfg(debug_assertions)]
mod demo;
mod embed;
mod escape;
pub mod focus;
pub mod geometry;
pub mod guest;
#[cfg(test)]
mod hosting_tests;
pub mod layout;
mod main_thread;
pub mod mover;
mod reaper;
mod watchdog;

use std::collections::HashMap;
use std::fmt;
use std::sync::Mutex;

use serde::Serialize;

pub use commands::{
    host_close, host_detach, host_embed, host_focus, host_list, host_poll, host_set_bounds,
    host_set_visible, shutdown, EMBEDDED_EVENT, LOST_EVENT,
};
#[cfg(debug_assertions)]
pub use commands::{host_hang_guest, host_open_guest};
#[cfg(debug_assertions)]
pub use demo::start_demo_if_asked;
pub use main_thread::remember_main_thread;
pub use watchdog::spawn as spawn_watchdog;

use embed::EmbeddedGuest;
use geometry::{CssRect, HostLayout, PhysicalRect};
use guest::GuestProcess;
use layout::Coalescer;

/// An `HWND` as something that can live in a `HashMap` behind a `Mutex`.
///
/// `HWND` is a raw pointer and therefore neither `Send` nor `Sync`, which would
/// keep the whole registry out of Tauri's managed state. Storing the integer
/// and rebuilding the handle at the Win32 call site is the honest fix: the
/// value is only ever *used* on the main thread (see [`main_thread`]), and
/// nothing here dereferences it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub struct WindowId(pub isize);

#[cfg(windows)]
impl WindowId {
    pub fn hwnd(self) -> windows::Win32::Foundation::HWND {
        windows::Win32::Foundation::HWND(self.0 as *mut core::ffi::c_void)
    }

    pub fn from_hwnd(hwnd: windows::Win32::Foundation::HWND) -> Self {
        Self(hwnd.0 as isize)
    }

    pub fn is_null(self) -> bool {
        self.0 == 0
    }
}

/// Why a host operation refused.
///
/// The codes are chosen to line up with `HostReason` in
/// `server/src/workbench_server/models/office_host.py`, so the Python service
/// can map a failure to a terminal state without inventing a second vocabulary:
/// `embed_refused` and `launch_failed` are the same words on both sides, and
/// `window_gone` is what becomes `process_exited`/`crashed` there.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HostErrorCode {
    /// The rectangle the UI sent cannot be a window.
    InvalidRect,
    /// No host with that id (already closed, or never opened).
    UnknownHost,
    /// The guest window or its process is gone.
    WindowGone,
    /// The window exists and would not be reparented.
    EmbedRefused,
    /// A guest process could not be started.
    LaunchFailed,
    /// A Win32 call refused; the message carries its own text.
    Win32,
    /// The main thread did not run the work in time.
    MainThread,
    /// Not this platform, or not this build.
    Unsupported,
}

/// One refusal, in the shape a `#[tauri::command]` can return and the UI can
/// switch on.
#[derive(Debug, Clone, Serialize)]
pub struct HostError {
    pub code: HostErrorCode,
    pub message: String,
}

impl HostError {
    pub fn new(code: HostErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    pub fn invalid_rect(message: impl Into<String>) -> Self {
        Self::new(HostErrorCode::InvalidRect, message)
    }

    pub fn unknown_host(host_id: &str) -> Self {
        Self::new(
            HostErrorCode::UnknownHost,
            format!("no hosted panel with id {host_id}"),
        )
    }

    pub fn window_gone(message: impl Into<String>) -> Self {
        Self::new(HostErrorCode::WindowGone, message)
    }

    pub fn embed_refused(message: impl Into<String>) -> Self {
        Self::new(HostErrorCode::EmbedRefused, message)
    }
}

impl fmt::Display for HostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}: {}", self.code, self.message)
    }
}

impl std::error::Error for HostError {}

#[cfg(windows)]
impl From<windows::core::Error> for HostError {
    fn from(err: windows::core::Error) -> Self {
        Self::new(HostErrorCode::Win32, err.to_string())
    }
}

/// One hosted panel: our two windows, the guest inside them, and the process
/// behind it.
struct Panel {
    /// Our panel window (child of the Tauri window).
    window: WindowId,
    /// Our clip child (child of `window`).
    clip: WindowId,
    /// The guest, once embedded, with everything needed to put it back.
    guest: Option<EmbeddedGuest>,
    /// The process we launched, if we launched one. Held to be dropped: its
    /// job object is what actually reaps the guest.
    process: Option<GuestProcess>,
    /// Last applied layout, so a resize storm collapses to the frames that
    /// actually changed something.
    coalescer: Coalescer,
    /// Self-drawn caption to hide, in **CSS pixels** — the unit the UI sent it
    /// in, kept that way on purpose.
    ///
    /// Storing the scaled value would make this panel a second DPI authority
    /// frozen at the moment of the embed, which is the one thing
    /// [`geometry`] exists to prevent. The physical inset is re-derived from
    /// the scale factor in force, on every layout: see [`Panel::layout_at`].
    caption_inset_css: f64,
}

impl Panel {
    /// The layout this panel wants for `rect` at the scale in force **now**.
    ///
    /// Nothing derived from a scale factor is cached across calls. A window
    /// dragged between two monitors at different DPI gets a resize, and this is
    /// the call that has to answer it with different physical numbers for the
    /// same CSS rectangle — the caption inset included.
    fn layout_at(&self, rect: CssRect, scale: f64) -> Result<HostLayout, HostError> {
        layout_for(rect, scale, self.caption_inset_css)
    }
}

/// Every hosted panel this window owns.
///
/// Lives in Tauri's managed state for the life of the process. The `Mutex` is
/// never held across a Win32 call that could block: everything inside runs on
/// the main thread, and the only other toucher is shutdown.
pub struct HostRegistry {
    panels: Mutex<HashMap<String, Panel>>,
}

impl HostRegistry {
    pub fn new() -> Self {
        Self {
            panels: Mutex::new(HashMap::new()),
        }
    }
}

impl Default for HostRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// What `host_list` reports: enough to see a leak, not enough to be a second
/// source of truth about the lifecycle (that is the Python service's job).
#[derive(Debug, Clone, Serialize)]
pub struct HostSnapshot {
    pub host_id: String,
    pub window: WindowId,
    pub guest: Option<WindowId>,
    pub pid: Option<u32>,
    pub layout: Option<HostLayout>,
}

/// What an embed reports back: the geometry actually applied, in physical
/// pixels, so a caller can assert on it instead of trusting it.
#[derive(Debug, Clone, Serialize)]
pub struct HostGeometry {
    pub host_id: String,
    pub window: WindowId,
    pub guest: WindowId,
    pub layout: HostLayout,
    /// The scale factor used — the single DPI authority, reported so a bug
    /// report can say which one it was.
    pub scale: f64,
}

/// Turn a CSS rectangle into the three physical rectangles a panel needs, at
/// one scale factor.
///
/// The only place the two unit systems meet, and that now includes the caption
/// inset: rectangle and inset are converted here, from the same number, on
/// every call. Anything that converted one of them earlier and kept the result
/// would be the second DPI authority this module exists to avoid.
fn layout_for(rect: CssRect, scale: f64, caption_inset_css: f64) -> Result<HostLayout, HostError> {
    let panel: PhysicalRect = PhysicalRect::from_css(rect, scale)?;
    Ok(geometry::host_layout(
        panel,
        geometry::caption_inset_px(caption_inset_css, scale),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn panel_with_inset(caption_inset_css: f64) -> Panel {
        Panel {
            window: WindowId(0),
            clip: WindowId(0),
            guest: None,
            process: None,
            coalescer: Coalescer::default(),
            caption_inset_css,
        }
    }

    #[test]
    fn one_scale_factor_drives_every_rectangle() {
        let rect = CssRect {
            x: 100.0,
            y: 50.0,
            width: 400.0,
            height: 300.0,
        };
        let layout = layout_for(rect, 1.5, 20.0).unwrap();
        assert_eq!(layout.panel.x, 150);
        assert_eq!(layout.panel.width, 600);
        assert_eq!(layout.clip.width, 600);
        // The inset goes through that same factor, here, rather than arriving
        // pre-scaled from somewhere that used a different one.
        assert_eq!(layout.guest.y, -30);
    }

    #[test]
    fn a_panel_re_derives_its_inset_when_the_scale_changes() {
        // The registry entry a `host_set_bounds` reads. Embedded on a 200%
        // monitor, then the window is dragged to a 100% one and dockview sends
        // a resize: the same CSS rectangle, a different scale factor, and the
        // inset has to follow it. Holding a physical inset here gave 28 twice.
        let panel = panel_with_inset(14.0);
        let rect = CssRect {
            x: 0.0,
            y: 0.0,
            width: 400.0,
            height: 300.0,
        };
        assert_eq!(panel.layout_at(rect, 2.0).unwrap().guest.y, -28);
        assert_eq!(panel.layout_at(rect, 1.0).unwrap().guest.y, -14);
        // And back, because a monitor move has no preferred direction.
        assert_eq!(panel.layout_at(rect, 2.0).unwrap().guest.y, -28);
    }

    #[test]
    fn an_unusable_rectangle_never_reaches_win32() {
        let rect = CssRect {
            x: 0.0,
            y: 0.0,
            width: 0.0,
            height: 300.0,
        };
        let err = layout_for(rect, 1.0, 0.0).unwrap_err();
        assert_eq!(err.code, HostErrorCode::InvalidRect);
    }

    #[test]
    fn error_codes_serialize_as_the_python_reasons() {
        let json = serde_json::to_string(&HostError::embed_refused("no")).unwrap();
        assert!(json.contains("\"embed_refused\""), "{json}");
        let json = serde_json::to_string(&HostError::unknown_host("h1")).unwrap();
        assert!(json.contains("\"unknown_host\""), "{json}");
    }
}
