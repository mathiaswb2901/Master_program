/**
 * Budget — the fleet: four agent panes, two terminals and four Monaco editors
 * in one window.
 *
 * The pane system's cost is not a page load, it is what the window feels like
 * once it is full. The arrangement the owner asked for ("four sessions visible
 * at once in a 2×2") plus the oldest reason anyone splits a window — two files
 * side by side — is four live agent sockets, two PTYs, a 5,005-file tree and
 * **four Monaco instances** compositing at once. Monaco is by far the heaviest
 * of the three tools this milestone made plural, so a budget that never mounted
 * one would be measuring the cheap half of the window.
 *
 * The two gestures that have to stay instant inside it are **splitting** —
 * dockview rebuilds the grid and React mounts a whole tool — and **moving
 * between panes**, which reads every pane's box and changes focus. Both are
 * measured with the fleet already up, and the measured split is the expensive
 * one: a Monaco pane onto a 5,000-line file.
 *
 * So this builds the fleet, then samples `requestAnimationFrame` intervals
 * across that split and across a run of directional moves. The reported numbers
 * are the worst frame in each, which is what a person actually notices; the
 * constants below are catastrophe guards.
 *
 * `@wallclock`: frame timing on a shared CI runner is a report, not a gate.
 */

import { expect, type Page } from "@playwright/test";

import { FIXTURE } from "./fixture";
import { installTelemetry, readTelemetry, record, round, sampleFrames } from "./instrument";
import { test } from "./window";

/**
 * Measured 2026-08-05 on the author's machine, on the 5,005-file fixture, at
 * 1920×1080, in a window of **13 panes** (4 agent sessions, 2 terminals, 3 file
 * panes plus the four defaults — the default Editor pane holds a fourth Monaco):
 *
 * * building the fleet — nine splits, four of them a round trip that creates a
 *   server session, plus three files opened: **4,306 ms**;
 * * the measured **split** — a Monaco pane onto the 5,000-line file, with three
 *   editors, four agents and two PTYs already live: p95 frame **33.4 ms**,
 *   longest **50.0 ms** — one frame in which three were due;
 * * **pane navigation**, eight directional moves across the full window: p95
 *   **33.3 ms**, longest **33.3 ms**, **0 of 21** frames over 50 ms — moving
 *   between panes does not care how full the window is;
 * * slowest input event over the whole run: **120 ms** — the Monaco mount.
 *
 * The honest reading: **mounting an editor is the expensive gesture**, and it is
 * the one the previous version of this budget never measured (4 agents + 2
 * terminals and no file open at all: 33.3 ms longest split frame, 64 ms slowest
 * event). Run-to-run the worst split frame sits at 50 ms and the slowest event
 * between 120 and 192 ms — a visible hitch, not a freeze, and the number item
 * 9's lazy acquisition and hibernation have to move.
 *
 * The two constants below are sized well above those as "the window looked
 * frozen" guards, not as the budget. The budget is the recorded line, and the
 * next lane that touches the dock is expected to compare against it.
 */
const FROZEN_FRAME_MS = 1_000;
/** Building the fleet is nine splits, four of which create a server session,
 * and three file opens — one of them a 5,000-line buffer. */
const FLEET_CEILING_MS = 90_000;

const AGENT_PANES = 4;
const TERMINAL_PANES = 2;
/** Files put in panes of their own. The default Editor pane keeps a Monaco on
 * the active file too, so the measured split lands in a window already holding
 * three of them and makes it four. */
const FILE_PANES = ["README.md", "notes.md"] as const;
/** The measured split's file: 5,000 lines, the fixture's heaviest buffer. */
const BIG_FILE = "big_model.py";

/** Split the focused pane and pick a row — the gesture, without the assertions
 * the journey suite already makes about it. */
async function split(page: Page, chord: string, label: string, row: string): Promise<void> {
  await page.keyboard.press(chord);
  const dialog = page.getByRole("dialog", { name: label });
  await expect(dialog).toBeVisible();
  await dialog.locator(".wb-qb-row", { hasText: row }).first().click();
  await expect(dialog).toBeHidden();
}

