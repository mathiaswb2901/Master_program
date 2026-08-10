//! `WORKBENCH_HOST_DEMO=1` — dock a synthetic guest in the running shell, with
//! no UI involvement at all.
//!
//! This exists because "verify by running" needs a way in. The panel that will
//! eventually call [`super::host_embed`] belongs to a different work lane, so
//! without this hook the only proof that hosting works inside a *real* Tauri
//! window would be a screenshot of a test harness. With it, `cargo run` puts a
//! live child window inside the shell and the shell log says where.
//!
//! `WORKBENCH_HOST_DEMO=hang` goes further: it wedges the guest's message loop
//! a few seconds after docking, which is how the hang-isolation question gets
//! asked of the actual product rather than of a stand-in window.
//!
//! **`WORKBENCH_HOST_DEMO=word[:<path>]` docks a real Word.** The synthetic
//! guest is a faithful stand-in for everything the mechanism does, but not for
//! everything Office *is* — Word's frame has its own child windows, its own
//! compositing and its own idea of when to paint. Questions about what the user
//! sees have to be asked of Word itself, and this is the cheapest way to ask one
//! without the whole UI in the loop: it goes through
//! [`super::commands::open_guest`], which is the same launch/find/embed the
//! Python service drives, with `OpusApp` in place of the fixture's class.
//! `=excel[:<path>]` does the same for `XLMAIN`.
//!
//! Debug builds only, and not wired to anything a user can reach.

use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Manager};

use super::commands;
use super::geometry::CssRect;
use super::main_thread;
use super::{zorder, HostError, HostGeometry, WindowId};

/// Panel id the demo hosts under.
const DEMO_HOST_ID: &str = "demo";
/// When the z-order probe runs again after the resize, measured from it.
///
/// Twice, not once, and both late: the window a panel has to stay in front of —
/// WebView2's widget — is not ours and can raise itself at any time, so "it was
/// on top when we created it" is not the same claim as "it is on top now".
const PROBE_AT: [Duration; 2] = [Duration::from_secs(3), Duration::from_secs(10)];
/// Long enough to see the guest painted and interact with it before it stops
/// responding.
const HANG_AFTER: Duration = Duration::from_secs(6);
/// Long enough to be unmistakably a hang, short enough that the machine is not
/// left in it.
const HANG_FOR: Duration = Duration::from_secs(20);
/// How long a real Office instance gets to show its frame. Generous on purpose:
/// a cold Word on a busy machine is nothing like the fixture's few hundred
/// milliseconds, and a timeout here reads as "hosting is broken" when it is not.
const OFFICE_TIMEOUT: Duration = Duration::from_secs(90);
/// Long enough for the page to have finished loading before the keyboard is put
/// in the guest. A webview that is still coming up takes the focus back, and
/// then the demo would be showing the opposite of what it is for.
const FOCUS_AFTER: Duration = Duration::from_secs(4);

/// Which guest the demo docks.
enum Which {
    /// The fixture, optionally wedged a few seconds in.
    Synthetic { hang: bool },
    /// A real Office frame: the executable, the document to open, and the frame
    /// class to find it by.
    Office {
        exe: PathBuf,
        document: Option<PathBuf>,
        class: &'static str,
    },
}

/// Start the demo if the environment asks for it. Called once, when the window
/// is up.
pub fn start_demo_if_asked(app: &AppHandle) {
    let Ok(mode) = std::env::var("WORKBENCH_HOST_DEMO") else {
        return;
    };
    if mode.is_empty() || mode == "0" {
        return;
    }
    let Some(which) = parse_mode(&mode) else {
        crate::backend::log(&format!(
            "office host demo: {mode:?} names no Office installation I can find; \
             expected 1, hang, word[:<path>] or excel[:<path>]"
        ));
        return;
    };
    let app = app.clone();
    // Off the main thread: launching a process and waiting for its window is
    // exactly the work the command layer refuses to do there.
    thread::spawn(move || run(&app, which));
}

