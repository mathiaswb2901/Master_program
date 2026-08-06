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

import { expect } from "@playwright/test";

import { FIXTURE } from "./fixture";
import {
  cumulativeLayoutShift,
  installTelemetry,
  readTelemetry,
  record,
  round,
} from "./instrument";
import { test } from "./window";

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
 *
 * **The bytes and the parse, 2026-08-05** — Monaco off the critical path (the
 * entry chunk drops from 1,030 kB gzipped to 191 kB) and `main.tsx` no longer
 * waiting for `window.load` before it renders. Same machine, `WB_PERF_WORKSPACE`
 * pinned so all eight runs measure one warm fixture, four runs each, medians:
 *
 * | | tree rows | FCP | DCL | long tasks |
 * |---|---|---|---|---|
 * | before | 1,566 ms | 618 ms | 434 ms | 2, ~290 ms, both before FCP |
 * | after  |   903 ms | 160 ms |  89 ms | 2, ~230 ms, all after FCP |
 *
 * Read the last column: the blocking total barely moved, but it moved to the
 * *other side of first paint* — what used to be Monaco parsing before the app
 * could draw anything is now Monaco parsing on an idle callback while the user
 * looks at their file tree.
 *
 * Both rows are the same commit with and without the change, which is what
 * makes the difference attributable. Rebased onto master with the terminal and
 * visual-artifacts lanes underneath, the same three runs read: cold FCP 172 ms,
 * DCL 99 ms, tree rows 1,019 ms; warm FCP 116 ms, DCL 56 ms. Machine noise on
 * this box is worth ±50 ms on a paint and ±200 ms on a row, so compare medians
 * of several runs against a *pinned* `WB_PERF_WORKSPACE`, never single runs
 * against a fixture each run rebuilt.
 *
 * The ceiling comes down to ~2.2x the measured number. It is not 2.2x of
 * something that got 4x faster: what a *row* costs is still dominated by
 * `GET /api/files/tree` walking 5,005 files and by rendering them
 * unvirtualised, and two queued Feel items own that. This one owns the paint.
 *
 * **What the lane was doing to this number, 2026-08-06.** Until `./window`
 * landed, every spec here started in whatever window the spec before it had
 * saved, so this test was timing a *restore* as often as a launch. Same machine,
 * pinned `WB_PERF_WORKSPACE`, `.workbench/` wiped, full lane, before and after
 * that change: tree rows **714.9 → 686.6 ms**, FCP **700 → 672 ms**, DCL
 * **68.4 → 64.1 ms** — inside this box's noise, which is the point: the reset is
 * one PUT on an API context before the page navigates, and it does not show up
 * in what this file measures.
 *
 * One number it did *not* fix is below. And when comparing runs, compare
 * like-for-like on the *server*, not just the workspace: the first page load
 * after `vite preview` starts pays for a module graph nothing has requested yet
 * (measured on this box the same afternoon: FCP **1,384 ms** as the run's first
 * navigation, **156 ms** as its second), so a spec run alone is not comparable
 * with the same spec seventh in a lane.
 */
const LAUNCH_CEILING_MS = 1_500;

/**
 * Rendering before the webfonts arrive is what buys the paint above, and the
 * bill for it would be a shell that reflows under the user a moment later.
 * `@fontsource` ships `font-display: swap`, so the swap is real; the shell's
 * geometry is grid/flex sized in `px` tokens and does not depend on it, which
 * is why the measured shift did not move at all — **0.003 before, 0.003 after**,
 * one sub-pixel shift from the status bar in both. This is the assertion that
 * keeps it that way: a layout whose height comes from text would start failing
 * here, and the fix would be `font-display`/`size-adjust`, not the gate again.
 *
 * **It is currently catching something else, and the something else is real.**
 * A workspace that has *any* agent session shifts **0.064** here — 20x the
 * measurement above and 3x the ceiling. The session picker is `flex: none;
 * max-height: 40%` (`ui/src/styles/agent.css`), so its height is its content's:
 * `GET /api/agents/sessions` answers after first paint, a row replaces the "No
 * sessions yet" line, and everything below it in the pane moves. Measured
 * 2026-08-06 on one commit, one fixture, one session apart — `launch` alone
 * against a fresh backend **0.003**, `activity.spec.ts` then `launch` **0.064**.
 * Every returning user is the second case, so this is a reflow the product owes
 * a fix (reserve the picker's box), not a ceiling this file owes a raise.
 *
 * It is also the last ordering coupling in the lane: `./window` resets the saved
 * *arrangement*, but a session lives in the backend the whole run shares and no
 * spec can delete one. Run this spec alone when you need the launch number
 * itself; leave the failure standing until the pane stops moving.
 */
const LAYOUT_SHIFT_CEILING = 0.02;

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
  const shift = cumulativeLayoutShift(telemetry);

  await record(
    info,
    "cold-launch",
    `tree rows at ${treeReady === null ? "never" : round(treeReady)} ms, ` +
      `FCP ${fcp === null ? "n/a" : round(fcp)} ms, ` +
      `DCL ${telemetry.navigation === null ? "n/a" : round(telemetry.navigation.domContentLoaded)} ms, ` +
      `${telemetry.longTasks.length} long tasks totalling ${round(blocked)} ms, ` +
      `layout shift ${shift.toFixed(3)}`,
    { fixture: FIXTURE, telemetry },
  );

  expect(treeReady, "no file row ever appeared").not.toBeNull();
  expect(treeReady ?? Infinity).toBeLessThan(LAUNCH_CEILING_MS);
  expect(shift, "the shell reflowed after first paint").toBeLessThan(LAYOUT_SHIFT_CEILING);
});

/**
 * The same launch with the bytes already on disk — every start after the first,
 * and in the shell every reload of a running window. It is the number that
 * isolates *evaluating* the entry chunk from downloading it, which is why it is
 * measured separately rather than inferred from the cold one.
 *
 * Measured 2026-08-05, same machine, four runs each, medians: FCP **158 ms**
 * before and **84 ms** after; DCL **61 ms** before and **27 ms** after; tree
 * rows 915 ms before and 822 ms after (that last one is the server's walk, as
 * above). Warm is where the entry chunk's *parse* shows on its own, with no
 * download in the way, and it more than halved.
 */
test("a warm reload paints from cache", { tag: "@wallclock" }, async ({ page }, info) => {
  await installTelemetry(page);
  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: "notes.md", exact: true })).toBeVisible();

  // A reload is a new document with a new timeline, so everything read below
  // describes the *second* navigation — with the first one's cache behind it.
  await page.reload();
  await expect(page.getByRole("treeitem", { name: "notes.md", exact: true })).toBeVisible();

  const telemetry = await readTelemetry(page);
  const fcp = telemetry.paints.find((p) => p.name === "first-contentful-paint")?.startTime ?? null;

  await record(
    info,
    "warm-launch",
    `tree rows at ${telemetry.treeReady === null ? "never" : round(telemetry.treeReady)} ms, ` +
      `FCP ${fcp === null ? "n/a" : round(fcp)} ms, ` +
      `DCL ${telemetry.navigation === null ? "n/a" : round(telemetry.navigation.domContentLoaded)} ms`,
    { fixture: FIXTURE, telemetry },
  );

  expect(telemetry.treeReady, "no file row ever appeared").not.toBeNull();
  expect(telemetry.treeReady ?? Infinity).toBeLessThan(LAUNCH_CEILING_MS);
});
