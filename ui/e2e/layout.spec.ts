/**
 * Journey 9 — the layout system: focus mode, named layouts, and a window that
 * remembers.
 *
 * The point of this journey is the **reload**. Everything the layout system
 * claims can be faked in memory; only a browser that goes away and comes back
 * proves the arrangement is on disk and vetted on the way in. So each half ends
 * with a reload and asserts what the window came back as.
 *
 * Asserts:
 *  - `Alt+M` fills the window with the focused panel — a panel that is *not* the
 *    editor, because "the focused one" has to mean it — and the second press
 *    puts every panel back at the size it had;
 *  - switching to a preset built from registered tools really changes the
 *    arrangement (Review has no terminal), and the change survives a reload;
 *  - saving under a name, switching to it and deleting it, from the QuickBar's
 *    "Layouts" section and from the status chip's menu;
 *  - a `shortcuts.md` `layout` entry moves the panels and does nothing else;
 *  - a saved layout naming a panel that no longer exists loses **that panel**
 *    and nothing else, and says so once;
 *  - a corrupt `layouts.json` opens the default arrangement with a warning —
 *    never a blank window.
 *
 * It ends by resetting to the default arrangement, because the journeys after
 * it share this workspace and expect the window they have always had.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { gotoApp, openApp, treeItem, workspaceReady } from "./app";
import { LAYOUT_SHORTCUT_CHORD, LAYOUT_SHORTCUT_NAME, writeWorkspaceFile } from "./workspace";

const SAVED_LAYOUT = "Bidding desk";
/** A panel id nothing is registered under — what a tool removed after a save
 * (or renamed, or gated off by its `when`) leaves behind in the file. */
const GHOST_PANEL = "missioncontrol";

const DEFAULT_PANELS = ["Agent", "Editor", "Files", "Terminal"];
const REVIEW_PANELS = ["Agent", "Editor", "Files"];
const AGENTS_PANELS = ["Agent", "Files", "Terminal"];

/** Panel tab titles on screen, sorted — an arrangement's identity, without
 * depending on which group dockview happens to render first. */
async function panels(page: Page): Promise<string[]> {
  const titles = await page.locator(".wb-panel-tab").allTextContents();
  return titles.map((title) => title.replace("×", "").trim()).sort();
}

/** Rendered size of each panel group, by tab title. Real geometry read from the
 * live DOM: a "maximized" panel that is not actually filling the window is
 * exactly the failure this journey exists to catch. */
async function groupSizes(page: Page): Promise<Record<string, [number, number]>> {
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

/** Run a QuickBar command by its row title. The whole layout system is
 * reachable this way, which is what registering it bought. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/** One warning toast, by what it is about. The seeded `shortcuts.md` raises its
 * own on every load, so "the warn toast" is never a single element here. */
const warnToast = (page: Page, about: string): Locator =>
  page.locator(".wb-toast.is-warn").filter({ hasText: about });

const layouts = async (page: Page): Promise<LayoutsResponse> =>
  (await page.request.get("/api/layouts")).json() as Promise<LayoutsResponse>;

/** Wait until the debounced autosave has written what is on screen. */
async function persisted(page: Page, name: string | null): Promise<void> {
  await expect
    .poll(async () => (await layouts(page)).state.current_name, { timeout: 10_000 })
    .toBe(name);
}

/**
 * Do something to the layouts file with no app running.
 *
 * Not fussiness: a live window autosaves, so a page left open while the file is
 * edited from outside would race to overwrite the very thing under test. The
 * user hitting this bug had quit, or never had that panel to begin with.
 */
async function withAppClosed(page: Page, edit: () => Promise<void> | void): Promise<void> {
  await page.goto("about:blank");
  await edit();
  await gotoApp(page);
  await workspaceReady(page);
}

/**
 * Leave the workspace with the arrangement every other journey expects.
 *
 * This is the only journey that persists window state, and it runs before six
 * others that share the workspace — so a *failed* step here must not hand them
 * a window with no editor in it. The last test step asserts the reset happens
 * through the UI; this makes sure it happens either way.
 */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", {
    data: { current: null, current_name: null, saved: [] },
  });
  await context.dispose();
});

