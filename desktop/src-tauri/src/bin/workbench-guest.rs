//! The synthetic guest: a stand-in for Word, in about two hundred lines.
//!
//! **Why a separate process and not a second window on a background thread.**
//! The three risks this whole feature carries are all cross-*process* risks. A
//! process can be reaped (or fail to be) — the job object exists because a real
//! Word survived `Application.Quit` in the spike. A process owns its own input
//! queue, and attaching ours to it when it becomes our child is the hazard that
//! decides whether native hosting can be the default. And a process can be
//! wedged without wedging us, which is the thing we need to measure. A window
//! on one of our own threads would prove none of that; it would prove that
//! `SetParent` works between two windows we already control, which was never in
//! doubt.
//!
//! **What it does that matters.** It is a top-level window of a known class
//! (found the way Word's `OpusApp` frame will be found: by class and pid), it
//! draws its own caption strip *inside its client area* — the thing the clip
//! child exists to hide — it shows whether it has the keyboard focus and what
//! has been typed into it, and it can be told to stop pumping messages for a
//! given number of milliseconds.
//!
//! **It is a fixture and stays one.** The body compiles only in debug builds,
//! its commands are registered only in debug builds (`lib.rs` keeps three
//! explicit handler lists for that reason), and nothing in the shipped UI can
//! reach it.

fn main() {
    #[cfg(all(windows, debug_assertions))]
    guest::run();
    #[cfg(not(all(windows, debug_assertions)))]
    eprintln!(
        "workbench-guest is a debug-only test fixture for the Office host and \
         does nothing in a release build"
    );
}

