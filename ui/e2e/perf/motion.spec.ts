/**
 * Budget — the motion layer: what it ships, and what it costs.
 *
 * The source-stylesheet half of the conformance test is `motion.test.ts`
 * (vitest, no browser, runs on `npm run test`). This file holds the two halves
 * that need the production build and a real browser:
 *
 * **The bundle.** Third-party stylesheets are shipped to the user as surely as
 * ours, and dockview and Monaco both animate layout properties. Those are read,
 * neutralised where we can reach them, and pinned in {@link VENDOR_LEDGER} —
 * which must match *exactly*, so a dependency bump that adds one fails here
 * rather than in someone's frame rate.
 *
 * **The cost.** A motion PR that introduces jank has failed at its own goal, so
 * the two interactions this vocabulary animates — focus mode (`Alt+M`) and a
 * layout switch — are sampled with the lane's rAF sampler and its
 * long-animation-frame observer.
 *
 * On "no frame over 16.7 ms", which is the budget this was asked for: a rAF
 * sampler cannot express it, and the two obvious restatements are both wrong.
 *
 *  * Literally — a 60 Hz frame is 16.667 ms and `performance.now()` is
 *    quantised, so an idle browser reports a mix of 16.7 and 16.8 ms intervals.
 *    The *unmodified* app scored 13 of 45 frames "over 16.7 ms" before any of
 *    this landed. The budget would fail on an app with no motion at all.
 *  * As a p95 — an interaction is ~43 sampled frames, so the 95th percentile is
 *    the third-worst one. Two frames lost to whatever else the machine is doing
 *    move it from 16.7 to 33.4. Measured: it passed three runs and failed the
 *    fourth, on identical code, when the rest of the lane ran first.
 *
 * So the gate is the **median** interval, which is what "every frame arrives on
 * time" actually means and is robust to a busy host — anything that animates a
 * property costing style, layout or paint moves it immediately, because it
 * costs that on *every* frame. Alongside it, two counts: no frame long enough
 * to look stuck, and no long animation frame at all. The tail is recorded.
 * `@wallclock` because the recorded numbers are a shared-runner report.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, type Page } from "@playwright/test";

import { ANIMATABLE, motionDeclarations } from "./css";
import { FIXTURE } from "./fixture";
import { installTelemetry, readTelemetry, record, sampleFrames, type FrameStats } from "./instrument";
import { test } from "./window";

const UI_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "..");

/**
 * Third-party rules that animate something the allowlist forbids.
 *
 * Every entry has been read. They fall into three groups.
 *
 *  1. **Overridden.** The two drop-target rules are neutralised in
 *     `ui/src/styles/dockview.css` (opacity only); the source rules survive in
 *     the bundle because a later override cannot delete an earlier line.
 *  2. **Unreachable.** The `dv-tab--*` and `dv-tab-group-chip--*` rules are
 *     dockview 7's `tabAnimation: 'smooth'` tab-drag mode and its tab-group
 *     chips. Every class in them is applied behind a `tabAnimation === 'smooth'`
 *     check, and `App.tsx` states `tabAnimation: 'default'` on the theme rather
 *     than relying on that being the library default — which is what makes
 *     "unreachable" a property of this app rather than of this dockview
 *     version. Tab groups are never created at all (`createTabGroup` is not
 *     called anywhere).
 *  3. **Monaco's own paint area**, where they cost the editor and not the app's
 *     layout.
 *
 * Asserted for equality in both directions: an entry that stops appearing is as
 * much a failure as one that appears, because a stale quarantine is a
 * quarantine nobody re-reads.
 */
const VENDOR_LEDGER: Record<string, string[]> = {
  // (1) overridden
  ".dv-drop-target>.dv-drop-target-dropzone>.dv-drop-target-selection": [
    "top",
    "left",
    "width",
    "height",
  ],
  ".dv-drop-target-container .dv-drop-target-anchor": ["top", "left", "width", "height"],
  // (2) unreachable: `tabAnimation: 'smooth'` only, and tab groups
  ".dv-tab.dv-tab--shifting": ["margin-left", "margin-right", "margin-top", "margin-bottom"],
  ".dv-tab.dv-tab--dragging,.dv-tab.dv-tab--group-collapsed": ["width", "padding", "margin"],
  ".dv-tab.dv-tab--group-expanding": ["width", "padding", "margin"],
  ".dv-tabs-container-vertical .dv-tab.dv-tab--dragging": ["height", "padding", "margin"],
  ".dv-tabs-container-vertical .dv-tab.dv-tab--group-collapsed": ["height", "padding", "margin"],
  ".dv-tabs-container-vertical .dv-tab.dv-tab--group-expanding": ["height", "padding", "margin"],
  ".dv-tab-group-chip.dv-tab-group-chip--shifting": ["margin-left"],
  ".dv-tab-group-chip.dv-tab-group-chip--dragging": ["width", "padding", "margin"],
  // (3) Monaco
  ".monaco-progress-container.discrete .progress-bit": ["width"],
  ".monaco-workbench:not(.reduce-motion) .monaco-tree-type-filter": ["top"],
  ".monaco-editor .cursors-layer.cursor-smooth-caret-animation>.cursor": ["all"],
};

/**
 * `will-change` promotes an element to its own compositor layer for good. A
 * handful is an optimisation; a page full is a memory cost paid every frame.
 * Workbench declares none — motion here promotes for the length of an animation
 * and hands the layer back — so this bound is the vendors' allowance, measured
 * at 8 and left a little room.
 */
const WILL_CHANGE_CEILING = 12;

