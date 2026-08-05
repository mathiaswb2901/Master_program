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
 * | after  | 976 / 1,209 / 1,145 / 1,134 ms | ~440 ms | 2, ~200 ms |
 *
 * The ceiling is ~2.5x today's number. It is a ratchet, not a target: the
 * target is a file tree that is there before the user's hand leaves the mouse,
 * and every Feel PR that gets closer is expected to lower this line in the same
 * diff. Note what the table does *not* show moving — first contentful paint and
 * the long-task total are unchanged, because the bytes and the parse are the
 * next problem and this PR did not touch them.
 */
const LAUNCH_CEILING_MS = 3_000;

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
