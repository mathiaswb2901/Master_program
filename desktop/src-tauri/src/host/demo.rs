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
//! Debug builds only, and not wired to anything a user can reach.

use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Manager};

use super::commands;
use super::geometry::CssRect;

/// Panel id the demo hosts under.
const DEMO_HOST_ID: &str = "demo";
/// Long enough to see the guest painted and interact with it before it stops
/// responding.
const HANG_AFTER: Duration = Duration::from_secs(6);
/// Long enough to be unmistakably a hang, short enough that the machine is not
/// left in it.
const HANG_FOR: Duration = Duration::from_secs(20);
/// Long enough for the page to have finished loading before the keyboard is put
/// in the guest. A webview that is still coming up takes the focus back, and
/// then the demo would be showing the opposite of what it is for.
const FOCUS_AFTER: Duration = Duration::from_secs(4);

/// Start the demo if the environment asks for it. Called once, when the window
/// is up.
pub fn start_demo_if_asked(app: &AppHandle) {
    let Ok(mode) = std::env::var("WORKBENCH_HOST_DEMO") else {
        return;
    };
    if mode.is_empty() || mode == "0" {
        return;
    }
    let hang = mode.eq_ignore_ascii_case("hang");
    let app = app.clone();
    // Off the main thread: launching a process and waiting for its window is
    // exactly the work the command layer refuses to do there.
    thread::spawn(move || run(&app, hang));
}

fn run(app: &AppHandle, hang: bool) {
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
    match commands::open_synthetic_guest(app, &window, DEMO_HOST_ID.to_string(), rect) {
        Ok(geometry) => crate::backend::log(&format!(
            "office host demo: guest {:#x} docked at {:?} (scale {})",
            geometry.guest.0, geometry.layout.panel, geometry.scale
        )),
        Err(err) => {
            crate::backend::log(&format!("office host demo: {err}"));
            return;
        }
    }

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
