/**
 * The popout lifecycle (dockview 7): a window in its own frame is still part of
 * this app.
 *
 * Journey 13 (`popout.spec.ts`) proves a pane really leaves the main window and
 * comes back with its terminal and its editor alive. What it could not prove
 * was anything about the window *after* it opened, because the app had no
 * handle on one: the only hook was the `onDidOpen` option on the
 * `addPopoutGroup` call `Panes.tsx` makes, which fires once, for the one popout
 * that call opened, and never again.
 *
 * dockview 7's `onDidAddPopoutGroup` / `getPopouts()` close that gap, and this
 * is the behaviour they buy — the one a user notices:
 *
 *  - a popped-out pane is themed from the theme *on screen*, not from the last
 *    one written to `localStorage`;
 *  - flipping the app theme repaints the windows that are already out. Before,
 *    a light window sat next to a dark one until it was docked and popped again.
 */

import { expect, request, test, type Page } from "@playwright/test";

import { openApp } from "./app";

const DEFAULT_PANELS = ["Agent", "Editor", "Files", "Terminal"];

async function panels(page: Page): Promise<string[]> {
  const titles = await page.locator(".wb-panel-tab").allTextContents();
  return titles.map((title) => title.replace("×", "").trim()).sort();
}

/** Run a QuickBar command by its row title (main window). */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/** A computed design token in a document — how we prove the theme travelled. */
function token(target: Page, name: string): Promise<string> {
  return target.evaluate(
    (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(),
    name,
  );
}

const themeAttr = (target: Page): Promise<string> =>
  target.evaluate(() => document.documentElement.getAttribute("data-theme") ?? "dark");

test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("a popped-out window is repainted when the app theme flips", async ({ page }) => {
  await openApp(page);
  const started = await themeAttr(page);

  const popupPromise = page.waitForEvent("popup");
  await page.keyboard.press("Control+2"); // the Editor pane, deterministically
  await page.keyboard.press("Alt+P");
  const popup = await popupPromise;
  await popup.waitForLoadState("load");

  await test.step("it opens wearing the theme the main window is wearing", async () => {
    await expect.poll(() => themeAttr(popup)).toBe(started);
    const surface = await token(page, "--surface-app");
    expect(surface, "the main window resolves the token").not.toBe("");
    await expect.poll(() => token(popup, "--surface-app")).toBe(surface);
  });

  await test.step("and it follows the flip, without being docked and popped again", async () => {
    // The whole point of holding a handle on the open windows: before
    // `getPopouts()` there was no way to reach this document again, so it kept
    // the palette it opened with while the rest of the app changed around it.
    await runCommand(page, "Toggle theme");
    const flipped = started === "light" ? "dark" : "light";
    await expect.poll(() => themeAttr(page)).toBe(flipped);
    await expect.poll(() => themeAttr(popup)).toBe(flipped);
    const surface = await token(page, "--surface-app");
    await expect.poll(() => token(popup, "--surface-app")).toBe(surface);
  });

  await test.step("leave the window as the journeys after this one expect it", async () => {
    const closed = popup.waitForEvent("close");
    await runCommand(page, "Bring a popped-out pane back in");
    await closed;
    await runCommand(page, "Toggle theme");
    await expect.poll(() => themeAttr(page)).toBe(started);
    await runCommand(page, "Switch to the Default layout");
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
  });
});
