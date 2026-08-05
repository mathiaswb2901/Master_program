/**
 * Journey 10 — the motion layer, as a user hits it.
 *
 * Three things a stylesheet cannot prove about itself, so they are asserted in
 * a real browser against the production build:
 *
 *  - **The theme-switch regression.** The audit found that flipping the theme
 *    ran transitions across the whole document, against DESIGN.md §5. This
 *    counts the `transitionrun` events the browser actually fires while the
 *    theme flips, which is the bug stated in the browser's own terms rather
 *    than in ours. It is bracketed by a control assertion — that the window is
 *    full of live transitions at that moment — so it can never pass by finding
 *    nothing to animate.
 *  - **Focus mode moves.** `Alt+M` landed without motion and teleported. The
 *    dock's animation is driven from `src/motion.ts`, so `Element.animate` is
 *    recorded and inspected: what it animated, for how long, and from what.
 *  - **Reduced motion, properly.** Under `prefers-reduced-motion: reduce` the
 *    travel goes to zero and the tint survives — not the crude version where
 *    every duration collapses and the colour feedback goes with it.
 *
 * It ends where it started (dark theme, default arrangement) because the
 * journeys share one workspace and one browser profile.
 */

import { expect, test, type Page } from "@playwright/test";

import { dockSettled, openApp, treeItem } from "./app";

/** Where the recorded `Element.animate` calls land. */
const ANIMS = "__wbAnimations";

interface AnimateCall {
  className: string;
  properties: string[];
  duration: number;
  easing: string;
  keyframes: Record<string, string>[];
  /** `performance.now()` inside the page when the call was made. Two calls one
   * animation apart are a different event from two calls that overlap. */
  at: number;
}

/**
 * Record every `Element.animate` call before the app's first line runs.
 *
 * A poll after the fact would race a 300 ms animation over a WebSocket-latency
 * round trip; wrapping the API is exact, and it also captures what was asked
 * for rather than what the compositor is part-way through.
 */
async function recordAnimations(page: Page): Promise<void> {
  await page.addInitScript((globalName: string) => {
    const calls: AnimateCall[] = [];
    (window as unknown as Record<string, unknown>)[globalName] = calls;
    const original = Element.prototype.animate;
    Element.prototype.animate = function (
      this: Element,
      keyframes: Keyframe[] | PropertyIndexedKeyframes | null,
      options?: number | KeyframeAnimationOptions,
    ): Animation {
      const frames = Array.isArray(keyframes) ? (keyframes as Record<string, string>[]) : [];
      const properties = [
        ...new Set(frames.flatMap((frame) => Object.keys(frame).filter((k) => k !== "offset"))),
      ];
      const timing = typeof options === "object" && options !== null ? options : {};
      calls.push({
        className: typeof this.className === "string" ? this.className : "",
        properties,
        duration: typeof timing.duration === "number" ? timing.duration : 0,
        easing: timing.easing ?? "",
        keyframes: frames,
        at: performance.now(),
      });
      return original.call(this, keyframes, options);
    };
  }, ANIMS);
}

const animations = (page: Page): Promise<AnimateCall[]> =>
  page.evaluate(
    (globalName: string) => (window as unknown as Record<string, AnimateCall[]>)[globalName],
    ANIMS,
  );

/** One frame, then another: enough for a running animation to have advanced past
 * its first keyframe, and far inside the 300 ms it has to run. */
async function twoFrames(page: Page): Promise<void> {
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
  );
}

/** Run a QuickBar command by its row title. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/** A CSS `<time>` in seconds. Chrome serialises `110ms` as `.11s`, so nothing
 * here may compare token values as strings. */
function seconds(value: string): number {
  const match = /^(-?[\d.]+)(ms|s)$/.exec(value.trim());
  return match === null ? Number.NaN : Number(match[1]) * (match[2] === "s" ? 1 : 0.001);
}

/** `data-theme` on `<html>`, or null when the dark default is in force. */
const themeAttribute = (page: Page): Promise<string | null> =>
  page.evaluate(() => document.documentElement.getAttribute("data-theme"));

/** A design token's computed value on `<html>`. */
const token = (page: Page, name: string): Promise<string> =>
  page.evaluate(
    (property: string) =>
      getComputedStyle(document.documentElement).getPropertyValue(property).trim(),
    name,
  );

