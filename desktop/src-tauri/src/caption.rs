//! The native caption, in Workbench's colours.
//!
//! The literal first pixel of the product is a strip Windows draws, and until
//! this module existed it drew it in the system's palette: a stock grey caption
//! reading "Workbench" above a graphite app. No stylesheet can reach it — it is
//! outside the document, outside the webview, and outside the process that
//! composes it.
//!
//! **Tint, do not replace.** Windows 11 exposes three DWM window attributes
//! that change what the window manager paints the frame *with* while leaving it
//! to paint the frame: `DWMWA_BORDER_COLOR` (34), `DWMWA_CAPTION_COLOR` (35) and
//! `DWMWA_TEXT_COLOR` (36) — verified against `windows` 0.61's
//! `Win32::Graphics::Dwm`, not taken from prose. So dragging, snapping,
//! double-click-to-maximise, the system menu, Aero Shake, the snap-layouts
//! flyout and the three window buttons all keep working because they are still
//! the real ones. The alternative — an undecorated window with our own drag
//! region and our own buttons — reimplements every one of those, and each is a
//! chance to get a detail subtly wrong.
//!
//! **The colours are the UI's, not ours.** They arrive from `ui/src/captionTint.ts`
//! as three resolved design tokens, because `tokens.css` is the single source of
//! truth for colour in this product and a hex constant here would be a second
//! palette that no theme toggle can reach. This module therefore knows nothing
//! about themes: it is told three colours and paints them.
//!
//! **One decision is ours**, and it is a contrast decision rather than a
//! preference: the minimise/maximise/close **glyphs are not covered by
//! `DWMWA_TEXT_COLOR`**. DWM draws them from the window's immersive light/dark
//! mode, so a dark caption on a machine running Windows in light mode gets
//! near-black glyphs on a near-black strip. [`Colors::parse`] therefore derives
//! `DWMWA_USE_IMMERSIVE_DARK_MODE` (20) from the caption's own relative
//! luminance — from the colour, not from the theme's name, so it stays right
//! through a palette change.
//!
//! **There is no reading these back.** Measured, so that nobody re-derives it:
//! `DwmGetWindowAttribute(DWMWA_CAPTION_COLOR)` answers `E_INVALIDARG` on
//! Windows 11 26200 — the colour attributes are write-only, and the documented
//! gettable set is `NCRENDERING_ENABLED`, `CAPTION_BUTTON_BOUNDS`,
//! `EXTENDED_FRAME_BOUNDS` and `CLOAKED`. So the evidence that a tint landed is
//! the `HRESULT` of the set (asserted in `window_tests` below) and the log line
//! [`apply_and_log`] writes; the evidence that it broke nothing is the window
//! *state* read back afterwards — styles, hit-testing, system menu.
//!
//! **Degrading is silent.** On Windows 10 attributes 34–36 do not exist and the
//! call fails with `E_INVALIDARG`; that is a line in the shell log and nothing
//! else. Attribute 20 is older (Windows 10 20H1), so it is attempted even when
//! the three colours are refused — a dark system caption over a dark app is a
//! better fallback than a light one, and it costs one call. Nothing here can
//! fail a startup: the command returns `()`.

use std::path::{Path, PathBuf};

use serde::Deserialize;

/// The payload of `set_caption_tint`, mirroring `CaptionTint` in `ui/src/shell.ts`.
///
/// Strings rather than numbers on purpose: what the UI has is a CSS token
/// value, and a hex string is the form both sides can read in a log line.
#[derive(Debug, Clone, Deserialize)]
pub struct CaptionTint {
    /// Caption background (`--surface-app`).
    pub caption: String,
    /// The window title (`--text-secondary`).
    pub text: String,
    /// The window's outer border (`--border-strong`).
    pub border: String,
}

/// Why a tint did not land.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TintError {
    /// A field was not `#rrggbb`. Nothing reached Win32 — an all-or-nothing
    /// refusal, because a frame wearing two of our colours and one of Windows'
    /// reads as a rendering bug rather than as a default.
    BadColor { field: &'static str, value: String },
    /// This window has no `HWND` (not Windows, or Tauri could not produce one).
    NoWindow(String),
    /// The DWM refused an attribute — a Windows build that predates it. Names
    /// the first refusal; the ones after it were attempted anyway.
    Unsupported {
        attribute: &'static str,
        detail: String,
    },
}

