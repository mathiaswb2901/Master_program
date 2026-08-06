/**
 * Budget — the click that opens a file, from mouse-down to text on screen.
 *
 * This is the counterweight to `launch.spec.ts`, and it exists because the
 * cheapest way to make launch fast is to make the first file open slow: move
 * Monaco out of the entry chunk and the bytes, the parse and the first editor
 * construction all move onto the user's first click instead. Trading a fast
 * launch for a slow first open is not a win, and without this number nobody
 * would notice the trade had been made.
 *
 * Two measurements, because the two ends of the trade look different:
 *
 * * **cold** — clicked the instant the tree has rows. Nobody is this fast; it
 *   is the worst case the change can produce, and it *races the prefetch*, so
 *   its number is bimodal by construction (see the table below). Recorded, with
 *   only a catastrophe guard on it — a bimodal number makes a bad ceiling.
 * * **prefetched** — clicked after the editor's chunk has actually arrived,
 *   which is what an idle-time prefetch is *for*. This one is where the tight
 *   ceiling goes, and it also asserts that a chunk arrived **before the click at
 *   all**: delete the prefetch and nothing fetches Monaco until the user asks,
 *   so that assertion fails while every millisecond budget still passes.
 *
 * `@wallclock` on both: these are milliseconds on a shared runner, so CI
 * records them and the counts in `bundle.spec.ts` are what blocks a merge.
 */

import { expect, type Page } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, mark, readTelemetry, record, round } from "./instrument";
import { test } from "./window";

/**
 * Measured 2026-08-05 on the author's machine (Win11, production build served
 * by `vite preview`, backend in the pinned 5,005-file fixture), opening
 * `notes.md`, four runs each, medians:
 *
 * | | cold | prefetched |
 * |---|---|---|
 * | Monaco in the entry chunk | 151 ms | 153 ms |
 * | Monaco lazy, **no** prefetch | 706 ms | 661 ms |
 * | Monaco lazy + idle prefetch | 149 ms | 152 ms |
 *
 * The middle row is why the prefetch exists and why it is not a nicety: making
 * launch fast by moving 3.3 MB onto the user's first click costs **half a
 * second** on that click, every time, and would have been a straight trade of
 * one complaint for another. Warmed on an idle callback instead, the first open
 * is within noise of what it was when Monaco was in the entry chunk — the same
 * click, for a launch that paints 4x sooner.
 *
 * **Where the cold number is honest about itself.** 149 ms is the median of a
 * click that arrived *after* the prefetch. On a contended run the click gets
 * there first and the same test measures **up to ~540 ms** — the full chunk
 * load, on the user's click, which is exactly the middle row. A test clicking
 * ~10 ms after the first tree row exists is faster than any hand; the number to
 * plan against is that a machine slow enough will occasionally show a user the
 * "Loading editor…" line for a third of a second on their very first open.
 */
const OPEN_CEILING_MS = 1_500;
/** The prefetched path is single-moded, so it carries the real budget. */
const PREFETCHED_CEILING_MS = 500;

/** The first row a user clicks: top level, no folder to expand first. */
const FILE = "notes.md";

/**
 * Has the editor's own chunk been fetched? Asked of resource timing rather than
 * of the app, so the same question is answerable on a build where the answer is
 * permanently *no* — which is exactly the build this change is measured against.
 * Returns false rather than throwing when nothing matching ever loads.
 */
async function editorChunkLoaded(page: Page, timeoutMs: number): Promise<boolean> {
  try {
    await page.waitForFunction(
      () =>
        performance
          .getEntriesByType("resource")
          .some((entry) => /monaco/i.test(entry.name) && entry.name.endsWith(".js")),
      undefined,
      { timeout: timeoutMs },
    );
    return true;
  } catch {
    return false;
  }
}

/** Click the file row and return the ms from the click to painted text. */
async function timeFirstOpen(page: Page): Promise<number> {
  await mark(page, "open");
  await page.getByRole("treeitem", { name: FILE, exact: true }).click();
  await expect(page.locator(".wb-editor-body .monaco-editor .view-lines")).toBeVisible();
  const telemetry = await readTelemetry(page);
  const started = telemetry.marks.open;
  const ready = telemetry.editorReady;
  expect(started, "the open mark was never stamped").not.toBeUndefined();
  expect(ready, "Monaco never painted a line").not.toBeNull();
  return (ready ?? 0) - started;
}

test("first file open, clicked the moment the tree is ready", { tag: "@wallclock" }, async ({
  page,
}, info) => {
  await installTelemetry(page);
  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: FILE, exact: true })).toBeVisible();

  const openMs = await timeFirstOpen(page);
  const telemetry = await readTelemetry(page);
  const worst = telemetry.longTasks.reduce((max, task) => Math.max(max, task.duration), 0);

  await record(
    info,
    "first-file-open-cold",
    `${round(openMs)} ms from click to painted text; longest task ${round(worst)} ms`,
    { fixture: FIXTURE, openMs, telemetry },
  );

  expect(openMs).toBeLessThan(OPEN_CEILING_MS);
});

test("first file open, after the editor chunk has arrived", { tag: "@wallclock" }, async ({
  page,
}, info) => {
  await installTelemetry(page);
  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: FILE, exact: true })).toBeVisible();
  // Bounded: on a build with no separate editor chunk this waits and finds
  // nothing, which is a result to record rather than a failure.
  const prefetched = await editorChunkLoaded(page, 10_000);

  const openMs = await timeFirstOpen(page);

  await record(
    info,
    "first-file-open-prefetched",
    `${round(openMs)} ms from click to painted text` +
      (prefetched ? " (editor chunk already loaded)" : " (no separate editor chunk was loaded)"),
    { fixture: FIXTURE, openMs, prefetched, telemetry: await readTelemetry(page) },
  );

  expect(
    prefetched,
    "no editor chunk arrived before the click — the idle prefetch is gone, and " +
      "the first file open now costs the user the whole download",
  ).toBe(true);
  expect(openMs).toBeLessThan(PREFETCHED_CEILING_MS);
});
