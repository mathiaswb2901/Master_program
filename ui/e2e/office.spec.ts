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
 *  4. All three applications dock — and a PowerPoint that cannot says so in a
 *     sentence naming the fix rather than an error, with the *right* fix for
 *     whose instance is in the way: the user's own, or the deck Workbench is
 *     already holding in the one process PowerPoint gives out.
 *  5. The degraded card is still the floor under all of it, on paper colors
 *     (DESIGN.md §2.8/§6.1).
 */

import { expect, test, type Page } from "@playwright/test";

import { openApp, treeItem } from "./app";
import {
  DOCX_ALREADY_OPEN,
  DOCX_FILE,
  DOCX_REFUSES_EMBED,
  PPTX_APP_RUNNING,
  PPTX_FILE,
  PPTX_SECOND,
  XLSX_FILE,
  XLSX_REFUSES_EMBED,
} from "./workspace";

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

  // The machine's Office sign-in shows as one quiet line: the fake backend
  // reports a signed-in, licensed "Analyst", so the panel says who is signed in
  // rather than the "sign into Word to edit" degrade.
  await expect(page.locator(".wb-office-identity")).toHaveText(/Signed in as Analyst/);

  await expect(page.locator(".wb-editor-tab").filter({ hasText: DOCX_FILE })).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test("an .xlsx docks, and the panel names Microsoft Excel", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await openApp(page);
  await treeItem(page, XLSX_FILE).click();

  // Excel walks the same launching -> embedding -> embedded the Word path does,
  // and the badge names the *program* holding the window — the whole point of
  // extending native hosting to XLMAIN.
  await expect(page.locator(".wb-office-hosted")).toHaveText(/Microsoft Excel/i);
  await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");
  await expect(page.locator(".wb-office-note")).toContainText("Simulated host");

  await expect(page.locator(".wb-editor-tab").filter({ hasText: XLSX_FILE })).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test("a workbook whose window will not dock ends in an editor, never a broken panel", async ({
  page,
}) => {
  await openApp(page);
  await treeItem(page, XLSX_REFUSES_EMBED).click();

  const card = page.locator(".wb-office-card");
  await expect(card.locator(".wb-office-card-title")).toHaveText("The window would not dock");
  await card.getByRole("button", { name: "Open a preview here" }).click();
  await expect(page.locator(".wb-office-card-title")).toHaveText(DEGRADED);
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

// The three PowerPoint journeys share one server, and PowerPoint is the one
// application whose *previous* journey changes the next one's answer: it runs a
// single instance for the whole session, so a deck left docked is still docked
// when the next test's page loads. Two habits keep that from turning into
// order-dependence. The refusal about the user's *own* PowerPoint is asked
// first, while Workbench is holding nothing; and every journey that docks a deck
// closes its tab on the way out, which is what a user does and what gives the
// window back. (`workers: 1` / `fullyParallel: false` in `playwright.config.ts`
// is what makes "first" mean anything.)

/** Close an editor tab the way the user does, and wait for the pane to go. The
 * unmount is what fires the host's `close`, so this is also how a journey hands
 * the single PowerPoint process back to the one after it. */
async function closeTab(page: Page, name: string): Promise<void> {
  await page.getByRole("button", { name: `Close ${name}` }).click();
  await expect(page.locator(".wb-editor-tab").filter({ hasText: name })).toHaveCount(0);
}

test("a deck refused because PowerPoint is open explains itself, on paper", async ({ page }) => {
  await openApp(page);
  await treeItem(page, PPTX_APP_RUNNING).click();

  // The one refusal unique to PowerPoint: it runs a single instance per session,
  // so Workbench will not start its own alongside the user's — and will not take
  // over theirs. A sentence with the action that changes the answer, not an error.
  const card = page.locator(".wb-office-card");
  await expect(card.locator(".wb-office-card-title")).toHaveText("PowerPoint is already open");
  await expect(card.locator(".wb-office-card-hint")).toContainText(
    "will not take over the one you are working in",
  );
  await expect(card.locator(".wb-office-card-file")).toHaveText(PPTX_APP_RUNNING);

  // And it is not a dead end: the preview path is one click away.
  await card.getByRole("button", { name: "Open a preview here" }).click();
  await expect(card.locator(".wb-office-card-title")).toHaveText(DEGRADED);
  await expect
    .poll(() => card.evaluate((el) => getComputedStyle(el).backgroundColor))
    .toBe(PAPER);
});

test("a .pptx docks, and the panel names Microsoft PowerPoint", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await openApp(page);
  await treeItem(page, PPTX_FILE).click();

  // The third application walks the same launching -> embedding -> embedded the
  // Word and Excel paths do. PPTFrameClass is not a special case in the panel:
  // it resolves through `hostable_kinds` like the other two.
  await expect(page.locator(".wb-office-hosted")).toHaveText(/Microsoft PowerPoint/i);
  await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");
  await expect(page.locator(".wb-office-note")).toContainText("Simulated host");

  await expect(page.locator(".wb-editor-tab").filter({ hasText: PPTX_FILE })).toBeVisible();
  expect(consoleErrors).toEqual([]);

  // Hand the one PowerPoint instance back before the next journey asks for it.
  await closeTab(page, PPTX_FILE);
});

test("a second deck opens as a preview and names the tab holding PowerPoint", async ({ page }) => {
  await openApp(page);

  // Two office documents are open by the end of this, and an office view is
  // `keepMounted` — both panes stay in the DOM and the inactive one is only
  // `display: none`. So every assertion below is scoped to the pane on screen;
  // an unscoped `.wb-office-note` would match the docked deck's "Simulated host"
  // line as happily as the one this journey is about.
  const pane = page.locator(".wb-office-host:not(.is-hidden)");

  // One deck docked — the ordinary case, and the thing that consumes the single
  // PowerPoint process for as long as the tab is open. Waited for through the
  // status bar as well as the pane: the capabilities re-read that the *next*
  // click depends on is fired by the host's first event, so a checkpoint two
  // events later is what makes "the answer has changed by now" a fact rather
  // than a hope.
  await treeItem(page, PPTX_FILE).click();
  await expect(pane.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");
  await expect(page.locator(".wb-status-item", { hasText: "1 document docked" })).toBeVisible();

  // Now the second. It must not attempt a launch it cannot win: the server has
  // taken `powerpoint` out of `hostable_kinds` — an answer it reads from its own
  // hosts, because docking reparents the frame and a top-level window walk can
  // no longer see it — so this lands straight on the preview, with a note naming
  // the tab to close rather than a PowerPoint window that is not on the desktop.
  await treeItem(page, PPTX_SECOND).click();

  const note = pane.locator(".wb-office-note");
  await expect(note).toContainText(PPTX_FILE);
  await expect(note).toContainText("Close that tab");
  await expect(pane.locator(".wb-office-card-title")).toHaveText(DEGRADED);
  // No launch was made, so there is no docking surface in this pane at all.
  await expect(pane.locator(".wb-office-native")).toHaveCount(0);

  await closeTab(page, PPTX_SECOND);
  await closeTab(page, PPTX_FILE);
});
