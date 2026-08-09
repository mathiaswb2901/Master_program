/**
 * The ordering rule behind "one Escape answers one dialog".
 *
 * `useOverlayKeys` is a window listener and belongs to the browser, but the part
 * that can be *wrong* is arithmetic on a list: who is on top after each open and
 * close. That is what turned one Escape into an answer to two dialogs, so it is
 * asserted here where it costs milliseconds. The behaviour it buys — a real
 * palette with a real confirmation over it — is `e2e/quickbarA11y.spec.ts`.
 */

import { describe, expect, it } from "vitest";

import { isTopOverlay, pushOverlay } from "./overlays";

describe("the overlay stack", () => {
  it("gives the keyboard to the newest layer, not the oldest", () => {
    // The whole reason this is a stack and not `stopImmediatePropagation()`:
    // listeners on one target fire in registration order, so "first to open"
    // would win — and the first to open is the dialog *underneath*.
    const palette = {};
    const confirm = {};
    const closePalette = pushOverlay(palette);
    expect(isTopOverlay(palette)).toBe(true);

    const closeConfirm = pushOverlay(confirm);
    expect(isTopOverlay(confirm)).toBe(true);
    expect(isTopOverlay(palette)).toBe(false);

    // The confirmation answered: the palette underneath owns the keyboard again.
    closeConfirm();
    expect(isTopOverlay(palette)).toBe(true);
    closePalette();
    expect(isTopOverlay(palette)).toBe(false);
  });

  it("removes by identity, so a layer closing underneath leaves the top alone", () => {
    // A pop would promote whichever layer happened to be last in the array and
    // hand the keyboard to a dialog that is no longer on screen.
    const under = {};
    const over = {};
    const closeUnder = pushOverlay(under);
    const closeOver = pushOverlay(over);

    closeUnder();
    expect(isTopOverlay(over)).toBe(true);
    expect(isTopOverlay(under)).toBe(false);

    closeOver();
    expect(isTopOverlay(over)).toBe(false);
  });

  it("ignores a second close, so a remount cannot evict the layer above it", () => {
    // React runs a cleanup exactly once, but the closure is handed out and an
    // effect that re-ran under StrictMode has called it already. A second call
    // must be a no-op rather than splicing out whatever now sits at that index.
    const first = {};
    const second = {};
    const closeFirst = pushOverlay(first);
    closeFirst();
    const closeSecond = pushOverlay(second);

    closeFirst();
    expect(isTopOverlay(second)).toBe(true);
    closeSecond();
  });

  it("says nobody owns the keyboard when nothing is open", () => {
    expect(isTopOverlay({})).toBe(false);
  });
});
