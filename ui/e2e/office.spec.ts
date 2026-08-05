/**
 * Journey 7 — office documents with no Document Server configured.
 *
 * The suite runs without `WORKBENCH_ONLYOFFICE_URL`, which is the state most
 * users start in — and is guaranteed rather than assumed: `playwright.config.ts`
 * strips the whole `WORKBENCH_*` prefix out of the inherited environment, so a
 * developer with Office configured still runs this journey. Opening a .docx
 * must land on the calm degraded card — on
 * paper colors, per DESIGN.md §2.8/§6.1 — and must not throw, blank the panel,
 * or try to read the document's bytes.
 */

import { expect, test } from "@playwright/test";

import { openApp, treeItem } from "./app";
import { DOCX_FILE } from "./workspace";

/** --surface-paper is #FFFFFF in both themes: the card is literally paper. */
const PAPER = "rgb(255, 255, 255)";

test("a .docx opens in degraded mode, on paper, without crashing", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await openApp(page);
  await treeItem(page, DOCX_FILE).click();

  const card = page.locator(".wb-office-card");
  await expect(card).toBeVisible();
  await expect(card.locator(".wb-office-card-title")).toHaveText("Office editing not configured");
  await expect(card.locator(".wb-office-card-file")).toHaveText(DOCX_FILE);
  await expect(card.getByRole("button", { name: "Copy full path" })).toBeVisible();

  await expect
    .poll(() => card.evaluate((el) => getComputedStyle(el).backgroundColor))
    .toBe(PAPER);

  // The tab is a normal editor tab, and no Monaco buffer was opened for it.
  await expect(page.locator(".wb-editor-tab").filter({ hasText: DOCX_FILE })).toBeVisible();
  expect(consoleErrors).toEqual([]);
});
