/**
 * Budget — the continuous case: expanding and scrolling a 2,000-file directory.
 *
 * Launch is one number a user forms an opinion from; scrolling is the other,
 * and it is the one no event-timing entry catches. Nothing here is "slow" by
 * the 200 ms standard — the complaint is a rhythm: a scroll where every third
 * frame is late reads as *cheap*, however fast the click that started it was.
 * So this samples `requestAnimationFrame` intervals across a real wheel scroll
 * and reports the tail (p95, longest, frames over 50 ms), which is what a
 * person actually notices.
 *
 * The fixture's flat 2,000-file directory is the case on purpose: the file tree
 * renders every row of an expanded folder, with no virtualisation
 * (`ui/src/panels/FileTree.tsx`). That is a known design debt and this is the
 * measurement that will show it being paid off.
 *
 * `@wallclock`: frame timing on a shared CI runner is a report, not a gate. The
 * assertions here are catastrophe guards — a frame long enough to look frozen —
 * not the budget. The budget is the recorded number, tightened as the Feel
 * track lands.
 */

import { expect, test } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, readTelemetry, record, round, sampleFrames } from "./instrument";

/**
 * Measured 2026-08-05 on the author's machine, expanding the 2,000-file folder
 * and then scrolling it:
 *
 * * expand: **894 ms**, and it is **one 400 ms frame** — the whole folder is
 *   rendered in a single commit, so the app is visibly frozen for a quarter of
 *   a second. This is the virtualisation debt, stated as a number.
 * * scroll: p95 **16.8 ms**, longest **66.6 ms**, 2 of 359 frames over 50 ms —
 *   good, and it stays good only because nothing else is competing yet.
 * * slowest input event: **144 ms** (the expanding click itself).
 *
 * The two constants below are catastrophe guards sized well above those, not
 * the budget. The budget is the recorded line, and the next Feel PR that
 * touches the tree is expected to move `expand` by an order of magnitude.
 */
const FROZEN_FRAME_MS = 1_500;
const EXPAND_CEILING_MS = 5_000;

test("expanding and scrolling 2,000 rows keeps its frames", { tag: "@wallclock" }, async ({
  page,
}, info) => {
  await installTelemetry(page);
  await page.goto("/");
  const flat = page.getByRole("treeitem", { name: "flat", exact: true });
  await expect(flat).toBeVisible();

  const startedAt = Date.now();
  const expandFrames = await sampleFrames(page, async () => {
    await flat.click();
    await expect(page.getByRole("treeitem", { name: "item_1999.txt", exact: true })).toBeAttached();
  });
  const expandMs = Date.now() - startedAt;

  const tree = page.locator(".wb-filetree");
  await tree.hover();
  const scrollFrames = await sampleFrames(page, async () => {
    for (let i = 0; i < 40; i++) {
      await page.mouse.wheel(0, 240);
      // Paced, not waited on: a wheel gesture *is* spread over time, and firing
      // 40 events in one task would measure a burst nobody performs. This is
      // the only place in the suite where a duration is the point.
      await page.waitForTimeout(16);
    }
  });

  const telemetry = await readTelemetry(page);
  const slowestEvent = telemetry.events.reduce((worst, e) => (e.duration > worst ? e.duration : worst), 0);

  await record(
    info,
    "flat-directory-interaction",
    `expand ${expandMs} ms (p95 frame ${expandFrames.p95Ms} ms, longest ${expandFrames.longestMs} ms); ` +
      `scroll p95 ${scrollFrames.p95Ms} ms, longest ${scrollFrames.longestMs} ms, ` +
      `${scrollFrames.over50ms}/${scrollFrames.frames} frames over 50 ms; ` +
      `slowest input event ${round(slowestEvent)} ms`,
    { fixture: FIXTURE, expandMs, expandFrames, scrollFrames, telemetry },
  );

  expect(expandMs).toBeLessThan(EXPAND_CEILING_MS);
  expect(expandFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
  expect(scrollFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
});
