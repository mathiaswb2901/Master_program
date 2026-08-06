/**
 * The window every perf spec starts in.
 *
 * The lane is one worker against one workspace, and the app persists the live
 * arrangement into that workspace — `<fixture>/.workbench/layouts.json`, on a
 * 500 ms debounce. So without this module a spec's starting window is whatever
 * the spec before it left behind, and a budget stops being a budget.
 *
 * That is not a theoretical hazard; it is what the lane was doing. Observed
 * 2026-08-06 on a pinned `WB_PERF_WORKSPACE` with `.workbench/` wiped first:
 * `panes.spec.ts` builds a 13-pane fleet on purpose (4 agents, 2 terminals, 4
 * Monaco instances) and sorts immediately before `watcher.spec.ts`, so the
 * watcher budget's `page.goto("/")` restored the fleet, the file tree came back
 * a fraction of its former height, and the row it waits for was below the fold
 * of a virtualiser with no reason to mount it — 30 s of waiting, in a spec that
 * passes in 4.5 s when it runs alone. With the reset in place the same spec, in
 * the same full lane, on the same pinned fixture: **3.2 s, 20 changes, 0
 * listings**.
 *
 * So every test that opens a page starts from the document a workspace has
 * before anyone has arranged anything. Three decisions worth stating:
 *
 *  * **Bound to the `page` fixture, not a `beforeEach` per spec.** A reset a new
 *    spec can forget is the same defect one commit later; this one arrives with
 *    the page. It also costs nothing for the two budgets that never open a page
 *    (`bundle.spec.ts`, and `motion.spec.ts`'s stylesheet half), because a
 *    fixture nobody requests is never set up. `eslint.config.js` makes taking
 *    Playwright's own `test` in a perf spec an error, so the import is the
 *    enforcement rather than a convention.
 *  * **Through the API, not the file.** `PUT /api/layouts` is what the app
 *    itself writes with and what the journey suite already resets with
 *    (`ui/e2e/panes.spec.ts`), so the service stays the only writer of a file it
 *    replaces atomically. Reaching around it to `fs.rm` the JSON would be a
 *    second writer racing the first for no gain.
 *  * **It may not perturb the launch budget**, whose numbers are quoted in
 *    `launch.spec.ts`'s own docstring. One request on an API context, before the
 *    page has navigated: the timeline `installTelemetry` reads begins at `goto`,
 *    and nothing here touches the browser context, its cache or its clock.
 *    Measured rather than argued, full lane before and after — tree rows
 *    **714.9 → 686.6 ms**, FCP **700 → 672 ms**, DCL **68.4 → 64.1 ms**, all
 *    inside this machine's noise. The one thing it costs the tree is a
 *    `.workbench` row that a never-arranged workspace would not have had, which
 *    is one row of seven and no extra directory listing.
 *
 * What this does **not** isolate is state the backend holds rather than the
 * workspace: an agent session or a PTY outlives the page that made it, and the
 * run shares one backend. `launch.spec.ts`'s layout-shift budget still reads a
 * session `activity.spec.ts` created — the measurement, and why the answer is a
 * product fix rather than a reset here, are in that spec's `LAYOUT_SHIFT_CEILING`.
 */

import { test as base, expect } from "@playwright/test";

import type { LayoutsState } from "../../src/types";

/**
 * What a workspace holds before anyone has arranged anything: no live
 * arrangement, no name for one, nothing saved.
 *
 * Annotated with the app's own type, the way the journey suite does it, so the
 * body is written against `LayoutsState` (mirrored from
 * `server/src/workbench_server/models/layouts.py`) rather than guessed at. It is
 * documentation an editor enforces and the build does not: `e2e/` is outside the
 * `tsc -b` program by design, so a field that changes shape is caught here by
 * reading, and in the lane by a 422 — which is why the PUT below is checked.
 */
export const NO_ARRANGEMENT: LayoutsState = { current: null, current_name: null, saved: [] };

/**
 * The lane's `test`. Same Playwright `test` in every other respect —
 * `test.use`, `test.describe`, tags and the `info` argument all behave — with
 * the starting window pinned for anything that asks for a page.
 */
export const test = base.extend({
  page: async ({ page, request }, use) => {
    const response = await request.put("/api/layouts", { data: NO_ARRANGEMENT });
    // A reset that silently failed would put the spec back in the previous
    // one's window and cost someone the afternoon this cost.
    expect(
      response.ok(),
      `could not reset the saved arrangement: ${String(response.status())} ${response.statusText()}`,
    ).toBeTruthy();
    await use(page);
  },
});