impl std::fmt::Display for TintError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BadColor { field, value } => {
                write!(f, "caption tint field {field} is not a colour: {value:?}")
            }
            Self::NoWindow(detail) => write!(f, "no window to tint: {detail}"),
            Self::Unsupported { attribute, detail } => write!(
                f,
                "this Windows build refused {attribute} ({detail}); the caption stays the system's"
            ),
        }
    }
}

/// Relative luminance at which black and white text contrast equally.
///
/// WCAG 2.1 contrast is `(L1 + 0.05) / (L2 + 0.05)`, so a background contrasts
/// with white and with black by the same ratio when `(L + 0.05)² = 1.05 × 0.05`
/// — i.e. `L ≈ 0.1791`. Below it, light-on-dark wins. This is the standard flip
/// point rather than a taste call, which is what lets it survive a palette
/// change nobody re-tunes it for.
const DARK_BELOW: f64 = 0.179_129_9;

/// A validated tint: what actually goes to the DWM.
///
/// Every field is derived here, once, so the Win32 half has no parsing, no
/// decisions and nothing to get wrong — and so all of it is testable on a
/// machine with no window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Colors {
    pub caption: u32,
    pub text: u32,
    pub border: u32,
    /// Whether the window buttons must be drawn light-on-dark. See the module
    /// note: `DWMWA_TEXT_COLOR` does not reach them.
    pub dark: bool,
}

impl Colors {
    pub fn parse(tint: &CaptionTint) -> Result<Self, TintError> {
        let caption = field("caption", &tint.caption)?;
        Ok(Self {
            caption: colorref(caption),
            text: colorref(field("text", &tint.text)?),
            border: colorref(field("border", &tint.border)?),
            dark: relative_luminance(caption) < DARK_BELOW,
        })
    }
}

fn field(name: &'static str, value: &str) -> Result<[u8; 3], TintError> {
    rgb(value).ok_or_else(|| TintError::BadColor {
        field: name,
        value: value.to_owned(),
    })
}

/// `#rrggbb` → its three channels. Nothing else is accepted: the UI already
/// normalises, and a lenient parser here would be a second normaliser to keep
/// honest.
pub fn rgb(value: &str) -> Option<[u8; 3]> {
    let hex = value.strip_prefix('#')?;
    if hex.len() != 6 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let byte = |at: usize| u8::from_str_radix(&hex[at..at + 2], 16).ok();
    Some([byte(0)?, byte(2)?, byte(4)?])
}

/// Channels → a Win32 `COLORREF`, which is `0x00BBGGRR`.
///
/// The byte order is the whole reason this is a named function with a test:
/// `#14161A` is `0x001A1614`, and getting it backwards produces a plausible
/// colour that is simply the wrong one — the failure a reviewer's eye cannot
/// catch from the diff.
pub fn colorref([r, g, b]: [u8; 3]) -> u32 {
    u32::from(r) | (u32::from(g) << 8) | (u32::from(b) << 16)
}

