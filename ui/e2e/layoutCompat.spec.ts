/**
 * The saved-layout contract, across a dockview major upgrade.
 *
 * `.workbench/layouts.json` is written by dockview's own serializer and read
 * back by its deserializer, so a dockview upgrade is the one change that can
 * silently take a user's window away — not by crashing, but by opening the
 * default arrangement and then *overwriting* the file that described the real
 * one (persistence arms as soon as a restore settles). Nothing in the suite
 * covered that: every other layout journey saves and restores inside a single
 * version, which is exactly the case that cannot fail.
 *
 * So the fixtures in `e2e/fixtures/` are **not** written by this suite. They
 * were captured from the running app on master at `45edcdc`, with
 * **dockview 4.13.1**, before the upgrade to 7 — three real `GET /api/layouts`
 * bodies covering the three shapes the serializer can produce:
 *
 *  - `layout-v4-grid.json` — a nested grid (branch inside branch), five panels
 *    including a plural instance pane (`terminal#2`) and an emptied, invisible
 *    leaf, which is what dockview leaves behind after a pane moves;
 *  - `layout-v4-maximized.json` — the same arrangement in focus mode, so
 *    `grid.maximizedNode` (a *path* into the tree, and the one key our own
 *    `pruneLayout` refuses to carry over a structural change) is exercised;
 *  - `layout-v4-popout.json` — a pane popped out to its own window, in the
 *    single-group `popoutGroups[].data` form v4 wrote.
 *
 * They are frozen input. Regenerating them from a newer dockview would delete
 * the only thing they test, so if one ever needs to change the change belongs
 * in the app, not in the fixture.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, request, test, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { dockSettled, gotoApp } from "./app";

const FIXTURES = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "fixtures");

/** One frozen `state.current` body, as dockview 4.13.1 wrote it. */
function fixture(name: string): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf-8")) as Record<
    string,
    unknown
  >;
}

/** Panel tab titles on screen, sorted — an arrangement's identity. */
async function panels(page: Page): Promise<string[]> {
  const titles = await page.locator(".wb-panel-tab").allTextContents();
  return titles.map((title) => title.replace("×", "").trim()).sort();
}

/** Rendered size of each pane, by tab title (see `layout.spec.ts`). */
async function groupSizes(page: Page): Promise<Record<string, [number, number]>> {
  await dockSettled(page);
  return page.evaluate(() => {
    const sizes: Record<string, [number, number]> = {};
    for (const group of document.querySelectorAll(".dv-groupview")) {
      const title = group.querySelector(".wb-panel-tab")?.textContent ?? "";
      const box = group.getBoundingClientRect();
      sizes[title.replace("×", "").trim()] = [Math.round(box.width), Math.round(box.height)];
    }
    return sizes;
  });
}

/**
 * Install a layouts document with no app running, then open the app on it and
 * wait until the restore has settled.
 *
 * Written with no app running because a live window autosaves, so editing the
 * file under one would race the very thing being tested (`layout.spec.ts` has
 * the same helper and the same reason).
 *
 * The readiness signal is the arrangement itself, not the file tree: one of
 * these fixtures was saved in focus mode, where the Files pane is 0×0 and
 * `workspaceReady` would wait 15 s for something the layout is correct to keep
 * hidden. The dock paints the *default* arrangement first and the restore
 * lands on top of it, so waiting for tabs the default arrangement does not
 * have is what separates "restored" from "has not restored yet". Each test
 * asserts the *exact* set itself, once this has settled.
 */
async function openOn(page: Page, current: unknown, waitFor: string[]): Promise<void> {
  await page.goto("about:blank");
  const response = await page.request.put("/api/layouts", {
    data: { current, current_name: null, saved: [] },
  });
  expect(response.ok()).toBe(true);
  await gotoApp(page);
  await expect
    .poll(
      async () => {
        const seen = await panels(page);
        return waitFor.every((title) => seen.includes(title));
      },
      { timeout: 20_000 },
    )
    .toBe(true);
}

/** What the app wrote back after adopting the layout. The half that matters:
 * an arrangement that renders once but re-serializes as something else has
 * still lost the user's window, one reload later. */
async function persistedPanelIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

/** No toast claiming the restore failed. Filtered, because the seeded
 * `shortcuts.md` raises its own warning on every load. */
async function noRestoreFailure(page: Page): Promise<void> {
  await expect(
    page.locator(".wb-toast").filter({ hasText: "could not be restored" }),
  ).toHaveCount(0);
  await expect(
    page.locator(".wb-toast").filter({ hasText: "no longer describes any panel" }),
  ).toHaveCount(0);
  await expect(page.locator(".wb-toast").filter({ hasText: "Restored your layout" })).toHaveCount(0);
}

