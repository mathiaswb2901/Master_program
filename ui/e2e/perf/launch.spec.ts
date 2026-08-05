/**
 * Budget — cold launch to a clickable file tree, on the 5,005-file fixture.
 *
 * The complaint this lane was opened for: "an old sluggish feel". Launch is
 * where a user forms that judgement, and it is the one number nobody can argue
 * with, so it gets a ratchet: a ceiling generously above today's measurement,
 * tightened by each PR that earns it. The ceiling is deliberately loose —
 * blocking a merge on a millisecond count measured on a shared runner is how a
 * perf gate gets disabled — which is why this test carries `@wallclock` and CI
 * records it rather than blocking on it.
 *
 * "Clickable", not "painted": the row is clicked and the editor tab it opens is
 * waited for. A tree that renders and then blocks the main thread for a second
 * has not launched.
 */

import { expect, test } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, readTelemetry, record, round } from "./instrument";

/**
 * Measured 2026-08-05 on the author's machine (Win11, production build served
 * by `vite preview`, backend in the 5,005-file fixture), four runs:
 *
 * | | tree rows | FCP | long tasks |
 * |---|---|---|---|
 * | before the double-walk fix | 1,569 / 1,659 / 1,588 ms | ~440 ms | 2, ~200 ms |
 * | after it (PR #35) | 976 / 1,209 / 1,145 / 1,134 ms | ~440 ms | 2, ~200 ms |
 * | this PR's own base (bb447b6) | 1,710 ms | ~840 ms | 2, ~354 ms |
 * | after the lazy tree | 990 ms | ~670 ms | 4, ~490 ms |
 *
 * The last pair is this PR, measured back to back on the same commit range:
 * the first paint of the tree no longer waits on a recursive walk of the
 * workspace. It waits on one `os.scandir` of the root — **1.7 ms and 0.4 KB**
 * against the 5,005-file fixture, where the walk it replaced was **494 ms and
 * 471 KB** (measured in-process, 20 requests each). Note the base moved *up*
 * from PR #35's row while this branch was out: visual artifacts and Word
 * hosting landed on master and the entry chunk grew. That is the point of
 * measuring the base rather than quoting the last recorded number.
 *
 * The ceiling is a ratchet, not a target: the target is a file tree that is
 * there before the user's hand leaves the mouse, and every Feel PR that gets
 * closer is expected to lower this line in the same diff. Note what the table
 * still does *not* show moving — first contentful paint and the long-task total,
 * because those are the entry chunk (4.1 MB raw, ~88% Monaco) and no file-tree
 * work touches them. That is the next item in the track.
 */
const LAUNCH_CEILING_MS = 1_500;

test("cold launch reaches a clickable file tree", { tag: "@wallclock" }, async ({ page }, info) => {
  await installTelemetry(page);
  await page.goto("/");

  // The three top-level folders the fixture guarantees, then a file that opens.
  for (const folder of FIXTURE.top_level_dirs) {
    await expect(page.getByRole("treeitem", { name: folder, exact: true })).toBeVisible();
  }
  await page.getByRole("treeitem", { name: "notes.md", exact: true }).click();
  await expect(page.locator(".wb-editor-tab").filter({ hasText: "notes.md" })).toBeVisible();

  const telemetry = await readTelemetry(page);
  const treeReady = telemetry.treeReady;
  const fcp = telemetry.paints.find((p) => p.name === "first-contentful-paint")?.startTime ?? null;
  const blocked = telemetry.longTasks.reduce((total, task) => total + task.duration, 0);

  await record(
    info,
    "cold-launch",
    `tree rows at ${treeReady === null ? "never" : round(treeReady)} ms, ` +
      `FCP ${fcp === null ? "n/a" : round(fcp)} ms, ` +
      `${telemetry.longTasks.length} long tasks totalling ${round(blocked)} ms`,
    { fixture: FIXTURE, telemetry },
  );

  expect(treeReady, "no file row ever appeared").not.toBeNull();
  expect(treeReady ?? Infinity).toBeLessThan(LAUNCH_CEILING_MS);
});