/**
 * **Every** stylesheet the build emits, concatenated — not the first one.
 *
 * There is more than one now: Monaco's CSS follows Monaco's chunk off the
 * launch path (`ui/src/monaco.ts`), so the build writes `index-*.css` and
 * `monacoBundle-*.css`. Both are shipped to the user, which is the standard
 * this budget is written to; reading only whichever the directory listed first
 * silently stopped scanning the editor, and the three Monaco entries in
 * {@link VENDOR_LEDGER} went missing — a quarantine that had not been lifted,
 * only stopped being looked at. Same reason the `will-change` count reads them
 * both: a ceiling that ignores a shipped sheet is not a ceiling.
 */
function builtCss(): string {
  const assets = path.join(UI_ROOT, "dist", "assets");
  const files = fs.readdirSync(assets).filter((name) => name.endsWith(".css"));
  expect(files, "the production build emitted a stylesheet").not.toHaveLength(0);
  return files.map((name) => fs.readFileSync(path.join(assets, name), "utf-8")).join("\n");
}

test.describe("the shipped bundle", () => {
  test("animates no layout property but the pinned vendor ones", () => {
    const found: Record<string, string[]> = {};
    for (const declaration of motionDeclarations(builtCss())) {
      const bad = declaration.properties.filter(
        (property) => property !== "none" && !ANIMATABLE.has(property),
      );
      if (bad.length > 0) found[declaration.selector] = bad;
    }
    expect(found).toEqual(VENDOR_LEDGER);
  });

  test("keeps static will-change bounded", () => {
    const count = (builtCss().match(/will-change\s*:/g) ?? []).length;
    expect(count).toBeLessThanOrEqual(WILL_CHANGE_CEILING);
  });
});

// ---- what motion costs -------------------------------------------------------

/**
 * Long enough for the longest spring to settle plus a frame or two, so the
 * sampler is still running while the animation finishes. `--spring-bounce-ms`
 * is 590 ms; nothing here may outlast this.
 */
const SETTLE_WAIT_MS = 700;

/**
 * Median ceiling: one 60 Hz frame, plus the sampler's own quantisation.
 *
 * A 60 Hz frame is 16.667 ms and `performance.now()` is coarse, so an idle
 * browser reports a mix of 16.7 and 16.8 ms intervals. 16.9 is therefore "the
 * typical frame arrived on time" — see the file header for why this is the
 * median and not the tail.
 */
const MEDIAN_CEILING_MS = 16.9;

/** Long-animation-frame entries raised inside a window of the run. */
const loafsBetween = (
  entries: { startTime: number; duration: number }[],
  from: number,
  to: number,
): { startTime: number; duration: number }[] =>
  entries.filter((entry) => entry.startTime >= from && entry.startTime <= to);

const now = (page: Page): Promise<number> => page.evaluate(() => performance.now());

/** Run a QuickBar command by its row title — the same path a user takes. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

const summary = (label: string, stats: FrameStats): string =>
  `${label}: median ${stats.medianMs} ms, p95 ${stats.p95Ms} ms, ` +
  `longest ${stats.longestMs} ms, ${stats.over16ms}/${stats.frames} over 16.7 ms, ` +
  `${stats.over50ms} over 50 ms`;

test("focus mode and a layout switch stay inside a frame", { tag: "@wallclock" }, async ({
  page,
}, info) => {
  await installTelemetry(page);
  await page.goto("/");
  const seed = page.getByRole("treeitem", { name: "src", exact: true });
  await expect(seed).toBeVisible();
  // Focus a panel that is not the one the app activates for you, so `Alt+M`
  // has a real choice to make.
  await seed.click();

  const enterStart = await now(page);
  const enter = await sampleFrames(page, async () => {
    await page.keyboard.press("Alt+M");
    await expect(page.locator(".wb-layout-chip")).toHaveText("Focused");
    await page.waitForTimeout(SETTLE_WAIT_MS);
  });
  const exit = await sampleFrames(page, async () => {
    await page.keyboard.press("Alt+M");
    await expect(page.locator(".wb-layout-chip")).not.toHaveText("Focused");
    await page.waitForTimeout(SETTLE_WAIT_MS);
  });

  const switchStart = await now(page);
  const switched = await sampleFrames(page, async () => {
    await runCommand(page, "Switch to the Review layout");
    await page.waitForTimeout(SETTLE_WAIT_MS);
  });
  const switchEnd = await now(page);
  await runCommand(page, "Switch to the Default layout");

  const telemetry = await readTelemetry(page);
  const focusLoafs = loafsBetween(telemetry.longAnimationFrames, enterStart, switchStart);
  const switchLoafs = loafsBetween(telemetry.longAnimationFrames, switchStart, switchEnd);

  await record(
    info,
    "motion-interactions",
    `${summary("focus in", enter)}; ${summary("focus out", exit)}; ` +
      `${summary("layout switch", switched)}; ` +
      `long animation frames: ${String(focusLoafs.length)} (focus), ` +
      `${String(switchLoafs.length)} (switch)`,
    { fixture: FIXTURE, enter, exit, switched, focusLoafs, switchLoafs },
  );

  // A composited transform/opacity animation cannot produce a long animation
  // frame. One means something is being animated that costs layout or paint.
  expect(focusLoafs, "focus mode raised a long animation frame").toEqual([]);
  expect(switchLoafs, "the layout switch raised a long animation frame").toEqual([]);
  for (const [label, stats] of [
    ["focus in", enter],
    ["focus out", exit],
    ["layout switch", switched],
  ] as const) {
    expect(stats.frames, `${label} sampled no frames`).toBeGreaterThan(20);
    expect(stats.medianMs, `${label}: the typical frame missed`).toBeLessThanOrEqual(
      MEDIAN_CEILING_MS,
    );
    expect(stats.over50ms, `${label}: a frame long enough to look stuck`).toBe(0);
  }
});