/// WCAG 2.1 relative luminance of a colour, 0.0 (black) to 1.0 (white).
pub fn relative_luminance([r, g, b]: [u8; 3]) -> f64 {
    let channel = |value: u8| {
        let c = f64::from(value) / 255.0;
        if c <= 0.040_45 {
            c / 12.92
        } else {
            ((c + 0.055) / 1.055).powf(2.4)
        }
    };
    0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

/// Tint `window`'s frame, and say what happened.
///
/// Callers log the result; nothing here is an error a user should be shown.
pub fn apply(window: &tauri::Window, tint: &CaptionTint) -> Result<Colors, TintError> {
    let colors = Colors::parse(tint)?;
    apply_colors(window, colors)?;
    Ok(colors)
}

#[cfg(windows)]
fn apply_colors(window: &tauri::Window, colors: Colors) -> Result<(), TintError> {
    let hwnd = window
        .hwnd()
        .map_err(|err| TintError::NoWindow(err.to_string()))?;
    win::tint(hwnd, colors)
}

#[cfg(not(windows))]
fn apply_colors(_window: &tauri::Window, _colors: Colors) -> Result<(), TintError> {
    Err(TintError::Unsupported {
        attribute: "DWMWA_CAPTION_COLOR",
        detail: "not a Windows build".to_owned(),
    })
}

// ---- remembering it across runs ---------------------------------------------
//
// Because "on startup" was not startup. The UI can only send a tint once its
// bundle has been fetched, evaluated and mounted — measured at +2.5 s against
// a cold Vite dev server, +0.18 s warm — and the window is on screen from the
// first moment. Until the tint arrived, the literal first pixel of the product
// was the stock caption this module exists to remove.
//
// So the last tint is written to disk and re-applied the instant a window
// exists (`restore_tint`, from `RunEvent::Ready`), before the webview has
// finished loading. It is a *cache of the UI's own value*, never a second
// palette: nothing here invents a colour, a first-ever run simply has none,
// and a palette change is one launch behind for the few hundred milliseconds
// until the real tint lands.
//
// The read is not on the event loop. `RunEvent::Ready` runs on the main
// thread, which is the window's message pump, and this file lives in the
// user's app-config directory — a roaming profile, a network-mounted
// `AppData`, or a path a virus scanner has open at that exact moment. None of
// those has a bound, and a window that will not repaint while one of them
// resolves is a worse failure than the caption flash this section exists to
// remove. Same reasoning that put `backend::start()` on a thread of its own.

/// The cache's name inside the app's own config directory.
///
/// One line of three hex colours rather than JSON: it costs no dependency, the
/// same [`rgb`] parser validates it, and a human opening the file can see
/// exactly what the window will be born wearing.
const CACHE_FILE: &str = "caption-tint.txt";

fn cache_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    use tauri::Manager;
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join(CACHE_FILE))
}

fn remember(app: &tauri::AppHandle, tint: &CaptionTint) {
    let Some(path) = cache_path(app) else { return };
    if let Some(dir) = path.parent() {
        if std::fs::create_dir_all(dir).is_err() {
            return;
        }
    }
    // A failed write costs the *next* launch a caption flash and nothing else,
    // so it is deliberately not reported: there is no action to take.
    let _ = std::fs::write(
        &path,
        format!("{} {} {}", tint.caption, tint.text, tint.border),
    );
}

/// Parse what [`remember`] wrote. `None` for anything else, including no file.
pub fn parse_cached(contents: &str) -> Option<CaptionTint> {
    let mut parts = contents.split_whitespace();
    let mut next = || {
        let value = parts.next()?;
        rgb(value).map(|_| value.to_owned())
    };
    let tint = CaptionTint {
        caption: next()?,
        text: next()?,
        border: next()?,
    };
    parts.next().is_none().then_some(tint)
}

/// The cached tint, or `None`.
///
/// No file (the first-ever run), a file that will not open, or contents that
/// are not three colours — all of them mean the same thing, which is that the
/// window is born wearing the system's caption and nothing is wrong.
fn read_cached(path: &Path) -> Option<CaptionTint> {
    parse_cached(&std::fs::read_to_string(path).ok()?)
}

/// Find and read the cache away from the calling thread, then hand what it
/// found to `paint`.
///
/// `locate` runs on the reader too, so *nothing* in the chain touches the
/// caller: not resolving the app-config directory, not the read itself. That
/// split is the whole reason this is a function — see the section note above
/// for why the event loop must not be the thread that waits on this disk.
///
/// Returns the reader's handle so a test can wait for it. The shell drops it:
/// there is nothing to join, and nothing that a failure here would change.
fn read_off_thread<L, P>(locate: L, paint: P) -> std::thread::JoinHandle<()>
where
    L: FnOnce() -> Option<PathBuf> + Send + 'static,
    P: FnOnce(CaptionTint) + Send + 'static,
{
    std::thread::spawn(move || {
        if let Some(tint) = locate().and_then(|path| read_cached(&path)) {
            paint(tint);
        }
    })
}

