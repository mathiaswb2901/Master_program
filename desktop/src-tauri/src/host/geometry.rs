//! Where a panel is, in the one unit Win32 understands.
//!
//! The UI measures a dockview panel in **CSS pixels** relative to the webview's
//! client area; Win32 places windows in **physical pixels**. Converting between
//! them needs a scale factor, and the whole file exists to make sure there is
//! exactly **one** authority for that number: the window's own
//! `scale_factor()`, passed in from the command layer. Nothing here calls
//! `GetDpiForWindow`, reads a monitor, or consults a process DPI mode — a
//! second source would disagree the moment a window is dragged between two
//! monitors at different scales, and a hosted window that is off by a scale
//! factor is off by a *lot*.
//!
//! Everything in this module is pure arithmetic, so all of it is unit-tested
//! without a window, a display or a message loop.

use serde::{Deserialize, Serialize};

use super::HostError;

/// The same ceiling `PanelRect` enforces in
/// `server/src/workbench_server/models/office_host.py`, so a rectangle the
/// Python layer would accept is one this layer accepts too. Physical pixels
/// after scaling can exceed it; the check is on the CSS values, as there.
const MAX_CSS_EXTENT: f64 = 100_000.0;

/// A guest cannot usefully draw a caption taller than this, and a bad value
/// here scrolls the whole document out of view. Clamped rather than refused:
/// the inset is a cosmetic hint, not a correctness input.
const MAX_CAPTION_INSET: i32 = 400;

/// A panel rectangle as the UI measures it: CSS pixels, relative to the client
/// area of the window the webview fills.
///
/// Deliberately `f64` and not `i32`: dockview lays out in fractional CSS pixels
/// (a 3-way split of 1000px is not three integers), and rounding on the
/// JavaScript side would round each panel independently — which is how you get
/// a one-pixel seam of desktop showing between two panels. Rounding happens
/// once, here, on the physical edges.
#[derive(Debug, Clone, Copy, PartialEq, Deserialize, Serialize)]
pub struct CssRect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

/// A rectangle in physical pixels, ready to hand to `SetWindowPos`.
///
/// The coordinate space is whatever the containing window's client area is —
/// the panel window is placed in the Tauri window's, the clip child in the
/// panel window's, the guest in the clip child's. Nothing here knows which.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct PhysicalRect {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

impl PhysicalRect {
    /// Convert one CSS rectangle to physical pixels at `scale`.
    ///
    /// Edges are rounded, then the size is derived from the rounded edges —
    /// **not** the size rounded on its own. At scale 1.5 two panels meeting at
    /// CSS x=100.5 both land on physical 151, so they abut exactly; rounding
    /// each width independently would leave a gap on one side and an overlap on
    /// the other, and the gap shows through as a stripe of whatever is behind
    /// the panel.
    pub fn from_css(rect: CssRect, scale: f64) -> Result<Self, HostError> {
        if !scale.is_finite() || scale <= 0.0 || scale > 64.0 {
            return Err(HostError::invalid_rect(format!(
                "implausible scale factor {scale}"
            )));
        }
        for value in [rect.x, rect.y, rect.width, rect.height] {
            if !value.is_finite() || value.abs() > MAX_CSS_EXTENT {
                return Err(HostError::invalid_rect(format!(
                    "rectangle {rect:?} is not a finite rectangle within \u{00b1}{MAX_CSS_EXTENT}"
                )));
            }
        }
        // A zero-area panel has no window to give it. The Python `PanelRect`
        // refuses it for the same reason (`width: int = Field(ge=1, …)`), and
        // `SetWindowPos` with a zero size is a window nobody can click.
        if rect.width < 1.0 || rect.height < 1.0 {
            return Err(HostError::invalid_rect(format!(
                "rectangle {rect:?} has no area"
            )));
        }

        let left = round_to_i32(rect.x * scale);
        let top = round_to_i32(rect.y * scale);
        let right = round_to_i32((rect.x + rect.width) * scale);
        let bottom = round_to_i32((rect.y + rect.height) * scale);
        Ok(Self {
            x: left,
            y: top,
            // `max(1)`: at a scale below 1 a sub-pixel panel can round to zero
            // width, and the rectangle above already proved it was meant to
            // have area.
            width: (right - left).max(1),
            height: (bottom - top).max(1),
        })
    }

