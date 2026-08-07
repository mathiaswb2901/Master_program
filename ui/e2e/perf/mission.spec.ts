/**
 * Budget — a **four-card Mission Control board** under a burst of agent output.
 *
 * `activity.spec.ts` prices the same firehose against the Activity panel, and
 * it is deliberately not this test. Two things differ, and both are new risk:
 *
 * 1. **Four sources, not one.** The board joins the activity feed, the usage
 *    service, the worktree pool and the app store's session-status map. Any of
 *    those four replacing its snapshot object re-runs `MissionBody` — so the
 *    per-card `memo` boundary is doing four times the work it does on the
 *    Activity panel, and a regression that removed it would be invisible to
 *    that budget.
 * 2. **Four cards, not one.** The claim the whole feature makes is that a
 *    *crew* stays readable. One busy session re-rendering three quiet cards, at
 *    up to four frames a second, is exactly the storm the server's 250 ms
 *    coalescing exists to prevent — arriving one layer later, and getting worse
 *    with precisely the thing the board is for.
 *
 * Four *sessions* rather than four spawned workers, on purpose: a worker needs
 * a borrowed git worktree and the perf fixture is a generated 5,005-file tree,
 * not a repository. What this measures — N cards on one board while the fleet
 * is loud — is the same shape, and the worker path is proven where it can be
 * (`test_orchestrator.py`, `ui/e2e/mission.spec.ts`).
 *
 * **The budget is a count, so it cannot flake**; the frame timings are recorded
 * next to it because they are what makes the count mean something, and the
 * frame ceiling below is a catastrophe guard rather than the gate (the same
 * split `panes.spec.ts` and `activity.spec.ts` make).
 */

import { expect, type Page } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, record, sampleFrames } from "./instrument";
import { test } from "./window";

/** Must match `STORM_TOOL_CALLS` in `services/fake_agent.py`. */
const STORM_TOOL_CALLS = 40;
/** Announce + settle: every one is a change the server could publish. */
const CHANGES = STORM_TOOL_CALLS * 2;

/**
 * Cards on the board while the storm lands. Four is the number the fleet view
 * is judged on throughout this milestone (ROADMAP item 10, `panes.spec.ts`),
 * and it is what `WORKBENCH_MAX_CONCURRENT_SESSIONS` allows by default —
 * that cap counts sessions *working*, and three of these are idle.
 */
const CARDS = 4;

/**
 * The ceiling is a quarter of the changes rather than a measured number: what
 * it excludes is the *design* where every tool call is a frame, not a run that
 * coalesced one pair fewer than last time. Same reasoning, same number, as
 * `activity.spec.ts` — stated here rather than imported, because a shared
 * constant would let one budget be loosened for the other's reasons.
 */
const MAX_FRAMES = CHANGES / 4;

/** Catastrophe guard, not the budget. */
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
            if ((parsed as { type?: string }).type === "session_activity") counts.activity += 1;
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

test("a four-card board rides out a Grep-heavy turn", async ({ page }, info) => {
  await installTelemetry(page); // `sampleFrames` reads the hooks it installs
  await countEventFrames(page);
  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: "flat", exact: true })).toBeVisible();

  // Open the board through the registry, exactly as a user would.
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">mission control");
  await quickbar.locator(".wb-qb-row", { hasText: "Show Mission Control" }).first().click();
  await expect(page.locator(".wb-mission")).toBeVisible();

  const cards = page.locator(".wb-mission .wb-mission-card");
  const input = page.locator(".wb-chat-input textarea");
  // A full perf lane shares one backend, so earlier specs' sessions are already
  // on the board — launch.spec seeds six, and a just-created session is a card
  // the moment it exists (an idle fleet is the common case; services/activity.py
  // notes a session on creation). The budget is four *added* cards, so count
  // them against whatever is already here rather than assume the board opened
  // empty — the same shared-backend reality launch.spec documents. The baseline
  // is the backend's own session count, and the board is held to it first, so it
  // is read from a fully-loaded board rather than a half-arrived one.
  const folders: { sessions: unknown[] }[] = await (
    await page.request.get("/api/agents/sessions")
  ).json();
  const baseline = folders.reduce((total, folder) => total + folder.sessions.length, 0);
  await expect(cards).toHaveCount(baseline);
  for (let index = 0; index < CARDS; index += 1) {
    await page.getByRole("button", { name: "New session", exact: true }).click();
    await expect(input).toBeVisible();
    await expect(cards).toHaveCount(baseline + index + 1);
  }

  const before = await readCounts(page);

  const frames = await sampleFrames(page, async () => {
    // The *focused* session storms; the other three cards are quiet, which is
    // the whole point — an unmemoized board re-renders all four per frame.
    await input.fill("tool storm please");
    await input.press("Enter");
    // The storm has landed when the last call is on the card and settled.
    await expect(page.locator(".wb-mission .wb-mission-then")).toContainText("Grep:");
  });

  const after = await readCounts(page);
  const activityFrames = after.activity - before.activity;

  await record(
    info,
    "mission-burst",
    `${String(CHANGES)} changes reached a ${String(baseline + CARDS)}-card board ` +
      `(${String(CARDS)} focused-crew cards over a ${String(baseline)}-session backend) as ` +
      `${String(activityFrames)} session_activity frames ` +
      `(${String(after.events - before.events)} /ws/events frames in all); ` +
      `p95 frame ${String(frames.p95Ms)} ms, longest ${String(frames.longestMs)} ms, ` +
      `${String(frames.over50ms)}/${String(frames.frames)} frames over 50 ms`,
    {
      fixture: FIXTURE,
      cards: baseline + CARDS,
      added: CARDS,
      changes: CHANGES,
      activityFrames,
      counts: after,
      frames,
    },
  );

  await expect(cards).toHaveCount(baseline + CARDS); // the fleet really was four cards deeper
  expect(
    activityFrames,
    "the burst is being coalesced, not fanned out one frame per call",
  ).toBeLessThanOrEqual(MAX_FRAMES);
  // It did arrive: a budget satisfied by nothing happening is not a budget.
  expect(activityFrames).toBeGreaterThan(0);
  expect(frames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
});
