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
 *    never a blank window;
 *  - a saved layout dockview *cannot* deserialize falls back to the default and
 *    leaves neither the chip nor the file naming the layout it came from;
 *  - two switches in a row survive a network that answers them out of order.
 *
 * It ends by resetting to the default arrangement, because the journeys after
 * it share this workspace and expect the window they have always had.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { dockSettled, gotoApp, openApp, treeItem, workspaceReady } from "./app";
import { LAYOUT_SHORTCUT_CHORD, LAYOUT_SHORTCUT_NAME, writeWorkspaceFile } from "./workspace";

const SAVED_LAYOUT = "Bidding desk";
/** A panel id nothing is registered under — what a tool removed after a save
 * (or renamed, or gated off by its `when`) leaves behind in the file. */
const GHOST_PANEL = "missioncontrol";
const BROKEN_LAYOUT = "Broken grid";

/**
 * A saved layout that passes vetting and still breaks dockview.
 *
 * `pruneLayout` vets **panel ids** — it does not, and cannot cheaply, vet
 * dockview's grid algebra. dockview's own deserializer requires the grid root
 * to be a branch and calls `clear()` *before* it checks, so this layout — whose
 * one panel (`editors`) is perfectly registered — empties the window and then
 * throws. That is the second, quieter failure mode of a restore: not "leave the
 * window alone", but "the window is now the default arrangement", and the two
 * cannot be reported to the caller as the same thing.
 */
const LEAF_ROOT_LAYOUT = {
  grid: {
    root: { type: "leaf", size: 1000, data: { id: "1", views: ["editors"], activeView: "editors" } },
    width: 1000,
    height: 800,
    orientation: "HORIZONTAL",
  },
  panels: { editors: { id: "editors", contentComponent: "editors", title: "Editor" } },
  activeGroup: "1",
};

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
 * exactly the failure this journey exists to catch.
 *
 * `dockSettled` first, because focus mode animates the dock on a transform and
 * a transform is part of `getBoundingClientRect()` — measured mid-flight, every
 * panel reads ~1.5 % small and "restored exactly" is false by four pixels. */
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

/**
 * A failed switch must not leave the chip — or the file — naming the layout it
 * failed to reach.
 *
 * The regression: `applySerialized` has two failure modes and used to report
 * both as `false`. When dockview's own deserializer throws, the fallback
 * rebuilds the **default** arrangement — but the switch went on setting nothing
 * and persisting anyway, so the window showed Default while the chip and
 * `layouts.json` still said the layout the user came from. Reloading read that
 * back as truth, so the mislabelling outlived the session that caused it.
 */
test("a saved layout dockview cannot deserialize leaves nothing lying about it", async ({
  page,
}) => {
  await openApp(page);

  await test.step("a layout that survives pruning and still throws", async () => {
    await withAppClosed(page, async () => {
      const response = await page.request.put("/api/layouts", {
        data: {
          current: null,
          current_name: null,
          saved: [{ name: BROKEN_LAYOUT, state: LEAF_ROOT_LAYOUT }],
        },
      });
      expect(response.ok()).toBe(true);
    });
    // Somewhere named, so "the name it came from" is a thing that can survive.
    await runCommand(page, "Switch to the Review layout");
    await expect(page.locator(".wb-layout-chip")).toHaveText("Review");
    await persisted(page, "Review");
  });

  await test.step("the window falls back to the default, and says so once", async () => {
    await runCommand(page, `Switch to the ${BROKEN_LAYOUT} layout`);
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await expect(
      page.locator(".wb-toast.is-error").filter({ hasText: "could not be restored" }),
    ).toContainText(BROKEN_LAYOUT);
  });

  await test.step("and neither the chip nor the file still says Review", async () => {
    // The default arrangement is nobody's named layout, so the chip is unnamed.
    await expect(page.locator(".wb-layout-chip")).toHaveText("Layout");
    // The half that survives a reload — and the half that made this worth
    // fixing rather than tolerating.
    await persisted(page, null);
    await page.reload();
    await workspaceReady(page);
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await expect(page.locator(".wb-layout-chip")).toHaveText("Layout");
  });
});

/**
 * Two switches in a row, and a network that delivers them out of order.
 *
 * The regression: every layout action wrote immediately and independently — no
 * in-flight tracking, no sequence number, and `request()` is a bare `fetch`.
 * `PUT /api/layouts` replaces the whole document and the server `os.replace()`s
 * whatever arrives, so two bodies in flight at once persist in delivery order
 * rather than in the order the user acted. Switch to Review, switch to Agents,
 * let the Review body arrive last, and the window and the chip say Agents while
 * the file the next reload trusts says Review — the user's real choice
 * discarded silently, inside a single window and a single session.
 *
 * **Why the delay and not a parked request.** Every switch also schedules the
 * debounced autosave, so a stale write released *early* is followed ~500 ms
 * later by a correct one that quietly repairs the file — the first version of
 * this test held the first PUT and passed against the unfixed app for exactly
 * that reason. Holding the Review body for longer than the debounce is what
 * makes "it arrived last" a fact of the run instead of a race with a timer.
 *
 * The fix is that there is never a second body in flight to be reordered with:
 * writes go through one chain, and a queued write reads the arrangement as it
 * is when it is finally sent.
 */
const REORDER_DELAY_MS = 2_000;

test("two layout switches in a row cannot land on disk out of order", async ({ page }) => {
  await openApp(page);
  await runCommand(page, "Switch to the Default layout");
  await persisted(page, null);

  /** The layout name a `PUT` body carries — how a write is identified here. */
  const nameIn = (body: string | null): string =>
    /"current_name":\s*("[^"]*"|null)/.exec(body ?? "")?.[1] ?? "?";
  const isLayoutPut = (request: { method: () => string; url: () => string }): boolean =>
    request.method() === "PUT" && request.url().includes("/api/layouts");

  await page.route("**/api/layouts", async (route) => {
    const request = route.request();
    // The reordering, made concrete: the body that carries Review is held on
    // the wire until every write the second switch produces has been answered.
    if (isLayoutPut(request) && nameIn(request.postData()) === '"Review"') {
      await page.waitForTimeout(REORDER_DELAY_MS);
    }
    await route.continue();
  });

  // Armed before the switches: the assertion below is only meaningful once the
  // stale body has actually landed, and a waiter registered afterwards could
  // miss it.
  const stale = page.waitForResponse(
    (response) =>
      isLayoutPut(response.request()) && nameIn(response.request().postData()) === '"Review"',
  );

  await test.step("switch twice while the first write is still on the wire", async () => {
    await runCommand(page, "Switch to the Review layout");
    await expect(page.locator(".wb-layout-chip")).toHaveText("Review");
    await runCommand(page, "Switch to the Agents layout");
    await expect(page.locator(".wb-layout-chip")).toHaveText("Agents");
    expect(await panels(page)).toEqual(AGENTS_PANELS);
  });

  await test.step("the late write cannot bury the choice the window is showing", async () => {
    // The stale body has landed — every Agents write went to the server before
    // it, so at this instant the unfixed app's file says Review and has nothing
    // left to write that would change it. Asserting any earlier would be free
    // to sample the file in the gap before it lands and pass on a bug it had
    // simply not seen yet.
    expect((await stale).ok()).toBe(true);
    // Fixed, the write that was queued behind the stale one now goes out
    // carrying the arrangement as it is *now*. Unfixed, this never arrives.
    await persisted(page, "Agents");
    expect(await panels(page)).toEqual(AGENTS_PANELS);
  });

  await page.unroute("**/api/layouts");
  await runCommand(page, "Switch to the Default layout");
  await persisted(page, null);
});
