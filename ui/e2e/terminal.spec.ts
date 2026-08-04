/**
 * Journey 2 — terminals: real ConPTY, several tabs, state that survives.
 *
 * Asserts:
 *  - Alt+T (the command registry's chord) opens a second terminal tab;
 *  - a command typed into the second tab really runs in a PowerShell PTY — the
 *    marker asserted is the shell's *output*, not the echo of what was typed;
 *  - closing that tab returns to the first one, whose scrollback is intact
 *    (each instance stays mounted across tab switches).
 */

import { expect, test } from "@playwright/test";

import { openApp, runInTerminal, terminal, terminalText } from "./app";

test("second terminal runs a command; the first keeps its scrollback", async ({ page }) => {
  await openApp(page);

  await test.step("leave a marker in the first terminal", async () => {
    await runInTerminal(page, 'echo "first-$(1+1)"');
    await expect(terminal(page).locator(".xterm-rows")).toContainText("first-2");
  });

  await test.step("Alt+T opens a second terminal", async () => {
    await page.keyboard.press("Alt+t");
    await expect(page.locator(".wb-term-tab")).toHaveCount(2);
    await expect(page.locator(".wb-term-tab.is-active")).toContainText("Terminal 2");
  });

  await test.step("run a command in the real PTY", async () => {
    // The output ("e2e-5") differs from the input ("$(2+3)"), so finding it
    // proves PowerShell evaluated the line rather than xterm echoing it.
    await runInTerminal(page, 'echo "e2e-$(2+3)"');
    await expect(terminal(page).locator(".xterm-rows")).toContainText("e2e-5");
  });

  await test.step("closing the tab returns to terminal 1 with its scrollback", async () => {
    await page.getByRole("button", { name: "Close Terminal 2" }).click();
    await expect(page.locator(".wb-term-tab")).toHaveCount(1);
    await expect(terminal(page).locator(".xterm-rows")).toContainText("first-2");
    expect(await terminalText(page)).not.toContain("e2e-5");
  });
});