#[cfg(all(windows, debug_assertions))]
mod guest {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Mutex;

    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, RECT, WPARAM};
    use windows::Win32::Graphics::Gdi::{
        BeginPaint, CreateSolidBrush, DeleteObject, DrawTextW, EndPaint, FillRect, InvalidateRect,
        SetBkMode, SetTextColor, UpdateWindow, DT_CENTER, DT_SINGLELINE, DT_VCENTER, HBRUSH,
        PAINTSTRUCT, TRANSPARENT,
    };
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DispatchMessageW, GetClientRect, GetMessageW,
        PostQuitMessage, RegisterClassExW, ShowWindow, TranslateMessage, CS_HREDRAW, CS_VREDRAW,
        MSG, SW_SHOWNOACTIVATE, WM_CHAR, WM_DESTROY, WM_KILLFOCUS, WM_PAINT, WM_SETFOCUS,
        WNDCLASSEXW, WS_OVERLAPPEDWINDOW, WS_VISIBLE,
    };

    use workbench_desktop_lib::host::guest::{
        SYNTHETIC_GUEST_CAPTION, SYNTHETIC_GUEST_CLASS, SYNTHETIC_GUEST_HANDSHAKE,
        SYNTHETIC_GUEST_HANG,
    };

    /// DESIGN.md `--error` (dark), `#F85149`, as a `COLORREF` (`0x00BBGGRR`).
    /// The self-drawn caption strip is painted in it on purpose: if the clip
    /// child ever stops hiding it, nobody will have to squint at a screenshot
    /// to notice.
    const CAPTION_FILL: COLORREF = COLORREF(0x0049_51F8);
    /// DESIGN.md `--surface-paper`, `#FFFFFF`: a document is white in both
    /// themes, and this is standing in for a document.
    const BODY_FILL: COLORREF = COLORREF(0x00FF_FFFF);
    /// DESIGN.md `--text-on-paper`, `#1B1F26`.
    const BODY_TEXT: COLORREF = COLORREF(0x0026_1F1B);
    /// DESIGN.md `--surface-paper` again, for text drawn on the caption strip.
    const CAPTION_TEXT: COLORREF = COLORREF(0x00FF_FFFF);

    /// Does this window have the keyboard focus? Drawn, so a screenshot is
    /// evidence about focus routing rather than an argument about it.
    static FOCUSED: AtomicBool = AtomicBool::new(false);
    /// What has been typed into it, for the same reason.
    static TYPED: Mutex<String> = Mutex::new(String::new());

    pub fn run() -> ! {
        // SAFETY: a conventional Win32 window program. Every call is on a
        // handle produced by the call above it, and the message loop owns the
        // window for the process's whole life.
        unsafe {
            let instance = GetModuleHandleW(None).expect("no module handle");
            let class_name: Vec<u16> = SYNTHETIC_GUEST_CLASS
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            let class = WNDCLASSEXW {
                cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
                style: CS_HREDRAW | CS_VREDRAW,
                lpfnWndProc: Some(wnd_proc),
                hInstance: instance.into(),
                lpszClassName: PCWSTR(class_name.as_ptr()),
                hbrBackground: HBRUSH(CreateSolidBrush(BODY_FILL).0),
                ..Default::default()
            };
            if RegisterClassExW(&class) == 0 {
                panic!("the synthetic guest could not register its window class");
            }

            let window = CreateWindowExW(
                Default::default(),
                PCWSTR(class_name.as_ptr()),
                windows::core::w!("Workbench synthetic guest"),
                WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                200,
                200,
                900,
                640,
                None,
                None,
                None,
                None,
            )
            .expect("the synthetic guest could not create its window");

            // `SW_SHOWNOACTIVATE`, not `SW_SHOW`: a fixture that steals the
            // foreground would make every focus measurement about itself.
            let _ = ShowWindow(window, SW_SHOWNOACTIVATE);
            let _ = UpdateWindow(window);

            // The handshake. Not how the window is found — that is by class and
            // pid, the way Word will be — but what the integration test checks
            // the finder's answer against.
            //
            // **Written with `writeln!`, not `println!`, and the handshake
            // last.** `println!` panics on a write error, and the test stops
            // reading — and so closes the pipe — the moment it has the
            // handshake line. A `println!` that raced that closure aborted the
            // process, which showed up as a guest whose window "no longer
            // exists" a few milliseconds after it was found. A fixture must not
            // die because nobody is listening any more.
            use std::io::Write;
            let mut out = std::io::stdout().lock();
            let _ = writeln!(out, "guest-pid {}", std::process::id());
            let _ = writeln!(out, "{SYNTHETIC_GUEST_HANDSHAKE}{}", window.0 as isize);
            let _ = out.flush();
            drop(out);

            let mut message = MSG::default();
            while GetMessageW(&mut message, None, 0, 0).as_bool() {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
            std::process::exit(0)
        }
    }

    unsafe extern "system" fn wnd_proc(
        window: HWND,
        message: u32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        match message {
            SYNTHETIC_GUEST_HANG => {
                // The whole point of the fixture: a message loop that stops
                // returning, on demand, for a known length of time.
                let millis = wparam.0 as u64;
                eprintln!("synthetic guest: hanging for {millis} ms");
                std::thread::sleep(std::time::Duration::from_millis(millis));
                eprintln!("synthetic guest: awake again");
                LRESULT(0)
            }
            WM_PAINT => {
                // SAFETY: paint bracket, and every handle is from the call above.
                unsafe { paint(window) };
                LRESULT(0)
            }
            WM_SETFOCUS | WM_KILLFOCUS => {
                FOCUSED.store(message == WM_SETFOCUS, Ordering::SeqCst);
                // SAFETY: a plain Win32 call on our own window.
                unsafe { InvalidateRect(Some(window), None, true).ok().ok() };
                LRESULT(0)
            }
            WM_CHAR => {
                if let Ok(mut typed) = TYPED.lock() {
                    if let Some(character) = char::from_u32(wparam.0 as u32) {
                        if !character.is_control() {
                            typed.push(character);
                        }
                    }
                    // Keep it to something that fits on one line.
                    while typed.chars().count() > 24 {
                        typed.remove(0);
                    }
                }
                // SAFETY: as above.
                unsafe { InvalidateRect(Some(window), None, true).ok().ok() };
                LRESULT(0)
            }
            WM_DESTROY => {
                // SAFETY: a plain Win32 call.
                unsafe { PostQuitMessage(0) };
                LRESULT(0)
            }
            // SAFETY: the default handler, on the arguments it was given.
            _ => unsafe { DefWindowProcW(window, message, wparam, lparam) },
        }
    }

    /// # Safety
    /// `window` must be a live window owned by this thread.
    unsafe fn paint(window: HWND) {
        unsafe {
            let mut ps = PAINTSTRUCT::default();
            let hdc = BeginPaint(window, &mut ps);
            let mut client = RECT::default();
            let _ = GetClientRect(window, &mut client);

            let body = CreateSolidBrush(BODY_FILL);
            FillRect(hdc, &client, body);
            let _ = DeleteObject(body.into());

            // The strip a hosted guest must not show: drawn inside the client
            // area, exactly as Word draws its own caption row.
            let caption = RECT {
                bottom: client.top + SYNTHETIC_GUEST_CAPTION,
                ..client
            };
            let caption_brush = CreateSolidBrush(CAPTION_FILL);
            FillRect(hdc, &caption, caption_brush);
            let _ = DeleteObject(caption_brush.into());

            SetBkMode(hdc, TRANSPARENT);
            SetTextColor(hdc, CAPTION_TEXT);
            draw(hdc, caption, "self-drawn caption — should be clipped away");

            SetTextColor(hdc, BODY_TEXT);
            let focused = if FOCUSED.load(Ordering::SeqCst) {
                "FOCUSED"
            } else {
                "not focused"
            };
            let typed = TYPED
                .lock()
                .map(|typed| typed.clone())
                .unwrap_or_else(|_| String::new());
            let mut line = RECT {
                top: client.top + SYNTHETIC_GUEST_CAPTION + 24,
                bottom: client.top + SYNTHETIC_GUEST_CAPTION + 64,
                ..client
            };
            draw(
                hdc,
                line,
                &format!(
                    "synthetic guest  ·  pid {}  ·  hwnd {:#x}",
                    std::process::id(),
                    window.0 as isize
                ),
            );
            line.top += 40;
            line.bottom += 40;
            draw(hdc, line, focused);
            line.top += 40;
            line.bottom += 40;
            draw(hdc, line, &format!("typed: {typed}"));

            let _ = EndPaint(window, &ps);
        }
    }

    /// # Safety
    /// `hdc` must be a device context that is valid for the current paint.
    unsafe fn draw(hdc: windows::Win32::Graphics::Gdi::HDC, mut rect: RECT, text: &str) {
        let mut wide: Vec<u16> = text.encode_utf16().collect();
        unsafe {
            DrawTextW(
                hdc,
                &mut wide,
                &mut rect,
                DT_CENTER | DT_VCENTER | DT_SINGLELINE,
            )
        };
    }
}
