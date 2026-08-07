/**
 * Journey 13 — pop a pane out to its own window (M5 item 13, browser slice).
 *
 * dockview can move a pane's group into a *separate* browser window and move it
 * back. With panes in place this is an arrangement, and the claims that make it
 * a tool rather than a demo are the ones here:
 *
 *  - a regular pane really leaves the main window (its group is gone from the
 *    main document and present in a new one at `/popout.html`), the popped-out
 *    document is **themed** from the same tokens, and the surfaces that were
 *    re-parented — xterm here, Monaco in the second test — still *work* in the
 *    new document rather than being torn down and rebuilt;
 *  - the pane **id is unchanged** by the trip (`terminal#2` is still
 *    `terminal#2`) and the popout is **persisted**, so `layouts.json` can bring
 *    it back where the user left it;
 *  - the keyboard path pops it back in;
 *  - a pane holding a **native Office window refuses**, with the reason on
 *    screen and no orphaned window — the gating safety decision, because a real
 *    Word docked in a pane has no HWND in a popped-out window's parent chain.
 *
 * The native-window half (following a real Word out) is a separate Tauri/WebView2
 * spike; this suite runs in a browser, where the refusal is the whole of the
 * Office story and everything else is verifiable.
 */

import { expect, request, test, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { openApp, workspaceReady } from "./app";
import { DOCX_FILE, NOTES_FILE, NOTES_MARKER } from "./workspace";

const DEFAULT_PANELS = ["Agent", "Editor", "Files", "Terminal"];

async function panels(page: Page): Promise<string[]> {
  const titles = await page.locator(".wb-panel-tab").allTextContents();
  return titles.map((title) => title.replace("×", "").trim()).sort();
}

/** The persisted layout on disk — the file the next launch trusts. */
async function persistedLayout(page: Page): Promise<{
  panels: string[];
  popoutGroups: { gridReferenceGroup?: string; url?: string }[];
}> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as {
    panels?: Record<string, unknown>;
    popoutGroups?: { gridReferenceGroup?: string; url?: string }[];
  } | null;
  return {
    panels: Object.keys(current?.panels ?? {}).sort(),
    popoutGroups: current?.popoutGroups ?? [],
  };
}

