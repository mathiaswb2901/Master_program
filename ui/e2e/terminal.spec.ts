/**
 * Journey 2 — terminals: real ConPTY, several tabs, state that survives.
 *
 * Asserts:
 *  - Alt+T (the command registry's chord) opens a second terminal tab;
 *  - a command typed into the second tab really runs in a PowerShell PTY — the
 *    marker asserted is the shell's *output*, not the echo of what was typed;
 *  - closing that tab returns to the first one, whose scrollback is intact
 *    (each instance stays mounted across tab switches).
 *
 * Marker rule: a PowerShell prompt prints its CWD, so the temp workspace path
 * is on screen in every assertion here. A marker must therefore share no
 * literal prefix with `workbench-e2e-<random>`. The original `e2e-5` did, and
 * failed in CI on the run that drew `workbench-e2e-54ic0X` — `mkdtemp`'s suffix
 * carries no hyphen, so `term2-` cannot be manufactured the same way.
 */

import { expect, test } from "@playwright/test";

import { expectTerminal, openApp, runInTerminal, terminal, terminalText } from "./app";

test("second terminal runs a command; the first keeps its scrollback", async ({ page }) => {
  await openApp(page);

  await test.step("the terminal is drawn by exactly one renderer", async () => {
    // The GPU renderer hangs an unclassed canvas in `.xterm-screen`; the DOM
    // renderer lays out `.xterm-rows` instead. (`canvas.xterm-link-layer` is
    // there either way, which is why it cannot be the discriminator.) Both
    // outcomes are supported — a machine with no WebGL2 must still get a
    // working terminal — but *neither* means a blank panel, and *both* would
    // mean the fallback fired without the addon letting go.
    const state = await terminal(page).evaluate((el) => ({
      gpu: el.querySelectorAll(".xterm-screen canvas:not(.xterm-link-layer)").length,
      dom: el.querySelectorAll(".xterm-rows").length,
    }));
    expect(state.gpu > 0 || state.dom > 0).toBe(true);
    expect(state.gpu > 0 && state.dom > 0).toBe(false);
    // Chromium here has WebGL2, so this run takes the GPU path; the two
    // fallbacks are unit-tested in `src/terminalRenderer.test.ts` and were
    // exercised for real against `chromium --disable-3d-apis`.
    expect(state.gpu).toBeGreaterThan(0);
  });

  await test.step("leave a marker in the first terminal", async () => {
    await runInTerminal(page, 'echo "term1-$(1+1)"');
    await expectTerminal(page, "term1-2");
  });

  await test.step("Alt+T opens a second terminal", async () => {
    await page.keyboard.press("Alt+t");
    await expect(page.locator(".wb-term-tab")).toHaveCount(2);
    await expect(page.locator(".wb-term-tab.is-active")).toContainText("Terminal 2");
  });

  await test.step("run a command in the real PTY", async () => {
    // The output ("term2-5") differs from the input ("$(2+3)"), so finding it
    // proves PowerShell evaluated the line rather than xterm echoing it.
    await runInTerminal(page, 'echo "term2-$(2+3)"');
    await expectTerminal(page, "term2-5");
  });

  await test.step("closing the tab returns to terminal 1 with its scrollback", async () => {
    await page.getByRole("button", { name: "Close Terminal 2" }).click();
    await expect(page.locator(".wb-term-tab")).toHaveCount(1);
    await expectTerminal(page, "term1-2");
    expect(await terminalText(page)).not.toContain("term2-5");
  });
});