    pub fn right(self) -> i32 {
        self.x.saturating_add(self.width)
    }

    pub fn bottom(self) -> i32 {
        self.y.saturating_add(self.height)
    }
}

/// `f64` -> `i32` without the silent saturation of `as`.
///
/// The range check above makes overflow unreachable in practice; this is here
/// so that a future caller who skips it gets a clamped pixel rather than
/// `i32::MIN`, which as a window position is a window nobody will ever see.
fn round_to_i32(value: f64) -> i32 {
    value.round().clamp(i32::MIN as f64, i32::MAX as f64) as i32
}

/// The three rectangles one hosted panel needs, in one place.
///
/// Three windows, because the middle one earns its keep: the **panel window**
/// is the stable rectangle the layout writes to and the WndProc listens on, the
/// **clip child** is the viewport, and the **guest** sits inside the clip child
/// offset upwards by whatever it draws for itself as a title bar. Word draws
/// its own caption row inside its window; hosting it without an offset shows a
/// dead strip of Word chrome at the top of the panel, and offsetting it inside
/// the panel window directly would push the offset into the same rectangle the
/// layout code and the WndProc reason about.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct HostLayout {
    /// The panel window, in the Tauri window's client area.
    pub panel: PhysicalRect,
    /// The clip child, in the panel window's client area: always the full panel
    /// at the origin. It exists to clip, not to move.
    pub clip: PhysicalRect,
    /// The guest, in the clip child's client area. Shifted up by the caption
    /// inset and grown by the same amount, so the strip it draws for itself
    /// falls outside the clip and the visible area is still a full panel of
    /// document.
    pub guest: PhysicalRect,
}