/** A computed design token in a document — how we prove the theme travelled. */
function token(target: Page, name: string): Promise<string> {
  return target.evaluate(
    (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(),
    name,
  );
}

/** Run a QuickBar command by its row title (main window). */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/** Split the focused pane and pick a row by title (reused from journey 10). */
async function split(page: Page, chord: string, label: string, row: string): Promise<void> {
  await page.keyboard.press(chord);
  const dialog = page.getByRole("dialog", { name: label });
  await expect(dialog).toBeVisible();
  await dialog.locator(".wb-qb-row", { hasText: row }).first().click();
  await expect(dialog).toBeHidden();
}

/** Leave the workspace as the other journeys expect it, whatever happened. */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("a terminal pane pops out to its own window, survives, and comes back", async ({ page }) => {
  await openApp(page);
  expect(await panels(page)).toEqual(DEFAULT_PANELS);

  await test.step("split a second terminal, so the popped-out pane has an instance id", async () => {
    await split(page, "Alt+Shift+S", "Split this pane downwards", "New terminal");
    await expect(page.locator(".wb-panel-tab", { hasText: "Terminal 2" })).toBeVisible();
    await expect(page.locator(".wb-terminal")).toHaveCount(2);
    await expect.poll(async () => (await persistedLayout(page)).panels).toContain("terminal#2");
  });

  let popup!: Page;

  await test.step("Alt+P moves that pane into a separate window", async () => {
    // The new terminal pane is the focused one after the split, so Alt+P pops it.
    const popupPromise = page.waitForEvent("popup");
    await page.keyboard.press("Alt+P");
    popup = await popupPromise;
    // dockview re-parents the pane's DOM into the popout on the window's `load`
    // event (it copies the stylesheets there too), so wait for that, not merely
    // for the document to parse.
    await popup.waitForLoadState("load");
    expect(new URL(popup.url()).pathname).toBe("/popout.html");

    // It really left the main window — its tab and its terminal are gone from
    // this document (dockview keeps the emptied source group in the grid but
    // invisible, so the tab, not the group count, is the honest signal)…
    await expect(page.locator(".wb-panel-tab", { hasText: "Terminal 2" })).toHaveCount(0);
    await expect(page.locator(".wb-terminal")).toHaveCount(1);
    // …and arrived in the popout, its tab and its terminal now in that document.
    await expect(popup.locator(".wb-panel-tab", { hasText: "Terminal 2" })).toBeVisible();
    await expect(popup.locator(".wb-terminal")).toHaveCount(1);
  });

  await test.step("the popped-out document is themed from the same tokens", async () => {
    // dockview copies the stylesheets; `popout.html` + Panes.tsx carry the theme
    // attribute. If either were missing the token would be empty or a different
    // theme's value — so an exact match to the main window proves both landed.
    const surface = await token(page, "--surface-app");
    expect(surface, "the main window resolves the token").not.toBe("");
    await expect.poll(() => token(popup, "--surface-app")).toBe(surface);
    expect(await popup.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe(
      await page.evaluate(() => document.documentElement.getAttribute("data-theme")),
    );
  });

  await test.step("the re-parented xterm is still live in the new window", async () => {
    // Not just present — driven: type into it in the popout and the PTY echoes,
    // which it cannot do if the terminal was rebuilt or the socket was dropped.
    const host = popup.locator(".wb-terminal:not(.is-hidden) .wb-terminal-host");
    await host.locator(".xterm-screen").click();
    await popup.keyboard.type("echo popped-out-and-live");
    await popup.keyboard.press("Enter");
    await expect
      .poll(
        () =>
          host.evaluate((el: HTMLElement & { readTerminalText?: () => string }) =>
            el.readTerminalText === undefined ? "" : el.readTerminalText(),
          ),
        { timeout: 30_000 },
      )
      .toContain("popped-out-and-live");
  });

  await test.step("the pop-out is persisted, and the pane id is unchanged", async () => {
    await expect
      .poll(async () => (await persistedLayout(page)).popoutGroups.length, { timeout: 10_000 })
      .toBeGreaterThan(0);
    const layout = await persistedLayout(page);
    expect(layout.popoutGroups[0]?.url).toBe("/popout.html");
    // Popping a pane out is not renaming it: `terminal#2` is still on disk as
    // `terminal#2`, which is what lets a restart bring it back in its window.
    expect(layout.panels, "the pane id survived the trip").toContain("terminal#2");
  });

  await test.step("the keyboard brings it back into the main window", async () => {
    const closed = popup.waitForEvent("close");
    await runCommand(page, "Bring a popped-out pane back in");
    await closed;
    await expect(page.locator(".wb-panel-tab", { hasText: "Terminal 2" })).toBeVisible();
    await expect(page.locator(".wb-terminal")).toHaveCount(2);
    await expect.poll(async () => (await persistedLayout(page)).popoutGroups.length).toBe(0);
  });

  await test.step("reset leaves the workspace as the next journey expects", async () => {
    await runCommand(page, "Switch to the Default layout");
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await expect(page.locator(".wb-terminal")).toHaveCount(1);
  });
});

test("a Monaco editor pane survives being popped out", async ({ page }) => {
  await openApp(page);

  await page.getByRole("treeitem", { name: NOTES_FILE, exact: true }).click();
  await expect(page.locator(".monaco-editor")).toBeVisible();
  await expect(page.locator(".monaco-editor")).toContainText(NOTES_MARKER);

  // Focus the Editor pane (the default, tabbed one) and pop it out.
  await page.keyboard.press("Control+2");
  const popupPromise = page.waitForEvent("popup");
  await page.keyboard.press("Alt+P");
  const popup = await popupPromise;
  await popup.waitForLoadState("load");

  // Monaco was *moved*, not recreated: the same document, still rendering the
  // same text, in the new window. A rebuild would have lost the content (or the
  // editor entirely) — this is the smoke check the milestone asks for.
  await expect(popup.locator(".monaco-editor")).toBeVisible();
  await expect(popup.locator(".monaco-editor")).toContainText(NOTES_MARKER);

  const closed = popup.waitForEvent("close");
  await runCommand(page, "Bring a popped-out pane back in");
  await closed;
  await runCommand(page, "Switch to the Default layout");
  expect(await panels(page)).toEqual(DEFAULT_PANELS);
});

test("a pane holding a native Office window refuses to pop out", async ({ page }) => {
  await openApp(page);

  // sample.docx docks (the fake host walks launching → embedding → embedded), so
  // the Editor pane now holds a real window — in production a Word with an HWND
  // in the main window's parent chain that a popped-out WebView2 cannot carry.
  await page.getByRole("treeitem", { name: DOCX_FILE, exact: true }).click();
  await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");

  let popped = false;
  page.on("popup", () => {
    popped = true;
  });

  await test.step("Alt+P on the Editor pane is refused, with the reason on screen", async () => {
    // Ctrl+2 focuses the Editor pane deterministically (the docked window is in
    // it), then Alt+P asks to pop that pane out.
    await page.keyboard.press("Control+2");
    await page.keyboard.press("Alt+P");
    // The refusal names the way out — the existing office.detachHost command.
    await expect(
      page.locator(".wb-toast-msg", { hasText: "Move the docked document to the desktop" }),
    ).toBeVisible();
  });

  await test.step("and nothing was orphaned — no window opened, the pane stayed", async () => {
    // The assertion is an absence, so give the popup a moment it never takes.
    await page.waitForTimeout(500);
    expect(popped, "no pop-out window was created").toBe(false);
    await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");
  });

  await test.step("detaching the window to the desktop clears the refusal", async () => {
    // The way out the toast pointed at: once the window is on the desktop the
    // pane holds nothing native, so the same pane pops out.
    await runCommand(page, "Move the docked document to the desktop");
    await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "detached");
    const popupPromise = page.waitForEvent("popup");
    await page.keyboard.press("Control+2");
    await page.keyboard.press("Alt+P");
    const popup = await popupPromise;
    await popup.waitForLoadState("load");
    expect(new URL(popup.url()).pathname).toBe("/popout.html");

    const closed = popup.waitForEvent("close");
    await runCommand(page, "Bring a popped-out pane back in");
    await closed;
  });

  await runCommand(page, "Switch to the Default layout");
  await workspaceReady(page);
});