/// `1` / `hang` / `word[:<path>]` / `excel[:<path>]`.
///
/// `None` only when a real application was asked for and its executable is not
/// on this machine — the one case where the demo cannot do what it was told and
/// silently docking the fixture instead would be a lie in the log.
fn parse_mode(mode: &str) -> Option<Which> {
    let (name, document) = match mode.split_once(':') {
        Some((name, path)) => (name, Some(PathBuf::from(path))),
        None => (mode, None),
    };
    let office = |exe: &str, class: &'static str| {
        office_exe(exe).map(|exe| Which::Office {
            exe,
            document: document.clone(),
            class,
        })
    };
    match name.to_ascii_lowercase().as_str() {
        "hang" => Some(Which::Synthetic { hang: true }),
        "word" => office("WINWORD.EXE", "OpusApp"),
        "excel" => office("EXCEL.EXE", "XLMAIN"),
        _ => Some(Which::Synthetic { hang: false }),
    }
}

/// Where a Click-to-Run or MSI Office puts its executables. Deliberately a short
/// list of the standard locations rather than a registry walk: this is a demo
/// hook, and a machine that keeps Office somewhere else can name the path the
/// long way round by putting it on `PATH`.
fn office_exe(name: &str) -> Option<PathBuf> {
    let roots = [
        r"C:\Program Files\Microsoft Office\root\Office16",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16",
        r"C:\Program Files\Microsoft Office\Office16",
        r"C:\Program Files (x86)\Microsoft Office\Office16",
    ];
    roots
        .iter()
        .map(|root| PathBuf::from(root).join(name))
        .find(|path| path.is_file())
}

fn run(app: &AppHandle, which: Which) {
    let Some(webview) = app.get_webview_window("main") else {
        crate::backend::log("office host demo: no main window");
        return;
    };
    let window = webview.as_ref().window();
    let rect = match demo_rect(&window) {
        Ok(rect) => rect,
        Err(err) => {
            crate::backend::log(&format!("office host demo: no usable rectangle: {err}"));
            return;
        }
    };
    let hang = matches!(which, Which::Synthetic { hang: true });
    let opened = match &which {
        Which::Synthetic { .. } => {
            commands::open_synthetic_guest(app, &window, DEMO_HOST_ID.to_string(), rect)
        }
        Which::Office {
            exe,
            document,
            class,
        } => {
            let args: Vec<&str> = document
                .as_deref()
                .and_then(|path| path.to_str())
                .into_iter()
                .collect();
            crate::backend::log(&format!(
                "office host demo: launching {} {:?} and waiting for a {class} frame",
                exe.display(),
                args
            ));
            commands::open_guest(
                app,
                &window,
                DEMO_HOST_ID.to_string(),
                rect,
                exe,
                &args,
                class,
                // What `ui/src/officeHost.ts` sends for a real document: Word's
                // own strip stays, because hiding it costs the ribbon's top row.
                0.0,
                OFFICE_TIMEOUT,
            )
        }
    };
    let docked: HostGeometry = match opened {
        Ok(geometry) => {
            crate::backend::log(&format!(
                "office host demo: guest {:#x} docked at {:?} (scale {})",
                geometry.guest.0, geometry.layout.panel, geometry.scale
            ));
            geometry
        }
        Err(err) => {
            crate::backend::log(&format!("office host demo: {err}"));
            return;
        }
    };

    // A resize storm against a real window, so the batching path is exercised
    // by something other than a unit test.
    for step in 0..120 {
        let mut moving = rect;
        moving.width = rect.width - f64::from(step % 40);
        let _ = commands::host_set_bounds(
            app.clone(),
            window.clone(),
            DEMO_HOST_ID.to_string(),
            moving,
        );
    }
    let _ = commands::host_set_bounds(app.clone(), window.clone(), DEMO_HOST_ID.to_string(), rect);

    probe_z_order(app, &window, docked.window);

    // **The keyboard trap, in the running shell.** Put the keyboard where a
    // click into a real Word puts it — inside the guest, whose window procedure
    // then receives every keystroke and hands the webview none of them. This is
    // the same call the UI makes when a document panel is focused, and it is
    // what makes the escape chord something to actually try rather than read
    // about.
    thread::sleep(FOCUS_AFTER);
    match commands::host_focus(app.clone(), DEMO_HOST_ID.to_string()) {
        Ok(()) => crate::backend::log(
            "office host demo: the keyboard is in the guest — every keystroke is its own now; \
             press Ctrl+Alt+Home to get it back",
        ),
        Err(err) => crate::backend::log(&format!(
            "office host demo: could not focus the guest: {err}"
        )),
    }

    if hang {
        thread::sleep(HANG_AFTER);
        crate::backend::log(&format!(
            "office host demo: hanging the guest for {HANG_FOR:?} — the window \
             should stay responsive"
        ));
        let _ = commands::host_hang_guest(
            app.clone(),
            DEMO_HOST_ID.to_string(),
            HANG_FOR.as_millis() as u32,
        );
    }
}

