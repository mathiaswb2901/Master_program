/**
 * The launch experience — the app never looks stuck while the backend boots.
 *
 * The perceived-speed fix has two halves, and this journey pins the seam between
 * them: a branded loading state is painted by the raw HTML *before* any app JS
 * runs (the inline splash in index.html), and React takes over cleanly once the
 * backend answers, removing the overlay with no blank/white gap in between.
 *
 * The harness backend is fast, so this asserts the *ordering and hand-off*, not
 * a duration: the splash is in the first paint, and the app chrome replaces it.
 */

import { expect, test } from "@playwright/test";

import { BOOT_ENGINE_MS, BOOT_TIMEOUT_MS } from "../src/boot";
import { openApp } from "./app";

test("a branded, non-blank loading state ships in the first paint", async ({ page }) => {
  // The raw document, before a byte of the bundle evaluates. This is literally
  // what the webview composites first, so whatever is here is the first paint.
  const html = await (await page.request.get("/")).text();

  // Branded: the mark, the wordmark and an honest status line — not a blank
  // page and not a raw error.
  expect(html).toContain('id="wb-splash"');
  expect(html).toContain("Workbench");
  expect(html).toContain("Starting Workbench");

  // Never a white flash: the root and the overlay are painted the ANVIL app
  // surface from the first frame, so there is no default-white gap before the
  // stylesheet lands.
  expect(html).toContain("background: #393939");
  expect(html).not.toContain("background: #ffffff");
});

test("React takes over and removes the splash once the backend is up", async ({ page }) => {
  await openApp(page);

  // Hand-off complete: the app chrome is on screen and the pre-JS overlay is
  // gone. Its removal is what proves the boot gate handed off rather than the
  // overlay simply sitting on top of a working app.
  await expect(page.locator(".wb-dock")).toBeVisible();
  await expect(page.locator("#wb-splash")).toHaveCount(0);
});

test("the boot gate advances its copy and offers Retry when the engine never answers", async ({
  page,
}) => {
  // Drive the gate's own timers by a controlled clock instead of waiting real
  // seconds, and stall the one handshake a browser boot blocks on (the auth
  // token) so the gate stays mounted rather than handing off to the app. This
  // is the only path that reaches the 'engine' and 'failed' copy the fast
  // harness backend otherwise skips straight past.
  await page.clock.install();
  let tokenRequests = 0;
  await page.route("**/api/auth/token", () => {
    // Never fulfilled: `tokenReady` stays false, so `App` keeps `<BootGate/>`
    // on screen for the whole timeline below.
    tokenRequests += 1;
  });

  await page.goto("/");

  const status = page.locator(".wb-boot-status");

  // First frame: the calm opening line, verbatim what the inline splash paints.
  await expect(status).toHaveText("Starting Workbench…");

  // Past the engine threshold: the copy owns up to the local engine. This is
  // exactly `bootPhaseAt(BOOT_ENGINE_MS)` rendered — the value `boot.test.ts`
  // pins — proving the tested function drives the shipped component.
  await page.clock.fastForward(BOOT_ENGINE_MS);
  await expect(status).toHaveText("Starting the local engine…");

  // Past the generous timeout: an honest error surfaced as an alert with a
  // Retry, never an endless spinner.
  await page.clock.fastForward(BOOT_TIMEOUT_MS);
  const boot = page.getByRole("alert");
  await expect(boot).toBeVisible();
  await expect(status).toHaveText("Workbench could not reach its local engine.");

  // Retry reloads the app: the boot's handshake fires a second time.
  await page.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => tokenRequests).toBeGreaterThan(1);
});