test("flipping the theme is one repaint, not a window full of animations", async ({ page }) => {
  await openApp(page);

  // Control. If the window carried no colour transitions, "zero transitions
  // ran" would be true of a broken app and of a fixed one alike.
  const transitioning = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("*")].filter((element) => {
      const duration = getComputedStyle(element).transitionDuration;
      return duration !== "" && duration !== "0s" && !duration.startsWith("0s, 0s");
    }).length,
  );
  expect(transitioning, "the window should be full of live colour transitions").toBeGreaterThan(10);

  await page.evaluate(() => {
    const state = { runs: [] as string[] };
    (window as unknown as { __wbTransitions: typeof state }).__wbTransitions = state;
    document.addEventListener(
      "transitionrun",
      (event) => {
        const target = event.target as HTMLElement;
        state.runs.push(`${target.tagName}.${String(target.className)} ${event.propertyName}`);
      },
      { capture: true },
    );
  });

  // Which way it flips depends on the host's `prefers-color-scheme` (index.html
  // picks the first theme before paint), so the assertion is that it *changed*.
  const before = await themeAttribute(page);
  await runCommand(page, "Toggle theme");
  await expect.poll(() => themeAttribute(page)).not.toBe(before);
  // Two frames: a transition started by the flip would have fired `transitionrun`
  // synchronously, but give the browser a chance to prove otherwise.
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
  );

  const runs = await page.evaluate(
    () => (window as unknown as { __wbTransitions: { runs: string[] } }).__wbTransitions.runs,
  );
  expect(runs, "DESIGN.md §5: the theme switch never animates").toEqual([]);
  // And the suppression attribute is not left on the document, which would
  // silently disable motion for the rest of the session.
  await expect(page.locator("html")).not.toHaveAttribute("data-theme-switching", /.*/);

  await runCommand(page, "Toggle theme");
  await expect.poll(() => themeAttribute(page)).toBe(before);
});

test("Alt+M moves the dock instead of teleporting it", async ({ page }) => {
  await recordAnimations(page);
  await openApp(page);

  // The generated `linear()` is a real easing function or it is nothing: an
  // easing the browser cannot parse makes the whole `transition` declaration
  // invalid, and the property silently stops animating. Read it back.
  const chipGlyph = page.locator(".wb-layout-chip svg");
  await expect(chipGlyph).toHaveCSS("transition-timing-function", /^linear\(/);
  expect(seconds(await chipGlyph.evaluate((el) => getComputedStyle(el).transitionDuration))).toBe(
    0.19,
  );

  await treeItem(page, "src").click();

  await page.keyboard.press("Alt+M");
  await expect(page.locator(".wb-layout-chip")).toHaveText("Focused");

  const entering = (await animations(page)).filter((call) => call.className.includes("wb-dock"));
  expect(entering.length, "focus mode animated the dock").toBe(1);
  // Composited properties only — the whole point of animating the dock rather
  // than the grid geometry dockview just changed.
  expect([...entering[0].properties].sort()).toEqual(["opacity", "transform"]);
  expect(entering[0].duration).toBeGreaterThan(0);
  expect(entering[0].easing, "the base spring, from the tokens").toContain("linear(");
  expect(entering[0].keyframes[0].transform, "it grows into the window").not.toBe("scale(1)");

  await page.keyboard.press("Alt+M");
  await expect(page.locator(".wb-layout-chip")).not.toHaveText("Focused");
  expect((await animations(page)).filter((call) => call.className.includes("wb-dock")).length).toBe(
    2,
  );

  // A layout switch is a different event and says so: opacity only, no zoom.
  // From a settled dock — a switch that interrupts a zoom continues it (the next
  // test), which would make this assertion depend on driver timing.
  await dockSettled(page);
  await runCommand(page, "Switch to the Review layout");
  const switched = (await animations(page)).filter((call) => call.className.includes("wb-dock"));
  expect(switched.length).toBe(3);
  expect(switched[2].keyframes[0].transform, "panels did not fly anywhere").toBe("scale(1)");
  expect(switched[2].duration).toBeLessThan(entering[0].duration);

  await runCommand(page, "Switch to the Default layout");
});

test("a second Alt+M mid-flight carries on instead of snapping back", async ({ page }) => {
  await recordAnimations(page);
  await openApp(page);
  await treeItem(page, "src").click();

  // Focus mode, then focus mode again two frames later — a real double press, and
  // 33 ms into an animation that has 300 ms to run. DESIGN.md §5.1.1: "a second
  // input mid-flight resolves into the first instead of queueing behind it".
  // Nothing is awaited on the app between the two presses but the frames
  // themselves: an assertion in there is a driver round trip, and a round trip
  // long enough to outlast the animation would make the test vacuous.
  await page.keyboard.press("Alt+M");
  await twoFrames(page);
  await page.keyboard.press("Alt+M");
  await expect(page.locator(".wb-layout-chip")).not.toHaveText("Focused");

  const dock = (await animations(page)).filter((call) => call.className.includes("wb-dock"));
  expect(dock.length, "each press animated the dock").toBe(2);
  // Everything below is about an *interrupted* animation, so fail loudly rather
  // than pass vacuously if the second press somehow arrived after the first had
  // already settled.
  expect(
    dock[1].at - dock[0].at,
    "the second press has to land inside the first animation for this to mean anything",
  ).toBeLessThan(dock[0].duration);

  // The regression: the second call replayed the *fixed* dip, so a dock already
  // most of the way back to opacity 1 / scale 1 dropped to 0.62 / 0.985 and
  // climbed again — a backward pop, mid-gesture. It must start from where the
  // first animation had got to, which is strictly past that dip and short of the
  // target it is still travelling towards.
  const from = dock[1].keyframes[0];
  const dip = dock[0].keyframes[0];
  expect(Number(from.opacity), "it carried on from where the dock was").toBeGreaterThan(
    Number(dip.opacity),
  );
  expect(Number(from.opacity), "and it had not arrived yet").toBeLessThan(1);
  const scaleOf = (frame: Record<string, string>): number =>
    Number(/scale\(([\d.]+)\)/.exec(frame.transform)?.[1]);
  expect(scaleOf(from), "the zoom continued too").toBeGreaterThan(scaleOf(dip));
  expect(scaleOf(from)).toBeLessThan(1);
});

test("the QuickBar leaves rather than vanishing", async ({ page }) => {
  await openApp(page);
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.locator(".wb-qb");
  await expect(quickbar).toBeVisible();

  // Watched from inside the page rather than polled from outside: the exit is
  // `--motion-exit-ms` long, and an assertion that has to arrive inside 120 ms
  // over a driver round trip is a flake waiting for a slow machine.
  await page.evaluate(() => {
    const state = { leaving: false, inert: false };
    (window as unknown as { __wbExit: typeof state }).__wbExit = state;
    new MutationObserver(() => {
      const element = document.querySelector<HTMLElement>(".wb-qb.is-leaving");
      if (element === null) return;
      state.leaving = true;
      state.inert ||= getComputedStyle(element).pointerEvents === "none";
    }).observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["class"],
    });
  });

  await page.keyboard.press("Escape");
  await expect(quickbar).toHaveCount(0);

  const exit = await page.evaluate(
    () => (window as unknown as { __wbExit: { leaving: boolean; inert: boolean } }).__wbExit,
  );
  expect(exit.leaving, "it was on screen, marked as leaving, before it went").toBe(true);
  expect(exit.inert, "and it stopped taking clicks the moment it started to").toBe(true);
});