/// **Is the docked guest actually on top of the webview?** Asked here, in the
/// running shell, because there is nowhere else it can be asked.
///
/// The hosting tests build the panel/clip/guest arrangement under a *top-level*
/// window of their own, which has no WebView2 widget in it — so they can prove
/// everything about docking except the one thing that only matters when a
/// webview is a sibling. This is where that gap is closed, and it is the probe
/// #127's verification note asked for: hit test and screen pixel, side by side,
/// with the sibling chain that explains either answer.
///
/// Runs on the demo's own thread, and every call it makes from there is one of
/// three kinds. The queries (`GetWindow`, `GetWindowRect`, `WindowFromPoint`,
/// `GetPixel`) are reads and touch no window's state. The resize goes through
/// Tauri, which marshals it. And the one call that *writes* — `ShowWindow`, in
/// the paint control — is handed to [`super::main_thread::on_main`] like every
/// other window write in the shell.
///
/// **`ShowWindow` is not exempt on the grounds of being asynchronous.** It is
/// not asynchronous: `ShowWindowAsync` exists precisely because the plain call
/// on a window owned by another thread waits on that thread's message queue, so
/// making it from here could park this thread behind whatever the main thread is
/// already doing — embedding a cold Word, say — which is the stall #121 was
/// merged to remove. Nothing here creates or destroys a window either; that is
/// still the main thread's, and only the main thread's.
fn probe_z_order(app: &AppHandle, window: &tauri::Window, panel: WindowId) {
    let Ok(top_level) = window.hwnd() else {
        crate::backend::log("office host demo: no HWND to probe against");
        return;
    };
    let report = |tag: &str| {
        let rect = zorder::screen_rect_of(panel.hwnd());
        // The middle of the panel: far from every edge, and inside the clip
        // child and the guest both.
        let point = (rect.x + rect.width / 2, rect.y + rect.height / 2);
        zorder::report(tag, top_level, panel.hwnd(), point);
    };

    // The state docking itself produced.
    report("docked");

    // And the control that says whether "the panel rectangle shows the webview"
    // would be a Z-order problem at all.
    {
        let rect = zorder::screen_rect_of(panel.hwnd());
        let on_owning_thread = |work: Box<
            dyn FnOnce() -> Result<(), HostError> + Send + 'static,
        >| { main_thread::on_main(app, work) };
        zorder::paint_control(
            &on_owning_thread,
            "docked",
            top_level,
            panel.hwnd(),
            (rect.x + rect.width / 2, rect.y + rect.height / 2),
        );
    }

    // **And the state a resize leaves behind.** The window a panel has to stay
    // in front of is not ours: wry repositions `WRY_WEBVIEW` whenever the shell
    // window changes size, and a `SetWindowPos` that did not carry
    // `SWP_NOZORDER` would put it back on top of every docked document. This is
    // how a user hits that — by dragging the window edge — so the demo does it
    // rather than assuming the answer.
    if let Ok(size) = window.inner_size() {
        let _ = window.set_size(tauri::PhysicalSize::new(
            size.width.saturating_sub(40).max(400),
            size.height,
        ));
        thread::sleep(Duration::from_millis(250));
        let _ = window.set_size(size);
        thread::sleep(Duration::from_millis(250));
        report("after a window resize");
    }

    let mut last = Duration::ZERO;
    for at in PROBE_AT {
        thread::sleep(at.saturating_sub(last));
        last = at;
        report(&format!("t+{at:?}"));
    }
}

/// The right-hand two thirds of the window, inset a little, in CSS pixels —
/// roughly where a document panel sits.
fn demo_rect(window: &tauri::Window) -> Result<CssRect, super::HostError> {
    let scale = commands::scale_factor(window)?;
    let size = window.inner_size().map_err(|err| {
        super::HostError::new(
            super::HostErrorCode::Win32,
            format!("no window size: {err}"),
        )
    })?;
    let width = f64::from(size.width) / scale;
    let height = f64::from(size.height) / scale;
    let margin = 12.0;
    Ok(CssRect {
        x: width / 3.0,
        y: 48.0,
        width: (width * 2.0 / 3.0 - margin).max(200.0),
        height: (height - 48.0 - margin).max(200.0),
    })
}