test("focus mode, named layouts, and an arrangement that survives a reload", async ({ page }) => {
  await openApp(page);
  expect(await panels(page)).toEqual(DEFAULT_PANELS);

  await test.step("Alt+M fills the window with the focused panel, and gives it back", async () => {
    const before = await groupSizes(page);
    // The Files panel, not the editor: focus mode has to follow the focus, so
    // the panel under test is one the app does not activate for you.
    await treeItem(page, "src").click();
    await page.keyboard.press("Alt+M");
    await expect(page.locator(".wb-layout-chip")).toHaveText("Focused");

    const maximized = await groupSizes(page);
    expect(maximized.Files?.[0] ?? 0).toBeGreaterThan(before.Files?.[0] ?? 0);
    for (const other of ["Editor", "Agent", "Terminal"]) {
      const [width, height] = maximized[other] ?? [0, 0];
      expect(width === 0 || height === 0, `${other} still has the window`).toBe(true);
    }

    await page.keyboard.press("Alt+M");
    await expect(page.locator(".wb-layout-chip")).not.toHaveText("Focused");
    // "Restore the previous arrangement exactly" — every panel, same size.
    expect(await groupSizes(page)).toEqual(before);
  });

  await test.step("a preset built from registered tools changes the arrangement", async () => {
    await runCommand(page, "Switch to the Review layout");
    expect(await panels(page)).toEqual(REVIEW_PANELS);
    await expect(page.locator(".wb-layout-chip")).toHaveText("Review");
  });

  await test.step("saving it under a name puts it in the QuickBar and on disk", async () => {
    await page.locator(".wb-layout-chip").click();
    const menu = page.getByRole("dialog", { name: "Layouts" });
    await menu.getByRole("textbox", { name: "Name for this arrangement" }).fill(SAVED_LAYOUT);
    await menu.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.locator(".wb-layout-chip")).toHaveText(SAVED_LAYOUT);
    await persisted(page, SAVED_LAYOUT);
    expect((await layouts(page)).state.saved.map((layout) => layout.name)).toEqual([SAVED_LAYOUT]);
  });

  await test.step("and the arrangement is still there after a reload", async () => {
    // The whole point: not "the store says Review", but "the window came back
    // without a terminal, because the file on disk said so".
    await page.reload();
    await workspaceReady(page);
    expect(await panels(page)).toEqual(REVIEW_PANELS);
    await expect(page.locator(".wb-layout-chip")).toHaveText(SAVED_LAYOUT);
  });

  await test.step("a shortcuts.md layout entry moves the panels", async () => {
    // The one entry kind that acts rather than inserts. It can do exactly this.
    await page.keyboard.press(LAYOUT_SHORTCUT_CHORD);
    await expect(page.locator(".wb-layout-chip")).toHaveText("Agents");
    expect(await panels(page)).toEqual(AGENTS_PANELS);
    // …and it is a QuickBar row like any other shortcut, showing the layout it
    // switches to rather than whatever the file chose to call it.
    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    const row = quickbar
      .locator(".wb-qb-row")
      .filter({ has: page.getByText(LAYOUT_SHORTCUT_NAME, { exact: true }) });
    await expect(row).toContainText("layout · Agents");
    await page.keyboard.press("Escape");
    await expect(quickbar).toBeHidden();
  });

  await test.step("switching back to the saved layout brings its panels back", async () => {
    await runCommand(page, `Switch to the ${SAVED_LAYOUT} layout`);
    expect(await panels(page)).toEqual(REVIEW_PANELS);
    await persisted(page, SAVED_LAYOUT);
  });
});

test("a saved layout that names a panel Workbench no longer has", async ({ page }) => {
  await openApp(page);
  // Self-contained: start from a known arrangement rather than from whatever
  // the previous test left, so a failure there reads as one failure.
  await runCommand(page, "Switch to the Review layout");
  await persisted(page, "Review");

  await test.step("the stale entry is dropped and the rest of the layout kept", async () => {
    const current = (await layouts(page)).state.current as {
      grid: { root: { data: unknown[] } };
      panels: Record<string, unknown>;
    };
    current.panels[GHOST_PANEL] = {
      id: GHOST_PANEL,
      contentComponent: GHOST_PANEL,
      title: "Mission Control",
    };
    current.grid.root.data.push({
      type: "leaf",
      size: 200,
      data: { id: "ghost", views: [GHOST_PANEL], activeView: GHOST_PANEL },
    });

    await withAppClosed(page, async () => {
      // dockview would restore this panel with no component behind it and take
      // the window down on render — which is why a layout is vetted before it
      // ever reaches `fromJSON`.
      const response = await page.request.put("/api/layouts", {
        data: { current, current_name: null, saved: [] },
      });
      expect(response.ok()).toBe(true);
    });

    expect(await panels(page)).toEqual(REVIEW_PANELS);
    // Filtered, not `.wb-toast.is-warn` alone: the seeded shortcuts file raises
    // its own warning on every load, and this journey is about a different one.
    await expect(warnToast(page, "Restored your layout")).toContainText(GHOST_PANEL);
  });

  await test.step("a corrupt layouts.json opens the default window, not a blank one", async () => {
    await withAppClosed(page, () => {
      writeWorkspaceFile(".workbench/layouts.json", "{ this is not a layout");
    });
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await expect(warnToast(page, "layouts.json")).toContainText("not valid JSON");
  });

  await test.step("reset leaves the workspace as the next journey expects it", async () => {
    await runCommand(page, "Switch to the Default layout");
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await persisted(page, null);
    expect((await layouts(page)).state.saved).toEqual([]);
  });
});
