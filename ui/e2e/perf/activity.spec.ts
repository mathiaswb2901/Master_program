/**
 * Budget — a burst of agent output must not become a re-render storm.
 *
 * The fleet feed is a firehose by construction: every tool call of every session
 * publishes on the socket every window is listening to. The existing budgets in
 * this lane price the *file tree* (listings, rows, watcher frames) and the
 * *pane fleet* (mounting editors and terminals); none of them ever mounts a
 * panel that re-renders on agent output, so none of them would notice this one
 * regressing to one frame per tool call.
 *
 * So: open the Activity panel, run a Grep-heavy turn (`tool storm` — forty
 * announce/settle pairs with nothing between them), and count what actually
 * reaches the page.
 *
 * **The budget is a count, so it cannot flake.** Eighty changes must arrive as a
 * handful of `session_activity` frames; the uncoalesced design produces eighty.
 * The frame timings are recorded next to it, because they are what makes the
 * count mean something — but they are not what gates, which is why this test is
 * untagged and its numbers are an attachment rather than an assertion.
 */

import { expect, test, type Page } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, record, sampleFrames } from "./instrument";

/** Must match `STORM_TOOL_CALLS` in `services/fake_agent.py`. */
const STORM_TOOL_CALLS = 40;
/** Announce + settle. Every one is a change the server could publish. */
const CHANGES = STORM_TOOL_CALLS * 2;

/**
 * Measured 2026-08-06 on the author's machine, on the 5,005-file fixture: **80
 * changes reached the page as 4 `session_activity` frames** (16 `/ws/events`
 * frames in all — the rest are the session's own status and the watcher), with
 * **0 of 10 sampled frames over 50 ms** while the storm landed.
 *
 * Four rather than the two `test_activity.py` measures over the same storm,
 * because a browser also pays for the session being created and for the turn's
 * own text arriving; both are far below the eighty an uncoalesced feed produces,
 * which is the only distinction this budget is trying to make.
 *
 * The ceiling is a quarter of the changes rather than the measured number: what
 * it excludes is the *design* where every tool call is a frame, not a run that
 * coalesced one pair fewer than last time.
 */
const MAX_FRAMES = CHANGES / 4;
/** Catastrophe guard, not the budget (see `panes.spec.ts` for the same split). */
const FROZEN_FRAME_MS = 1_000;

interface FrameCounts {
  activity: number;
  events: number;
}

/** Count `/ws/events` frames by type, before a line of app code runs. */
async function countEventFrames(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const counts = { activity: 0, events: 0 };
    (window as unknown as Record<string, unknown>).__wbFrames = counts;
    const Native = window.WebSocket;
    class Counting extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        if (!String(url).includes("/ws/events")) return;
        this.addEventListener("message", (event) => {
          counts.events += 1;
          try {
            const parsed: unknown = JSON.parse((event as MessageEvent<string>).data);
            const type = (parsed as { type?: string }).type;
            if (type === "session_activity") counts.activity += 1;
          } catch {
            // Not a frame we are counting; the totals still see it.
          }
        });
      }
    }
    window.WebSocket = Counting as unknown as typeof WebSocket;
  });
}

const readCounts = (page: Page): Promise<FrameCounts> =>
  page.evaluate(() => (window as unknown as { __wbFrames: FrameCounts }).__wbFrames);

test("a Grep-heavy turn costs the shared socket a handful of frames", async ({ page }, info) => {
  await installTelemetry(page); // `sampleFrames` reads the hooks it installs
  await countEventFrames(page);
  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: "flat", exact: true })).toBeVisible();

  // Open the panel through the registry, exactly as a user would.
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">live agent activity");
  await quickbar.locator(".wb-qb-row", { hasText: "Show live agent activity" }).first().click();
  await expect(page.locator(".wb-activity")).toBeVisible();

  await page.getByRole("button", { name: "New session", exact: true }).click();
  const input = page.locator(".wb-chat-input textarea");
  await expect(input).toBeVisible();
  await expect(page.locator(".wb-activity-session")).toHaveCount(1);

  const before = await readCounts(page);

  const frames = await sampleFrames(page, async () => {
    await input.fill("tool storm please");
    await input.press("Enter");
    // The storm has landed when the last call is on the row and settled.
    await expect(page.locator(".wb-activity-session").first()).toContainText("dropped");
    await expect(page.locator(".wb-activity-then")).toContainText("Grep:");
  });

  const after = await readCounts(page);
  const activityFrames = after.activity - before.activity;

  await record(
    info,
    "activity-burst",
    `${String(CHANGES)} changes (${String(STORM_TOOL_CALLS)} tool calls) arrived as ` +
      `${String(activityFrames)} session_activity frames ` +
      `(${String(after.events - before.events)} /ws/events frames in all); ` +
      `p95 frame ${String(frames.p95Ms)} ms, longest ${String(frames.longestMs)} ms, ` +
      `${String(frames.over50ms)}/${String(frames.frames)} frames over 50 ms`,
    { fixture: FIXTURE, changes: CHANGES, activityFrames, counts: after, frames },
  );

  expect(activityFrames, "the burst is being coalesced, not fanned out one frame per call").toBeLessThanOrEqual(
    MAX_FRAMES,
  );
  // It did arrive: a budget satisfied by nothing happening is not a budget.
  expect(activityFrames).toBeGreaterThan(0);
  expect(frames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
});