test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("a nested grid serialized by dockview 4.13 opens panel-for-panel", async ({ page }) => {
  const grid = ["Agent", "Editor", "Files", "Terminal", "Terminal 2"];
  await openOn(page, fixture("layout-v4-grid"), ["Terminal 2"]);

  await test.step("every pane the file named is on screen, and nothing else", async () => {
    expect(await panels(page)).toEqual(grid);
    await noRestoreFailure(page);
  });

  await test.step("the geometry the file described is the geometry on screen", async () => {
    // Not just "the panels exist": the grid *tree* is what a saved layout is
    // for. The fixture's root is `[files 240 | branch 660 | agent 380]` with
    // the middle branch stacking editor / terminal#2 / terminal at 232 each,
    // so reading those five numbers back off the live DOM is the assertion
    // that both levels of the tree and their sizes survived deserialization.
    const sizes = await groupSizes(page);
    expect(sizes.Files?.[0]).toBeCloseTo(240, -1);
    expect(sizes.Agent?.[0]).toBeCloseTo(380, -1);
    for (const stacked of ["Editor", "Terminal", "Terminal 2"]) {
      expect(sizes[stacked]?.[0], `${stacked} width`).toBeCloseTo(660, -1);
      expect(sizes[stacked]?.[1], `${stacked} height`).toBeCloseTo(232, -1);
    }
  });

  await test.step("and it re-serializes as the same set of panes", async () => {
    // Persistence arms once a restore settles, so this file is what the *next*
    // launch trusts. A restore that renders correctly and writes back a
    // different pane set has still lost the window, one reload later.
    await expect
      .poll(() => persistedPanelIds(page), { timeout: 10_000 })
      .toEqual(["agent", "editors", "files", "terminal", "terminal#2"]);
  });
});

test("a 4.13 layout saved in focus mode comes back in focus mode", async ({ page }) => {
  await openOn(page, fixture("layout-v4-maximized"), ["Scratchpad", "Terminal 2"]);

  await noRestoreFailure(page);
  await expect(page.locator(".wb-layout-chip")).toHaveText("Focused");
  // `maximizedNode` is a *path* (`{"location":[1,1,1]}`), not a panel id, so
  // "restored" has to mean the right panel — the fixture was maximized on the
  // Scratchpad, which is the third level down. A deserializer that walked the
  // tree differently would fill the window with a plausible wrong pane.
  const sizes = await groupSizes(page);
  expect(sizes.Scratchpad?.[0] ?? 0).toBeGreaterThan(1000);
  for (const other of ["Files", "Agent", "Terminal", "Editor", "Terminal 2"]) {
    const [width, height] = sizes[other] ?? [0, 0];
    expect(width === 0 || height === 0, `${other} still has the window`).toBe(true);
  }
});

test("a 4.13 layout with a popped-out pane loses no pane", async ({ page }) => {
  const popups: Page[] = [];
  page.on("popup", (popup) => popups.push(popup));

  await openOn(page, fixture("layout-v4-popout"), ["Scratchpad", "Terminal 2"]);
  await noRestoreFailure(page);

  await test.step("the grid half is exactly what the file described", async () => {
    // Five of the six panes were in the grid; `terminal` was in its own window.
    await expect
      .poll(async () => (await panels(page)).filter((title) => title !== "Terminal"))
      .toEqual(["Agent", "Editor", "Files", "Scratchpad", "Terminal 2"]);
  });

  await test.step("and the popped-out pane is somewhere, never nowhere", async () => {
    // A restore has no user gesture behind it, so the browser may refuse the
    // window — dockview then re-grids the pane and the app says so once. Either
    // outcome is correct; *losing* the pane is not, and that is the assertion.
    // The union across the main window and any popout is what the user has.
    await expect
      .poll(
        async () => {
          const seen = await panels(page);
          for (const popup of popups) {
            if (!popup.isClosed()) seen.push(...(await panels(popup)));
          }
          return seen.includes("Terminal");
        },
        { timeout: 15_000 },
      )
      .toBe(true);
  });

  await test.step("and the pane ids survive the round trip to disk", async () => {
    await expect
      .poll(() => persistedPanelIds(page), { timeout: 10_000 })
      .toEqual(["agent", "editors", "files", "scratchpad", "terminal", "terminal#2"]);
  });

  for (const popup of popups) if (!popup.isClosed()) await popup.close();
});
