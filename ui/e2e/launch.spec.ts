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
