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

import { expect, type Page, test } from "@playwright/test";

import { expectTerminal, openApp, runInTerminal, terminal, terminalText } from "./app";

/** Which renderer owns the visible terminal right now. Never both — see below. */
function rendererState(page: Page): Promise<{ gpu: boolean; dom: boolean }> {
  return terminal(page).evaluate((el) => ({
    gpu: el.querySelectorAll(".xterm-screen canvas:not(.xterm-link-layer)").length > 0,
    dom: el.querySelectorAll(".xterm-rows").length > 0,
  }));
}

test("second terminal runs a command; the first keeps its scrollback", async ({ page }) => {
  await openApp(page);

  await test.step("the terminal is drawn by exactly one renderer", async () => {
    // The GPU renderer hangs an unclassed canvas in `.xterm-screen`; the DOM
    // renderer lays out `.xterm-rows` instead. (`canvas.xterm-link-layer` is
    // there either way, which is why it cannot be the discriminator.) Both
    // outcomes are supported — a machine with no WebGL2 must still get a
    // working terminal — but *neither* means a blank panel, and *both* would
    // mean the fallback fired without the addon letting go.
    //
    // Polled, not sampled once: the addon is a lazily imported chunk, so the
    // terminal opens on the DOM renderer and swaps a tick or two later.
    // Chromium here has WebGL2, so this run takes the GPU path; the refusal
    // fallbacks are unit-tested in `src/terminalRenderer.test.ts` and were
    // exercised for real against `chromium --disable-3d-apis`.
    await expect.poll(() => rendererState(page)).toMatchObject({ gpu: true, dom: false });
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

/** What the DOM renderer has actually laid out — empty while WebGL owns the screen. */
function renderedText(page: Page): Promise<string> {
  return terminal(page).evaluate((el) => el.querySelector(".xterm-rows")?.textContent ?? "");
}

/**
 * The failure this whole fallback exists for, done for real.
 *
 * The unit tests drive `onContextLoss` on a hand-written stub, which proves
 * this module's bookkeeping and nothing about xterm: if `WebglAddon.dispose()`
 * did not really hand the screen back — a stale canvas left in place, or a
 * terminal that simply stops updating — those tests would still pass while a
 * driver reset or a GPU sleep blanked the panel mid-session for a real user.
 * So kill the context the way the driver does, through `WEBGL_lose_context` on
 * the live canvas, and require the terminal to keep working.
 *
 * Asserted on the *rendered rows*, not on xterm's buffer: the buffer keeps
 * filling from the socket whether or not anything is drawing, so reading it
 * here would pass against exactly the blank terminal this guards.
 */
test("a lost WebGL context hands the screen back to the DOM renderer", async ({ page }) => {
  await openApp(page);
  await expect.poll(() => rendererState(page)).toMatchObject({ gpu: true, dom: false });

  await test.step("leave output on screen, then kill the GL context", async () => {
    await runInTerminal(page, 'echo "before-$(1+2)"');
    await expectTerminal(page, "before-3");

    const killed = await terminal(page).evaluate((el) => {
      for (const canvas of el.querySelectorAll("canvas")) {
        // A canvas already holding a 2d context (the link layer) returns null
        // here rather than being handed a GL one, so this finds the addon's.
        const gl = canvas.getContext("webgl2") as WebGL2RenderingContext | null;
        const ext = gl?.getExtension("WEBGL_lose_context");
        if (ext) {
          ext.loseContext();
          return true;
        }
      }
      return false;
    });
    // If nothing was lost the rest of this test proves nothing at all.
    expect(killed, "no live WebGL context on the terminal canvas").toBe(true);
  });

  await test.step("the DOM renderer takes over, with the scrollback intact", async () => {
    await expect.poll(() => rendererState(page)).toMatchObject({ gpu: false, dom: true });
    await expect.poll(() => renderedText(page)).toContain("before-3");
  });

  await test.step("and the terminal still draws what the PTY sends after the loss", async () => {
    await runInTerminal(page, 'echo "after-$(3+4)"');
    await expect.poll(() => renderedText(page)).toContain("after-7");
  });
});
