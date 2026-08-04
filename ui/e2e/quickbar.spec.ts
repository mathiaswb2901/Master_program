/**
 * Journey 3 — QuickBar, shortcuts.md and the never-run rule.
 *
 * Asserts:
 *  - the malformed entry seeded into `<workspace>/.workbench/shortcuts.md`
 *    surfaces as a problems toast rather than dying silently;
 *  - Ctrl+Shift+P opens the QuickBar in command mode, with keycaps on the rows
 *    that carry a chord;
 *  - file-supplied shortcuts appear under their own "Shortcuts" category;
 *  - running a shell shortcut *types* the snippet into the terminal and stops
 *    there: the text sits on the prompt line, and the shell produced no output
 *    for it (the snippet appears exactly once — an executed `echo` would print
 *    its argument a second time).
 */

import { expect, test } from "@playwright/test";

import { openApp, terminal, terminalText } from "./app";
import { BROKEN_SHORTCUT_NAME, SHORTCUT_BODY, SHORTCUT_NAME } from "./workspace";

const MARKER = "e2e-shortcut-marker";

test("command mode, shortcut categories, and a snippet that never runs", async ({ page }) => {
  await openApp(page);

  await test.step("the malformed entry raises the problems toast", async () => {
    // Toasts auto-dismiss after ~6 s, so this is asserted first: it is raised
    // during the initial shortcuts load, before anything else happens.
    await expect(page.locator(".wb-toast.is-warn")).toContainText(BROKEN_SHORTCUT_NAME);
    await expect(page.locator(".wb-toast.is-warn")).toContainText(".workbench/shortcuts.md");
  });

  await test.step("Ctrl+Shift+P opens command mode with keycaps", async () => {
    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await expect(quickbar).toBeVisible();
    await expect(quickbar.locator(".wb-qb-input")).toHaveValue(">");
    const newTerminalRow = quickbar.locator(".wb-qb-row", { hasText: "New terminal" }).first();
    await expect(newTerminalRow.locator(".wb-keycap")).toHaveText(["Alt", "T"]);
  });

  await test.step("shortcuts.md entries get their own category", async () => {
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await expect(quickbar.locator(".wb-qb-cat")).toContainText("Shortcuts");
    const row = quickbar.locator(".wb-qb-row", { hasText: SHORTCUT_NAME }).first();
    // The row shows the snippet itself, never the file's own description.
    await expect(row).toContainText(SHORTCUT_BODY);
  });

  await test.step("running the shortcut types it without executing it", async () => {
    await page
      .getByRole("dialog", { name: "Quick open" })
      .locator(".wb-qb-row", { hasText: SHORTCUT_NAME })
      .first()
      .click();
    await expect(terminal(page).locator(".xterm-rows")).toContainText(MARKER);

    const text = await terminalText(page);
    const occurrences = text.split(MARKER).length - 1;
    expect(occurrences, "the snippet is on the prompt line and was never run").toBe(1);
    // The line it sits on is the live prompt, not a finished command's output.
    expect(text.slice(text.lastIndexOf("PS "))).toContain(SHORTCUT_BODY);
  });
});