test("reduced motion zeroes the travel and keeps the tint", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await recordAnimations(page);
  await openApp(page);

  await test.step("every travel distance is zero", async () => {
    expect(await token(page, "--motion-rise")).toBe("0px");
    expect(await token(page, "--motion-lift")).toBe("0px");
    expect(await token(page, "--motion-scale-in")).toBe("1");
    expect(await token(page, "--motion-zoom-in")).toBe("1");
  });

  await test.step("and every transform transition with it", async () => {
    for (const name of ["--motion-move-snap", "--motion-move"]) {
      expect(seconds((await token(page, name)).split(" ")[0]), name).toBe(0);
    }
  });

  await test.step("but colour and opacity still animate", async () => {
    // The half the crude `* { transition-duration: 1ms }` version destroys.
    // Compared as numbers: the browser is free to serialise `110ms` as `.11s`.
    expect(seconds(await token(page, "--motion-tint-ms"))).toBeCloseTo(0.11, 3);
    expect(seconds(await token(page, "--motion-exit-ms"))).toBeCloseTo(0.12, 3);
    const enter = await token(page, "--motion-enter");
    expect(seconds(enter.split(" ")[0])).toBeGreaterThan(0);
    const button = page.locator(".wb-btn").first();
    if ((await button.count()) > 0) {
      expect(await button.evaluate((el) => getComputedStyle(el).transitionDuration)).not.toMatch(
        /^0s(, 0s)*$/,
      );
    }
  });

  await test.step("and focus mode fades instead of zooming", async () => {
    await treeItem(page, "src").click();
    await page.keyboard.press("Alt+M");
    await expect(page.locator(".wb-layout-chip")).toHaveText("Focused");
    const call = (await animations(page)).filter((c) => c.className.includes("wb-dock"))[0];
    expect(call.keyframes[0].transform, "no travel").toBe("scale(1)");
    expect(call.keyframes[0].opacity, "the fade survives").not.toBe("1");
    await page.keyboard.press("Alt+M");
    await expect(page.locator(".wb-layout-chip")).not.toHaveText("Focused");
  });

  await page.emulateMedia({ reducedMotion: null });
});