/// Paint the window the moment it exists, from the last run's colours.
///
/// Called from `RunEvent::Ready`, which is the earliest point at which there is
/// a window to paint — Tauri creates the configured window only after `setup`
/// returns. The disk work happens on a thread of its own and only the painting
/// is posted back to the main one: the read is the unbounded half, while
/// `DwmSetWindowAttribute` is neither slow nor ours to make thread-safe, and
/// `host/main_thread.rs` already sets the house rule that Win32 window work
/// runs on the thread that owns the window.
///
/// Silent in every failure direction: no cache, no window, an event loop that
/// has already gone, or a DWM that refuses — and the caption is simply the
/// system's until the UI sends the real one a moment later.
pub fn restore_tint(app: &tauri::AppHandle) {
    let reader = app.clone();
    let painter = app.clone();
    read_off_thread(
        move || cache_path(&reader),
        move |tint| {
            let app = painter.clone();
            if let Err(err) = painter.run_on_main_thread(move || pre_tint(&app, &tint)) {
                crate::backend::log(&format!("caption not pre-tinted: {err}"));
            }
        },
    );
}

/// The painting half of [`restore_tint`], on the main thread it posts to.
fn pre_tint(app: &tauri::AppHandle, tint: &CaptionTint) {
    use tauri::Manager;
    let Some(webview) = app.get_webview_window("main") else {
        return;
    };
    match apply(&webview.as_ref().window(), tint) {
        Ok(colors) => crate::backend::log(&format!(
            "caption pre-tinted from the last run: caption={:#08X}",
            colors.caption
        )),
        Err(err) => crate::backend::log(&format!("caption not pre-tinted: {err}")),
    }
}

/// Apply and log — the whole body of the `set_caption_tint` command.
///
/// A refusal is a log line and nothing more: the caption keeps the system's
/// colours, which is exactly the product as it shipped yesterday.
pub fn apply_and_log(window: &tauri::Window, tint: &CaptionTint) {
    use tauri::Manager;
    // Remembered on the way in, not on success: the file records what the UI
    // asked for, and a build that refuses the attributes today will refuse the
    // pre-tint the same way tomorrow — at the same cost of one log line.
    remember(window.app_handle(), tint);
    match apply(window, tint) {
        Ok(colors) => crate::backend::log(&format!(
            "caption tinted: caption={:#08X} text={:#08X} border={:#08X} buttons={}",
            colors.caption,
            colors.text,
            colors.border,
            if colors.dark {
                "light-on-dark"
            } else {
                "dark-on-light"
            }
        )),
        Err(err) => crate::backend::log(&format!("caption not tinted: {err}")),
    }
}

#[cfg(windows)]
mod win {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::Graphics::Dwm::{
        DwmSetWindowAttribute, DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR,
        DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWINDOWATTRIBUTE,
    };

    use super::{Colors, TintError};

    /// Paint all four attributes, reporting the first refusal.
    ///
    /// Deliberately not short-circuiting. `DWMWA_USE_IMMERSIVE_DARK_MODE` is
    /// four years older than the colour attributes, so on the build where the
    /// colours are refused it is the one thing that still lands — and a dark
    /// system caption above a dark app is a strictly better fallback than a
    /// light one. Whether *any* of them landed is a log line, not a branch:
    /// there is no partial state to unwind, since a refused attribute simply
    /// leaves the frame as the window manager already had it.
    pub(super) fn tint(hwnd: HWND, colors: Colors) -> Result<(), TintError> {
        let mut first = None;
        for (attribute, name, value) in [
            (DWMWA_CAPTION_COLOR, "DWMWA_CAPTION_COLOR", colors.caption),
            (DWMWA_TEXT_COLOR, "DWMWA_TEXT_COLOR", colors.text),
            (DWMWA_BORDER_COLOR, "DWMWA_BORDER_COLOR", colors.border),
            (
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                "DWMWA_USE_IMMERSIVE_DARK_MODE",
                u32::from(colors.dark),
            ),
        ] {
            if let Err(err) = set(hwnd, attribute, value) {
                first.get_or_insert(TintError::Unsupported {
                    attribute: name,
                    detail: err.to_string(),
                });
            }
        }
        match first {
            Some(err) => Err(err),
            None => Ok(()),
        }
    }