/// Lay out one hosted panel. `caption_inset` is in physical pixels and is
/// clamped, never rejected — it is a cosmetic hint from the caller about how
/// much self-drawn chrome to hide, and getting it wrong should misalign a
/// window, not fail an embed.
pub fn host_layout(panel: PhysicalRect, caption_inset: i32) -> HostLayout {
    let inset = caption_inset.clamp(0, MAX_CAPTION_INSET);
    HostLayout {
        panel,
        clip: PhysicalRect {
            x: 0,
            y: 0,
            width: panel.width,
            height: panel.height,
        },
        guest: PhysicalRect {
            x: 0,
            y: -inset,
            width: panel.width,
            height: panel.height.saturating_add(inset),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn css(x: f64, y: f64, width: f64, height: f64) -> CssRect {
        CssRect {
            x,
            y,
            width,
            height,
        }
    }

    #[test]
    fn converts_at_unit_scale() {
        let rect = PhysicalRect::from_css(css(10.0, 20.0, 300.0, 400.0), 1.0).unwrap();
        assert_eq!(
            rect,
            PhysicalRect {
                x: 10,
                y: 20,
                width: 300,
                height: 400
            }
        );
    }

    #[test]
    fn adjacent_panels_do_not_gap_or_overlap_at_fractional_scale() {
        // The failure this guards: rounding each panel's width on its own.
        // 100.5 * 1.5 = 150.75 -> 151 for both the right edge of A and the left
        // edge of B, so they meet exactly.
        for scale in [1.25, 1.5, 1.75, 2.0] {
            let left = PhysicalRect::from_css(css(0.0, 0.0, 100.5, 50.0), scale).unwrap();
            let right = PhysicalRect::from_css(css(100.5, 0.0, 100.5, 50.0), scale).unwrap();
            assert_eq!(
                left.right(),
                right.x,
                "a seam opened between two panels at scale {scale}"
            );
        }
    }

    #[test]
    fn a_stack_of_panels_still_covers_the_whole_height() {
        // Three panels splitting 1000 CSS px at 125%: every physical row is
        // covered exactly once.
        let scale = 1.25;
        let mut y = 0.0;
        let height = 1000.0 / 3.0;
        let mut edges = vec![];
        for _ in 0..3 {
            let rect = PhysicalRect::from_css(css(0.0, y, 100.0, height), scale).unwrap();
            edges.push((rect.y, rect.bottom()));
            y += height;
        }
        assert_eq!(edges[0].0, 0);
        assert_eq!(edges[0].1, edges[1].0);
        assert_eq!(edges[1].1, edges[2].0);
        assert_eq!(edges[2].1, 1250);
    }

    #[test]
    fn a_panel_scrolled_off_the_left_keeps_its_negative_origin() {
        // Legal, and modelled as legal in `PanelRect` too: a panel can be
        // dragged or scrolled partly out of view.
        let rect = PhysicalRect::from_css(css(-40.0, -10.0, 200.0, 100.0), 1.0).unwrap();
        assert_eq!(rect.x, -40);
        assert_eq!(rect.y, -10);
        assert_eq!(rect.width, 200);
    }

    #[test]
    fn refuses_a_rectangle_with_no_area() {
        assert!(PhysicalRect::from_css(css(0.0, 0.0, 0.0, 100.0), 1.0).is_err());
        assert!(PhysicalRect::from_css(css(0.0, 0.0, 100.0, -5.0), 1.0).is_err());
    }

    #[test]
    fn refuses_values_javascript_can_produce_and_win32_cannot_use() {
        assert!(PhysicalRect::from_css(css(f64::NAN, 0.0, 10.0, 10.0), 1.0).is_err());
        assert!(PhysicalRect::from_css(css(0.0, f64::INFINITY, 10.0, 10.0), 1.0).is_err());
        assert!(PhysicalRect::from_css(css(0.0, 0.0, 1e9, 10.0), 1.0).is_err());
    }

    #[test]
    fn refuses_an_implausible_scale_factor() {
        let rect = css(0.0, 0.0, 100.0, 100.0);
        assert!(PhysicalRect::from_css(rect, 0.0).is_err());
        assert!(PhysicalRect::from_css(rect, -1.0).is_err());
        assert!(PhysicalRect::from_css(rect, f64::NAN).is_err());
        assert!(PhysicalRect::from_css(rect, 1000.0).is_err());
    }

    #[test]
    fn a_sub_pixel_panel_still_gets_a_pixel() {
        let rect = PhysicalRect::from_css(css(0.0, 0.0, 1.0, 1.0), 0.5).unwrap();
        assert_eq!((rect.width, rect.height), (1, 1));
    }

    #[test]
    fn the_caption_inset_moves_only_the_guest() {
        let panel = PhysicalRect {
            x: 300,
            y: 40,
            width: 800,
            height: 600,
        };
        let layout = host_layout(panel, 32);
        assert_eq!(layout.panel, panel);
        // The clip is the viewport: the panel's size, at its own origin.
        assert_eq!(
            layout.clip,
            PhysicalRect {
                x: 0,
                y: 0,
                width: 800,
                height: 600
            }
        );
        // The guest is pushed up by the inset and grown by it, so the visible
        // area is still a full panel of document.
        assert_eq!(layout.guest.y, -32);
        assert_eq!(layout.guest.height, 632);
        assert_eq!(layout.guest.bottom() - 0, 600);
    }

    #[test]
    fn no_inset_leaves_the_guest_flush() {
        let panel = PhysicalRect {
            x: 0,
            y: 0,
            width: 640,
            height: 480,
        };
        let layout = host_layout(panel, 0);
        assert_eq!(layout.guest, layout.clip);
    }

    #[test]
    fn an_absurd_inset_is_clamped_rather_than_fatal() {
        let panel = PhysicalRect {
            x: 0,
            y: 0,
            width: 640,
            height: 480,
        };
        assert_eq!(host_layout(panel, -50).guest.y, 0);
        assert_eq!(host_layout(panel, 10_000).guest.y, -MAX_CAPTION_INSET);
    }
}