/** Open a file the way a person does — `Ctrl+P`, type, pick. The editor's pane
 * picker only offers files that are *open*, so this comes first. */
async function openFile(page: Page, name: string): Promise<void> {
  await page.keyboard.press("Control+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await expect(quickbar).toBeVisible();
  await quickbar.getByRole("textbox").fill(name);
  await quickbar.locator(".wb-qb-row", { hasText: name }).first().click();
  await expect(quickbar).toBeHidden();
}

/** Mounted Monaco instances, across every pane that holds one. */
const editors = (page: Page) => page.locator(".wb-editor-body .monaco-editor");

/**
 * A 13-pane window is not something anyone builds on a laptop lid, and dockview
 * has a minimum size per group — at the default 1280×720 the last splits land
 * under it and the gesture measures a refusal instead of a mount. This is also
 * the harder measurement, not the softer one: 2.25× the pixels to composite,
 * with four Monaco instances drawing into them. The other budgets in this lane
 * keep the default viewport; this line is only comparable with itself.
 */
test.use({ viewport: { width: 1920, height: 1080 } });

test("a window full of agents, terminals and editors keeps its frames", { tag: "@wallclock" }, async ({
  page,
}, info) => {
  await installTelemetry(page);
  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: "flat", exact: true })).toBeVisible();

  const startedAt = Date.now();
  for (let i = 0; i < TERMINAL_PANES; i++) {
    await split(page, "Alt+Shift+S", "Split this pane downwards", "New terminal");
  }
  for (let i = 0; i < AGENT_PANES; i++) {
    await split(page, "Alt+S", "Split this pane to the right", "New agent session");
    await expect(page.locator(".wb-chat")).toHaveCount(i + 2);
  }

  // Three files open — two into panes of their own now, the third is what the
  // measured split mounts. One Monaco is live in the default Editor pane from
  // the first of these.
  for (const name of [...FILE_PANES, BIG_FILE]) {
    await openFile(page, name);
  }
  await expect(editors(page)).toHaveCount(1);
  for (const [i, name] of FILE_PANES.entries()) {
    await split(page, "Alt+Shift+S", "Split this pane downwards", name);
    await expect(editors(page)).toHaveCount(i + 2);
  }
  const fleetMs = Date.now() - startedAt;

  // The measured split: the heaviest tool this milestone made plural, onto the
  // heaviest file in the fixture, in a window already carrying three Monaco
  // instances, four agent sockets, two PTYs and the tree.
  const splitFrames = await sampleFrames(page, async () => {
    await split(page, "Alt+S", "Split this pane to the right", BIG_FILE);
    await expect(editors(page)).toHaveCount(FILE_PANES.length + 2);
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
  const monaco = await editors(page).count();

  await record(
    info,
    "pane-fleet-interaction",
    `${String(panes)} panes (${String(AGENT_PANES)} agents, ${String(TERMINAL_PANES)} terminals, ` +
      `${String(monaco)} Monaco instances); fleet built in ${String(fleetMs)} ms; ` +
      `split (Monaco onto ${BIG_FILE}) p95 frame ${String(splitFrames.p95Ms)} ms, ` +
      `longest ${String(splitFrames.longestMs)} ms; ` +
      `navigation p95 ${String(navFrames.p95Ms)} ms, longest ${String(navFrames.longestMs)} ms, ` +
      `${String(navFrames.over50ms)}/${String(navFrames.frames)} frames over 50 ms; ` +
      `slowest input event ${String(round(slowestEvent))} ms`,
    { fixture: FIXTURE, panes, monaco, fleetMs, splitFrames, navFrames, telemetry },
  );

  // The arrangement the milestone was built to support really is on screen —
  // a budget that measured a window missing its editors would pass and mean
  // nothing.
  expect(monaco, "four Monaco instances, which is the risk being priced").toBe(
    FILE_PANES.length + 2,
  );
  expect(fleetMs).toBeLessThan(FLEET_CEILING_MS);
  expect(splitFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
  expect(navFrames.longestMs).toBeLessThan(FROZEN_FRAME_MS);
});