    /// One `DwmSetWindowAttribute`. Every attribute here takes a 32-bit value —
    /// a `COLORREF` or a `BOOL` — so one setter covers all four.
    fn set(hwnd: HWND, attribute: DWMWINDOWATTRIBUTE, value: u32) -> windows::core::Result<()> {
        // SAFETY: `value` outlives the call, and `size_of` is the size the DWM
        // is told to read from it. `hwnd` is only read.
        unsafe {
            DwmSetWindowAttribute(
                hwnd,
                attribute,
                std::ptr::addr_of!(value).cast(),
                std::mem::size_of::<u32>() as u32,
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{mpsc, Arc, Mutex};

    use super::*;

    fn tint(caption: &str) -> CaptionTint {
        CaptionTint {
            caption: caption.to_owned(),
            text: "#a8b0bc".to_owned(),
            border: "#3d4450".to_owned(),
        }
    }

    #[test]
    fn a_colorref_puts_blue_in_the_high_byte() {
        // The bug this exists to catch: `#14161A` is a near-black graphite, and
        // the byte-swapped reading of it (0x0014161A) is a *blue*. Both look
        // like plausible constants in a diff.
        assert_eq!(colorref(rgb("#14161A").unwrap()), 0x001A_1614);
        assert_eq!(colorref(rgb("#FF0000").unwrap()), 0x0000_00FF);
        assert_eq!(colorref(rgb("#0000FF").unwrap()), 0x00FF_0000);
        // Never sets the high byte, which is where DWM's sentinel values
        // (`DWMWA_COLOR_DEFAULT`/`_NONE`) live.
        assert_eq!(colorref([0xFF, 0xFF, 0xFF]) & 0xFF00_0000, 0);
    }

    #[test]
    fn only_a_six_digit_hex_colour_parses() {
        assert_eq!(rgb("#a8b0bc"), Some([0xA8, 0xB0, 0xBC]));
        assert_eq!(rgb("#A8B0BC"), Some([0xA8, 0xB0, 0xBC]));
        for bad in ["", "#abc", "a8b0bc", "#a8b0bcff", "#gggggg", "rgb(1,2,3)"] {
            assert_eq!(rgb(bad), None, "{bad}");
        }
    }

    #[test]
    fn a_bad_field_refuses_the_whole_tint_and_names_itself() {
        let err = Colors::parse(&tint("not a colour")).unwrap_err();
        assert_eq!(
            err,
            TintError::BadColor {
                field: "caption",
                value: "not a colour".to_owned()
            }
        );
        // And the message says which field, so the log is actionable.
        assert!(err.to_string().contains("caption"), "{err}");
    }

    #[test]
    fn the_button_glyphs_follow_the_caption_rather_than_the_theme_name() {
        // Palette-independent on purpose: asserting today's `--surface-app`
        // values here would bake the palette into Rust, which is the exact
        // thing this module exists to avoid.
        assert!(Colors::parse(&tint("#000000")).unwrap().dark);
        assert!(!Colors::parse(&tint("#ffffff")).unwrap().dark);
        // Either side of the equal-contrast flip point (L ≈ 0.179): #767676 is
        // the darkest grey that still reads better with black glyphs.
        assert!(!Colors::parse(&tint("#767676")).unwrap().dark);
        assert!(Colors::parse(&tint("#757575")).unwrap().dark);
    }

    #[test]
    fn luminance_is_wcag_relative_luminance() {
        assert!((relative_luminance([0, 0, 0]) - 0.0).abs() < 1e-12);
        assert!((relative_luminance([255, 255, 255]) - 1.0).abs() < 1e-12);
        // The green channel carries ~71% of the weight, blue ~7%.
        assert!(relative_luminance([0, 255, 0]) > relative_luminance([255, 0, 0]));
        assert!(relative_luminance([255, 0, 0]) > relative_luminance([0, 0, 255]));
    }

    #[test]
    fn the_cache_round_trips_through_the_line_it_writes() {
        // The two halves are written apart (`remember` formats, `parse_cached`
        // reads), so the format is asserted from both ends rather than assumed.
        let tint = CaptionTint {
            caption: "#14161a".to_owned(),
            text: "#a8b0bc".to_owned(),
            border: "#3d4450".to_owned(),
        };
        let line = format!("{} {} {}", tint.caption, tint.text, tint.border);
        let back = parse_cached(&line).expect("a tint");
        assert_eq!(Colors::parse(&back).unwrap(), Colors::parse(&tint).unwrap());
    }

    #[test]
    fn a_cache_that_is_not_three_colours_pre_tints_nothing() {
        // The file is in the user's config directory, so it can be anything at
        // all by the time we read it: truncated by a crash, hand-edited, or
        // left over from a format that no longer exists. Every one of those has
        // to mean "no pre-tint", never a half-painted frame.
        for bad in [
            "",
            "   ",
            "#14161a",
            "#14161a #a8b0bc",
            "#14161a #a8b0bc #3d4450 #extra",
            "#14161a #a8b0bc notacolour",
            "{\"caption\":\"#14161a\"}",
        ] {
            assert!(parse_cached(bad).is_none(), "{bad:?}");
        }
    }

    /// A directory of this module's own, in `backend.rs`'s style: a real path
    /// on a real disk without a dev-dependency for it, removed on the way out.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("wb-caption-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("a scratch directory");
        dir
    }

    #[test]
    fn nothing_in_the_restore_chain_runs_on_the_thread_that_asks_for_it() {
        // `restore_tint` is called from `RunEvent::Ready` — the event loop, and
        // so the window's message pump. Reading there stalls the pump for as
        // long as the disk takes, and this file lives in the user's app-config
        // directory: a roaming profile, a network-mounted AppData, or a path AV
        // has open. `locate` is asserted alongside the read because resolving
        // that directory is part of the same chain.
        let dir = scratch("off-thread");
        let path = dir.join(CACHE_FILE);
        std::fs::write(&path, "#14161a #a8b0bc #3d4450").unwrap();

        let here = std::thread::current().id();
        let threads = Arc::new(Mutex::new(Vec::new()));
        let (located, painted) = (Arc::clone(&threads), Arc::clone(&threads));
        let (tx, rx) = mpsc::channel();
        read_off_thread(
            move || {
                located.lock().unwrap().push(std::thread::current().id());
                Some(path)
            },
            move |tint| {
                painted.lock().unwrap().push(std::thread::current().id());
                tx.send(tint).unwrap();
            },
        )
        .join()
        .expect("the reader thread");

        // It still reads the colours it was given — moving the work must not
        // quietly lose it.
        let tint = rx.recv().expect("the cached tint");
        assert_eq!(Colors::parse(&tint).unwrap().caption, 0x001A_1614);

        let threads = threads.lock().unwrap();
        assert_eq!(threads.len(), 2, "locate and paint both ran: {threads:?}");
        assert!(
            threads.iter().all(|id| *id != here),
            "the cache was touched on the caller's thread: {threads:?} vs {here:?}"
        );

        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_cache_that_is_missing_or_junk_paints_nothing() {
        let dir = scratch("no-paint");
        let path = dir.join(CACHE_FILE);
        // Never written: the first-ever run, which must not be an error.
        assert!(read_cached(&path).is_none());
        // Written, but not three colours — same outcome, no half-painted frame.
        std::fs::write(&path, "#14161a #a8b0bc").unwrap();
        assert!(read_cached(&path).is_none());

        // And end to end: neither "no cache directory at all" nor a junk file
        // reaches `paint`, so `restore_tint` has nothing to post to the window.
        for locate in [None, Some(path)] {
            let painted = Arc::new(AtomicBool::new(false));
            let flag = Arc::clone(&painted);
            read_off_thread(move || locate, move |_| flag.store(true, Ordering::SeqCst))
                .join()
                .expect("the reader thread");
            assert!(!painted.load(Ordering::SeqCst));
        }

        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_payload_deserializes_from_what_the_ui_sends() {
        // The wire shape, verbatim from `setCaptionTint` in `ui/src/shell.ts`.
        // `r##"…"##`, because the payload itself contains `"#`.
        let tint: CaptionTint =
            serde_json::from_str(r##"{"caption":"#14161a","text":"#a8b0bc","border":"#3d4450"}"##)
                .unwrap();
        let colors = Colors::parse(&tint).unwrap();
        assert_eq!(colors.caption, 0x001A_1614);
        assert!(colors.dark);
    }
}

/// The half that needs a real window on a real desktop.
///
/// Same bargain as `host::hosting_tests`: these create a window, so they need a
/// window station (any normal Windows login). They are what makes "tinting does
/// not disturb the native frame" a checked claim rather than an argument — the
/// window is interrogated *after* the tint for the exact bits that dragging,
/// snapping, maximising and the system menu are built on.
#[cfg(all(windows, test))]
mod window_tests {
    use windows::core::{w, PCWSTR};
    use windows::Win32::Foundation::{LPARAM, RECT, WPARAM};
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DestroyWindow, GetSystemMenu, GetWindowLongPtrW,
        GetWindowRect, RegisterClassExW, SendMessageW, GWL_STYLE, HTCAPTION, WINDOW_EX_STYLE,
        WINDOW_STYLE, WM_NCHITTEST, WNDCLASSEXW, WS_CAPTION, WS_MAXIMIZEBOX, WS_MINIMIZEBOX,
        WS_OVERLAPPEDWINDOW, WS_SYSMENU, WS_THICKFRAME,
    };

    use super::*;

    /// The class the fixture window is created from.
    ///
    /// A class of our own, whose procedure is `DefWindowProcW` and nothing else,
    /// because `DefWindowProcW` is the code that turns the style bits into
    /// `HTCAPTION` — the very thing under test. The predefined `STATIC` class
    /// was tried first and is wrong for this: a static control answers
    /// `WM_NCHITTEST` with `HTTRANSPARENT` whatever its styles say, which would
    /// have made the assertion below unable to fail.
    fn class_atom() -> u16 {
        static ATOM: std::sync::OnceLock<u16> = std::sync::OnceLock::new();
        *ATOM.get_or_init(|| {
            unsafe extern "system" fn proc(
                hwnd: windows::Win32::Foundation::HWND,
                msg: u32,
                wparam: WPARAM,
                lparam: LPARAM,
            ) -> windows::Win32::Foundation::LRESULT {
                unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
            }
            // SAFETY: the descriptor is read for the duration of the call and
            // the class name is a static wide literal.
            let atom = unsafe {
                let instance = GetModuleHandleW(None).expect("this module's instance");
                RegisterClassExW(&WNDCLASSEXW {
                    cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
                    lpfnWndProc: Some(proc),
                    hInstance: instance.into(),
                    lpszClassName: w!("WorkbenchCaptionTintTest"),
                    ..Default::default()
                })
            };
            assert_ne!(atom, 0, "{}", windows::core::Error::from_win32());
            atom
        })
    }

    /// A real top-level window with a real caption, never shown.
    ///
    /// Not shown on purpose: `WM_NCHITTEST` and the style bits are pure
    /// geometry and state, so visibility buys nothing, and a test suite must
    /// not flash windows across the desktop of whoever is running it.
    struct Frame(windows::Win32::Foundation::HWND);

    impl Frame {
        fn new() -> Self {
            let atom = class_atom();
            let hwnd = unsafe {
                CreateWindowExW(
                    WINDOW_EX_STYLE(0),
                    PCWSTR(atom as usize as *const u16),
                    w!("Workbench caption tint test"),
                    WS_OVERLAPPEDWINDOW,
                    100,
                    100,
                    640,
                    400,
                    None,
                    None,
                    None,
                    None,
                )
            }
            .expect("a top-level window");
            assert!(!hwnd.0.is_null());
            Self(hwnd)
        }

        fn style(&self) -> WINDOW_STYLE {
            WINDOW_STYLE(unsafe { GetWindowLongPtrW(self.0, GWL_STYLE) } as u32)
        }

        /// What the window says a point inside its caption strip is.
        fn hit_test_caption(&self) -> u32 {
            let mut rect = RECT::default();
            unsafe { GetWindowRect(self.0, &mut rect) }.expect("a window rectangle");
            // Halfway across the frame, a few pixels below its top edge: inside
            // the caption, clear of the resize border and of the buttons.
            let x = (rect.left + rect.right) / 2;
            let y = rect.top + 12;
            let packed = ((y as u32 as isize) << 16) | (x as u32 as isize & 0xFFFF);
            let hit = unsafe {
                SendMessageW(self.0, WM_NCHITTEST, Some(WPARAM(0)), Some(LPARAM(packed)))
            };
            hit.0 as u32
        }
    }

    impl Drop for Frame {
        fn drop(&mut self) {
            let _ = unsafe { DestroyWindow(self.0) };
        }
    }

    fn tint(caption: &str, text: &str) -> CaptionTint {
        CaptionTint {
            caption: caption.to_owned(),
            text: text.to_owned(),
            border: "#3d4450".to_owned(),
        }
    }

    /// What this machine did with the attributes, printed rather than asserted.
    ///
    /// `Ok` on Windows 11 (22000+), `Unsupported` below it. Asserting one of
    /// them would be asserting the OS version of whoever runs the suite; the
    /// contract is that neither outcome panics and neither changes the window.
    fn report(label: &str, outcome: &Result<(), TintError>) {
        match outcome {
            Ok(()) => println!("{label}: every DWM attribute accepted"),
            Err(err) => println!("{label}: {err}"),
        }
    }

    #[test]
    fn tinting_a_real_window_leaves_every_native_frame_behaviour_intact() {
        let frame = Frame::new();
        let before = frame.style();
        assert_eq!(frame.hit_test_caption(), HTCAPTION);

        let outcome =
            super::win::tint(frame.0, Colors::parse(&tint("#14161a", "#a8b0bc")).unwrap());
        report("dark tint", &outcome);

        // The point of tinting rather than replacing: Windows still owns the
        // frame afterwards. Each bit below is a behaviour the brief demanded
        // keep working — `WS_CAPTION` the strip itself, `WS_SYSMENU` the system
        // menu and the close button, the two box bits minimise/maximise (and so
        // double-click-to-maximise), `WS_THICKFRAME` resizing and snapping.
        assert_eq!(frame.style(), before);
        assert!(before.contains(WS_CAPTION));
        assert!(before.contains(WS_SYSMENU));
        assert!(before.contains(WS_MINIMIZEBOX));
        assert!(before.contains(WS_MAXIMIZEBOX));
        assert!(before.contains(WS_THICKFRAME));
        // Dragging and double-click-to-maximise are both "the caption was hit".
        assert_eq!(frame.hit_test_caption(), HTCAPTION);
        // The system menu is still there to be opened (Alt+Space, right-click).
        assert!(!unsafe { GetSystemMenu(frame.0, false) }.is_invalid());
    }

    #[test]
    fn a_theme_flip_re_tints_the_same_window() {
        // The second half of requirement 3: the tint is not a one-shot at
        // startup. Same window, the other palette, and the frame is still the
        // window manager's afterwards.
        let frame = Frame::new();
        let dark = super::win::tint(frame.0, Colors::parse(&tint("#14161a", "#a8b0bc")).unwrap());
        report("dark tint", &dark);
        let light = super::win::tint(frame.0, Colors::parse(&tint("#eceef1", "#4b5563")).unwrap());
        report("light tint", &light);
        assert_eq!(
            dark.is_ok(),
            light.is_ok(),
            "one theme landed and one did not"
        );
        assert_eq!(frame.hit_test_caption(), HTCAPTION);
    }

    #[test]
    fn a_refused_colour_never_reaches_win32() {
        // The all-or-nothing rule, from the outside: a malformed payload is
        // rejected by `Colors::parse`, so there is no call to make and no
        // half-tinted frame to explain.
        let frame = Frame::new();
        assert!(Colors::parse(&tint("rgb(20, 22, 26)", "#a8b0bc")).is_err());
        assert_eq!(frame.hit_test_caption(), HTCAPTION);
    }
}
