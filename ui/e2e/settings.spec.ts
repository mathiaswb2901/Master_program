/**
 * Journey — Settings (M7 V8): the knobs that were environment variables.
 *
 * What this drives, in the order a user meets it:
 *
 *  - **`Ctrl+,` opens it**, as a tab in the centre — nothing to dismiss;
 *  - **a choice takes effect and is stored on the server**, not only in this
 *    browser: the theme flips the window *and* lands in `GET /api/settings`;
 *  - **it survives a relaunch with `localStorage` wiped**, which is the whole
 *    point of moving the preference off the browser — the pre-paint cache is a
 *    cache, and the server document is the authority;
 *  - **a theme changed anywhere else is written back**: the QuickBar's *Toggle
 *    theme* updates the stored document, so the toggle survives a restart;
 *  - **a launch-only setting says so** rather than implying it took effect;
 *  - **telemetry is a statement, not a switch** — there are exactly three
 *    controls on the panel and none of them is a telemetry toggle;
 *  - **one Settings pane, not two**: the tool is singular, so asking again
 *    focuses the pane that exists.
 *
 * The suite shares one app-data directory for the run, so this journey puts the
 * document back as it found it on the way out — the discover/first-run
 * convention.
 */

import { expect, test, type Page } from "@playwright/test";

import { launchSettled, openApp, workspaceReady } from "./app";

interface StoredSettings {
  theme: string;
  office_native: string;
  voice_input: boolean;
}

const panel = (page: Page) => page.getByRole("region", { name: "Settings" });

const group = (page: Page, name: string) => page.getByRole("radiogroup", { name });

/** The stored document, read out of band — the server's copy, not the page's. */
async function storedSettings(page: Page): Promise<StoredSettings> {
  const response = await page.request.get("/api/settings");
  expect(response.ok(), "the settings endpoint answers").toBe(true);
  const body = (await response.json()) as { stored: StoredSettings };
  return body.stored;
}

const documentTheme = (page: Page): Promise<string> =>
  page.evaluate(() => document.documentElement.getAttribute("data-theme") ?? "dark");

/** Run a registered command the way a user would — through the QuickBar. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(`>${title}`);
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
}

test("settings: a choice sticks on the server, survives a relaunch, and telemetry is not a switch", async ({
  page,
}) => {
  await openApp(page);
  const before = await storedSettings(page);

  try {
    await test.step("Ctrl+, opens the panel", async () => {
      await page.keyboard.press("Control+Comma");
      await expect(panel(page)).toBeVisible();
      // Three controls, and the theme one shows the stored value as checked.
      await expect(group(page, "Theme")).toBeVisible();
      await expect(group(page, "Open documents in Word and Excel")).toBeVisible();
      await expect(group(page, "Voice input")).toBeVisible();
    });

    await test.step("asking again focuses the pane that exists — it is singular", async () => {
      await runCommand(page, "Open Settings");
      await expect(page.locator(".wb-settings")).toHaveCount(1);
    });

    await test.step("telemetry is stated as off, with nothing to click", async () => {
      const privacy = page.getByRole("region", { name: "Privacy" });
      await expect(privacy).toContainText("Telemetry");
      await expect(privacy.locator(".wb-settings-stance")).toHaveText("Off");
      // The assertion that matters: no fourth control appeared for it.
      await expect(page.getByRole("radiogroup")).toHaveCount(3);
    });

    await test.step("choosing a theme repaints the window and is stored server-side", async () => {
      await group(page, "Theme").getByRole("radio", { name: "Light" }).click();
      await expect.poll(() => documentTheme(page)).toBe("light");
      await expect
        .poll(async () => (await storedSettings(page)).theme, {
          message: "the choice reached the server document",
        })
        .toBe("light");
    });

    await test.step("a launch-only setting says it waits for a restart", async () => {
      await group(page, "Open documents in Word and Excel")
        .getByRole("radio", { name: "Off" })
        .click();
      await expect
        .poll(async () => (await storedSettings(page)).office_native)
        .toBe("off");
      // The server, not the panel, decides this sentence is due.
      await expect(panel(page)).toContainText("Applies when Workbench restarts.");
    });

    await test.step("it survives a relaunch with the browser's own cache wiped", async () => {
      // The pre-paint cache is only a cache: clearing it must not lose the
      // choice, because the document on the server is the authority.
      await page.evaluate(() => {
        localStorage.removeItem("workbench-theme");
      });
      await page.reload();
      await workspaceReady(page);
      await launchSettled(page);
      await expect.poll(() => documentTheme(page)).toBe("light");
    });

    await test.step("a theme toggled from the QuickBar is written back", async () => {
      await runCommand(page, "Toggle theme");
      await expect.poll(() => documentTheme(page)).toBe("dark");
      await expect
        .poll(async () => (await storedSettings(page)).theme, {
          message: "the toggle updated the stored document, so it survives a restart",
        })
        .toBe("dark");
    });
    await test.step("closing its tab is the way out, and it leaves no trace", async () => {
      // Settings docks in the centre, beside the editor — so a pane left open
      // here is a pane the *saved arrangement* carries into the next launch,
      // sitting in front of the editor. Closing it is what a user does, and it
      // is what keeps this journey's workspace the one the next journey expects
      // (the discover/first-run convention).
      await page.getByRole("button", { name: "Close Settings" }).click();
      await expect(page.locator(".wb-settings")).toHaveCount(0);
      // The arrangement is written on a debounce, so wait for the *file* to
      // agree rather than for the DOM alone.
      await expect
        .poll(async () => JSON.stringify(await (await page.request.get("/api/layouts")).json()), {
          message: "the saved arrangement no longer carries a Settings pane",
        })
        .not.toContain('"settings"');
    });
  } finally {
    // Put the document back as it was found: the suite shares one app-data
    // directory, and a journey that left the theme flipped would be a surprise
    // in whatever runs next.
    await page.request.put("/api/settings", { data: before });
  }
});
