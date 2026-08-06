/**
 * Journey 10 — the theme a file opens with, when the toggle races the load.
 *
 * Monaco no longer loads at boot: it is warmed on an idle callback once the
 * workspace tree lands, and its theme is defined when that chunk finishes
 * importing. That opens a window — between "the prefetch is on the wire" and
 * "the bundle has evaluated" — in which `defineWorkbenchTheme` cannot do
 * anything, because there is no Monaco to define a theme on yet. A user who
 * toggles the app theme inside that window used to get an editor themed by
 * neither palette: the load defined the theme name it had captured when the
 * prefetch was *scheduled* while reading the tokens of the theme now on screen,
 * so `workbench-light` was registered with dark colors and `workbench-dark` —
 * the name `<Editor theme=…>` then asks for — was never registered at all.
 * Monaco falls back to a built-in theme for a name it does not know, silently,
 * so the first file opened rendered on a white `vs` background under dark app
 * chrome until the theme was toggled a second time.
 *
 * The window is milliseconds wide in normal use, so this journey widens it the
 * only way that keeps everything else real: the chunk's *response* is held on
 * the wire while the user toggles. Nothing about the app is stubbed, and no
 * timer is waited on — the hold is released on the app's own request.
 */

import { expect, test, type Page } from "@playwright/test";

import { editor, gotoApp, treeItem, workspaceReady } from "./app";
import { NOTES_FILE } from "./workspace";

/** Vite names the dynamic chunk after its entry module (`monacoBundle.ts`). */
const MONACO_CHUNK = "**/assets/monacoBundle-*.js";

/** `--surface-panel` of the theme on screen, as the browser's own `rgb(…)`
 * string — the exact form a computed background reads back in. */
function panelToken(page: Page): Promise<string> {
  return page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.backgroundColor = getComputedStyle(document.documentElement)
      .getPropertyValue("--surface-panel")
      .trim();
    document.body.append(probe);
    const value = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return value;
  });
}

/** What Monaco actually painted the buffer with. */
function editorBackground(page: Page): Promise<string> {
  return editor(page)
    .locator(".monaco-editor-background")
    .first()
    .evaluate((el) => getComputedStyle(el).backgroundColor);
}

const documentTheme = (page: Page): Promise<string> =>
  page.evaluate(() => document.documentElement.getAttribute("data-theme") ?? "dark");

test("a theme toggled while Monaco is still loading still themes the first file", async ({
  page,
}) => {
  let release = (): void => undefined;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(MONACO_CHUNK, async (route) => {
    await held;
    await route.continue();
  });

  try {
    const prefetched = page.waitForRequest(MONACO_CHUNK);
    await gotoApp(page);
    await workspaceReady(page);

    // The idle prefetch has fired and its bytes are held: the app is now
    // exactly where the bug lived — a load in flight, no Monaco yet.
    await prefetched;
    const before = await documentTheme(page);

    await test.step("toggle the theme while the editor is still on the wire", async () => {
      await page.keyboard.press("Control+Shift+P");
      const quickbar = page.getByRole("dialog", { name: "Quick open" });
      await quickbar.locator(".wb-qb-input").fill(">Toggle theme");
      await quickbar.locator(".wb-qb-row", { hasText: "Toggle theme" }).first().click();
      // The app chrome repaints immediately — this half never broke.
      await expect.poll(() => documentTheme(page)).not.toBe(before);
    });

    release();

    await test.step("the first file opens in the theme that is on screen", async () => {
      await treeItem(page, NOTES_FILE).click();
      await expect(editor(page)).toBeVisible();
      // Not "some workbench theme": the buffer's background is the panel token
      // of the theme the rest of the window is wearing. Before the fix this
      // read Monaco's built-in fallback, because the name the editor asked for
      // had never been defined.
      await expect.poll(() => editorBackground(page)).toBe(await panelToken(page));
    });

    await test.step("and toggling back is still correct", async () => {
      // The steady-state path, which always worked, must keep working: with
      // Monaco loaded, `setTheme` redefines the theme from the live tokens.
      await page.keyboard.press("Control+Shift+P");
      const quickbar = page.getByRole("dialog", { name: "Quick open" });
      await quickbar.locator(".wb-qb-input").fill(">Toggle theme");
      await quickbar.locator(".wb-qb-row", { hasText: "Toggle theme" }).first().click();
      await expect.poll(() => documentTheme(page)).toBe(before);
      await expect.poll(() => editorBackground(page)).toBe(await panelToken(page));
    });
  } finally {
    // A failure above must not leave the route handler awaiting forever.
    release();
  }
});
