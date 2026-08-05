/**
 * Budget — the fleet: four agent panes and two terminals in one window.
 *
 * The pane system's cost is not a page load, it is what the window feels like
 * once it is full. Four live agent sockets, two PTYs, a 5,005-file tree and a
 * Monaco instance all compositing at once is the arrangement the owner asked
 * for ("four sessions visible at once in a 2×2"), and the two gestures that
 * have to stay instant inside it are **splitting** — dockview rebuilds the grid
 * and React mounts a whole tool — and **moving between panes**, which reads
 * every pane's box and changes focus.
 *
 * So this builds the fleet, then samples `requestAnimationFrame` intervals
 * across one split with the fleet already up, and across a run of directional
 * moves. The reported numbers are the worst frame in each, which is what a
 * person actually notices; the constants below are catastrophe guards.
 *
 * `@wallclock`: frame timing on a shared CI runner is a report, not a gate.
 */

import { expect, test, type Page } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, readTelemetry, record, round, sampleFrames } from "./instrument";

/**
 * Measured 2026-08-05 on the author's machine, on the 5,005-file fixture, with
 * four agent panes and two terminal panes already on screen:
 *
 * * split (dockview rebuilds the grid, React mounts a chat): see the recorded
 *   line — the number this file exists to publish;
 * * pane navigation (eight directional moves): likewise.
 *
 * Both constants are sized well above those as "the window looked frozen"
 * guards, not as the budget. The budget is the recorded line, and the next lane
 * that touches the dock is expected to compare against it.
 */
const FROZEN_FRAME_MS = 1_000;
/** Building the fleet is six splits, four of which create a server session. */
const FLEET_CEILING_MS = 60_000;

const AGENT_PANES = 4;
const TERMINAL_PANES = 2;

/** Split the focused pane and pick a row — the gesture, without the assertions
 * the journey suite already makes about it. */
async function split(page: Page, chord: string, label: string, row: string): Promise<void> {
  await page.keyboard.press(chord);
  const dialog = page.getByRole("dialog", { name: label });
  await expect(dialog).toBeVisible();
  await dialog.locator(".wb-qb-row", { hasText: row }).first().click();
  await expect(dialog).toBeHidden();
}

test("a window full of agents and terminals keeps its frames", { tag: "@wallclock" }, async ({
  page,
}, info) => {
  await installTelemetry(page);
  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: "flat", exact: true })).toBeVisible();

  const startedAt = Date.now();
  for (let i = 0; i < TERMINAL_PANES; i++) {
    await split(page, "Alt+Shift+S", "Split this pane downwards", "New terminal");
  }
  // One fewer than the target: the last one is the split being measured.
  for (let i = 0; i < AGENT_PANES - 1; i++) {
    await split(page, "Alt+S", "Split this pane to the right", "New agent session");
    await expect(page.locator(".wb-chat")).toHaveCount(i + 2);
  }
  const fleetMs = Date.now() - startedAt;

  // The measured split: the window is already carrying three agent sockets, two
  // PTYs and the tree when this one lands.
  const splitFrames = await sampleFrames(page, async () => {
    await split(page, "Alt+S", "Split this pane to the right", "New agent session");
    await expect(page.locator(".wb-chat")).toHaveCount(AGENT_PANES + 1);
  });

  const panes = await page.locator(".dv-groupview").count();
  const navFrames = await sampleFrames(page, async () => {
    for (const chord of [
      "Alt+ArrowLeft",
      "Alt+ArrowUp",
      "Alt+ArrowRight",
      "Alt+ArrowDown",
      "Alt+O",
      "Alt+O",
      "Alt+O",
      "Alt+O",
    ]) {
      await page.keyboard.press(chord);
      // Paced like a person moving through a window, not fired in one task.
      await page.waitForTimeout(16);
    }
  });

  const telemetry = await readTelemetry(page);
  const slowestEvent = telemetry.events.reduce((worst, e) => (e.duration > worst ? e.duration : worst), 0);

  await record(
    info,
    "pane-fleet-interaction",
    `${String(panes)} panes (${String(AGENT_PANES)} agents, ${String(TERMINAL_PANES)} terminals); ` +
      `fleet built in ${String(fleetMs)} ms; ` +
      `split p95 frame ${String(splitFrames.p95Ms)} ms, longest ${String(splitFrames.longestMs)} ms; ` +
      `navigation p95 ${String(navFrames.p95Ms)} ms, longest ${String(navFrames.longestMs)} ms, ` +
      `${String(navFrames.over50ms)}/${String(navFrames.frames)} frames over 50 ms; ` +
      `slowest input event ${String(round(slowestEvent))} ms`,
    { fixture: FIXTURE, panes, fleetMs, splitFrames, navFrames, telemetry },
  );

  expect(fleetMs).toBeLessThan(FLEET_CEILING_MS);
  expect(splitFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
  expect(navFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
});
