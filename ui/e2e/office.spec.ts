/**
 * Journey 7 — opening an Office document, every way it can go.
 *
 * The suite runs with `WORKBENCH_OFFICE_FAKE=1` and **without**
 * `WORKBENCH_ONLYOFFICE_URL` (`playwright.config.ts` strips the whole
 * `WORKBENCH_*` prefix out of the inherited environment, so a developer with
 * Office configured still runs this journey the way CI does). That pair is
 * deliberate: the host lifecycle is the fake backend's, and the floor under
 * every refusal is the honest degraded card. No Office, no Rust and no native
 * window anywhere — and still every state a user can land in.
 *
 * What must hold:
 *
 *  1. A .docx docks, and the panel says which application holds it.
 *  2. A refusal is an *explanation* with a way out — "already open somewhere
 *     else" is Workbench keeping its promise never to take over a window it did
 *     not start — and one click reads the document anyway.
 *  3. An embed that fails ends in a working editor, not a broken panel.
 *  4. PowerPoint is preview-only and says why, in a sentence rather than an
 *     error.
 *  5. The degraded card is still the floor under all of it, on paper colors
 *     (DESIGN.md §2.8/§6.1).
 */

import { expect, test } from "@playwright/test";

import { openApp, treeItem } from "./app";
import { DOCX_ALREADY_OPEN, DOCX_FILE, DOCX_REFUSES_EMBED, PPTX_FILE } from "./workspace";

/** --surface-paper is #FFFFFF in both themes: the card is literally paper. */
const PAPER = "rgb(255, 255, 255)";
const DEGRADED = "Office editing not configured";

test("a .docx docks, and the panel names the application holding it", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await openApp(page);
  await treeItem(page, DOCX_FILE).click();

  // launching -> embedding -> embedded, driven by the server: the badge appears
  // only once the *host* says the window is in the panel.
  await expect(page.locator(".wb-office-hosted")).toHaveText(/Microsoft Word/i);
  await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");
  // And it says out loud that nothing is really open, which is the one thing
  // the fake must never be quiet about.
  await expect(page.locator(".wb-office-note")).toContainText("Simulated host");

  await expect(page.locator(".wb-editor-tab").filter({ hasText: DOCX_FILE })).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test("a document open somewhere else is explained, and still readable", async ({ page }) => {
  await openApp(page);
  await treeItem(page, DOCX_ALREADY_OPEN).click();

  const card = page.locator(".wb-office-card");
  await expect(card.locator(".wb-office-card-title")).toHaveText("Already open somewhere else");
  await expect(card.locator(".wb-office-card-hint")).toContainText(
    "will not take over the copy you already have open",
  );
  await expect(card.locator(".wb-office-card-file")).toHaveText(DOCX_ALREADY_OPEN);

  // A refusal is not a dead end: one click lands on the OnlyOffice path, which
  // here has no Document Server and so shows its own card.
  await card.getByRole("button", { name: "Open a preview here" }).click();
  await expect(page.locator(".wb-office-card-title")).toHaveText(DEGRADED);
});

test("an embed that is refused ends in an editor, never a broken panel", async ({ page }) => {
  await openApp(page);
  await treeItem(page, DOCX_REFUSES_EMBED).click();

  const card = page.locator(".wb-office-card");
  await expect(card.locator(".wb-office-card-title")).toHaveText("The window would not dock");
  await card.getByRole("button", { name: "Open a preview here" }).click();
  await expect(page.locator(".wb-office-card-title")).toHaveText(DEGRADED);
});

test("PowerPoint is preview-only, on paper, and says why", async ({ page }) => {
  await openApp(page);
  await treeItem(page, PPTX_FILE).click();

  // Straight to the preview path — no launch is attempted at all — with the
  // reason above the document rather than an error in place of it.
  await expect(page.locator(".wb-office-note")).toContainText("PowerPoint runs one instance");
  const card = page.locator(".wb-office-card");
  await expect(card.locator(".wb-office-card-title")).toHaveText(DEGRADED);
  await expect(card.locator(".wb-office-card-file")).toHaveText(PPTX_FILE);
  await expect(card.getByRole("button", { name: "Copy full path" })).toBeVisible();
  await expect
    .poll(() => card.evaluate((el) => getComputedStyle(el).backgroundColor))
    .toBe(PAPER);
});
