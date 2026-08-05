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
 * The fixture's flat 2,000-file directory is the case on purpose. It used to be
 * the panel's worst moment: the tree rendered a real `<button>` subtree per
 * expanded node, recursively, so opening this folder took the row count from 10
 * to 2,010 in one commit and froze the window for a quarter of a second. The
 * mounted-row budget below is what says that is over — and it is a **count**,
 * so it blocks in CI while the frame numbers are recorded.
 */

import { expect, test } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, readTelemetry, record, round, sampleFrames } from "./instrument";

/**
 * Measured 2026-08-05 on the author's machine, expanding the 2,000-file folder
 * and then scrolling it:
 *
 * | | expand wall-clock | worst frame | worst long task | rows in the DOM |
 * |---|---|---|---|---|
 * | before virtualisation | 1,176 ms | 500 ms | ~500 ms | 2,010 |
 * | after | 150 ms | 16.8 ms | 0 (none) | 32 |
 *
 * Scrolling: p95 **33.4 → 16.8 ms**, longest **83.4 → 16.8 ms**, frames over
 * 50 ms **4 of 362 → 0 of 123**. Slowest input event **200 → 40 ms** — the
 * expanding click itself, which is what the audit called the one interaction
 * that visibly froze the window.
 *
 * The wall-clock constants below are ratcheted to roughly 5x and 15x the
 * measured numbers, not to 1x: they are catastrophe guards on a shared runner.
 * The budget is `MAX_MOUNTED_ROWS`, which cannot flake, plus the recorded line.
 */
const FROZEN_FRAME_MS = 250;
const EXPAND_CEILING_MS = 800;

/**
 * Rows the DOM may hold after expanding a 2,000-child directory.
 *
 * A screenful at 26 px is ~30 rows in this panel at the default window size,
 * plus 8 rows of overscan at each end. 60 is that with room to breathe, and it
 * is two orders of magnitude below the 2,010 this used to mount — so it fails
 * on the thing it is guarding (a tree that renders what it cannot show) and not
 * on a viewport that came out fifty pixels taller on someone's machine.
 */
const MAX_MOUNTED_ROWS = 60;

/** A single main-thread task long enough to be seen as a freeze. Expanding is
 * one click; it has no business blocking a frame at all now that the row count
 * is independent of the folder's size. */
const EXPAND_LONG_TASK_MS = 100;

test("expanding a 2,000-file folder renders a screenful, not the folder", async ({ page }, info) => {
  await installTelemetry(page);
  await page.goto("/");
  const flat = page.getByRole("treeitem", { name: "flat", exact: true });
  await expect(flat).toBeVisible();

  const before = await page.locator('[role="treeitem"]').count();
  await flat.click();
  // The first child is the app's own signal that the expansion landed.
  await expect(page.getByRole("treeitem", { name: "item_0000.txt", exact: true })).toBeVisible();
  const mounted = await page.locator('[role="treeitem"]').count();

  // The rows that are *not* mounted must still be reachable — a virtualiser
  // that renders a screenful and cannot scroll to the rest is not a fix.
  await page.locator(".wb-filetree").evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  await expect(page.getByRole("treeitem", { name: "item_1999.txt", exact: true })).toBeVisible();
  const atBottom = await page.locator('[role="treeitem"]').count();

  await record(
    info,
    "flat-directory-rows",
    `${before} rows before, ${mounted} after expanding 2,000 children, ${atBottom} at the bottom`,
    { fixture: FIXTURE, before, mounted, atBottom, children: 2_000 },
  );

  expect(mounted).toBeLessThanOrEqual(MAX_MOUNTED_ROWS);
  expect(atBottom).toBeLessThanOrEqual(MAX_MOUNTED_ROWS);
  // And the last row is really the last: the spacer is the full height, so the
  // scrollbar tells the truth about how much is there.
  expect(await page.locator(".wb-tree-viewport").evaluate((el) => el.clientHeight)).toBeGreaterThan(
    2_000 * 26 - 1,
  );
});

test("expanding and scrolling 2,000 rows keeps its frames", { tag: "@wallclock" }, async ({
  page,
}, info) => {
  await installTelemetry(page);
  await page.goto("/");
  const flat = page.getByRole("treeitem", { name: "flat", exact: true });
  await expect(flat).toBeVisible();

  const beforeExpand = (await readTelemetry(page)).longTasks.length;
  const startedAt = Date.now();
  const expandFrames = await sampleFrames(page, async () => {
    await flat.click();
    await expect(page.getByRole("treeitem", { name: "item_0000.txt", exact: true })).toBeVisible();
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
  const expandTasks = telemetry.longTasks.slice(beforeExpand);
  const worstExpandTask = expandTasks.reduce((worst, t) => (t.duration > worst ? t.duration : worst), 0);

  await record(
    info,
    "flat-directory-interaction",
    `expand ${expandMs} ms (p95 frame ${expandFrames.p95Ms} ms, longest ${expandFrames.longestMs} ms, ` +
      `worst long task ${round(worstExpandTask)} ms); ` +
      `scroll p95 ${scrollFrames.p95Ms} ms, longest ${scrollFrames.longestMs} ms, ` +
      `${scrollFrames.over50ms}/${scrollFrames.frames} frames over 50 ms; ` +
      `slowest input event ${round(slowestEvent)} ms`,
    { fixture: FIXTURE, expandMs, expandFrames, scrollFrames, expandTasks, telemetry },
  );

  expect(expandMs).toBeLessThan(EXPAND_CEILING_MS);
  expect(expandFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
  expect(scrollFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
  // The one that used to fail: a single 400–500 ms task rendering 2,000 rows.
  expect(worstExpandTask).toBeLessThan(EXPAND_LONG_TASK_MS);
});
