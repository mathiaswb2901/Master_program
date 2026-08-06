/**
 * Journey 10 — switching workspaces from inside the app.
 *
 * The thing this proves, and the reason it is an E2E journey rather than a unit
 * test: a switch has to carry *everything* rooted in the workspace with it. The
 * file tree, the layout file and `shortcuts.md` all live in different services
 * on the server and in different capabilities in the UI, and each of them
 * holding a stale root is a bug that looks like data from the wrong project
 * rather than like a crash. Only a real switch between two real workspaces, in
 * a real window, shows all three following at once.
 *
 * Runs **last** in the suite (alphabetically after `visual.spec.ts`) and ends by
 * switching back, because there is one backend and every other journey expects
 * it to be rooted in the seeded workspace.
 */

import path from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import { openApp, treeItem, typeInEditor } from "./app";
import {
  DOCX_FILE,
  DOCX_REFUSES_CLOSE,
  E2E_WORKSPACE,
  NOTES_FILE,
  SECOND_FILE,
  SECOND_LAYOUT_NAME,
  SECOND_SHORTCUT_NAME,
  SECOND_WORKSPACE,
  SHORTCUT_NAME,
} from "./workspace";

const firstName = path.basename(E2E_WORKSPACE);
const secondName = path.basename(SECOND_WORKSPACE);

/** Exactly this string, as a regex — see `SECOND_WORKSPACE` for why every
 * matcher in this file is exact. */
const only = (text: string): RegExp =>
  new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

/** The folder name on the status chip — the label, not the button, so the
 * assertion is the name and not the name plus whatever else the chip draws. */
const chip = (page: Page): Locator => page.locator(".wb-workspace-chip-label");

/** A picker row, by its title exactly. */
const row = (page: Page, picker: Locator, title: string): Locator =>
  picker
    .locator(".wb-qb-row")
    .filter({ has: page.locator(".wb-qb-row-title", { hasText: only(title) }) });

/**
 * Open the switcher from the chip and choose a row.
 *
 * `type` is the browser-tab path fallback: there is no folder dialog outside the
 * desktop shell, so pasting a path into the picker is how a workspace that is
 * not on the recent list is opened — and it is what this suite can drive.
 */
async function switchTo(page: Page, rowTitle: string, type?: string): Promise<void> {
  await chip(page).click();
  // `exact`: the confirm modal is called "Switch workspace?", so the default
  // substring match resolves to both the moment a dirty buffer is involved.
  const picker = page.getByRole("dialog", { name: "Switch workspace", exact: true });
  await expect(picker).toBeVisible();
  if (type !== undefined) await picker.locator(".wb-qb-input").fill(type);
  await row(page, picker, rowTitle).click();
  await expect(picker).toBeHidden();
}

/** The names on the Layouts menu — this workspace's saved arrangements. */
async function savedLayouts(page: Page): Promise<string[]> {
  await page.locator(".wb-layout-chip").click();
  const menu = page.getByRole("dialog", { name: "Layouts" });
  await expect(menu).toBeVisible();
  const names = await menu.locator(".wb-menu-item").allInnerTexts();
  await page.locator(".wb-menu-backdrop").click();
  await expect(menu).toBeHidden();
  return names;
}

/** Command-palette row titles, for the `shortcuts.md` assertion. */
async function commandTitles(page: Page): Promise<string[]> {
  await page.keyboard.press("Control+Shift+P");
  const palette = page.getByRole("dialog", { name: "Quick open" });
  await expect(palette).toBeVisible();
  const titles = await palette.locator(".wb-qb-row-title").allInnerTexts();
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  return titles;
}

test.afterAll(async ({ request }) => {
  // Belt and braces: every other journey assumes the backend is rooted in the
  // seeded workspace, and a failure part-way through this one must not leave it
  // pointed somewhere else for a re-run.
  await request.post("/api/workspace/switch", { data: { path: E2E_WORKSPACE } });
});

test("switches between two workspaces, and refuses to lose unsaved work", async ({ page }) => {
  await openApp(page);

  // Where we are: the seeded workspace, its file, its shortcut.
  await expect(chip(page)).toHaveText(firstName);
  await expect(treeItem(page, NOTES_FILE)).toBeVisible();
  expect(await commandTitles(page)).toContain(SHORTCUT_NAME);

  // ---- a docked Office window is unsaved work the buffers cannot see -------
  // An `office` open file is never marked dirty — the unsaved paragraph is
  // inside Word, not inside anything this app models — so the dirty-buffer
  // check is blind to it. Without a guard of its own, the switch closed a real
  // window behind the user's back while the tree was already painting the other
  // project.
  await treeItem(page, DOCX_FILE).click();
  await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");

  await switchTo(page, `Open ${SECOND_WORKSPACE}`, SECOND_WORKSPACE);
  const docked = page.getByRole("dialog", { name: "Switch workspace?" });
  await expect(docked).toBeVisible();
  await expect(docked).toContainText(DOCX_FILE);
  await expect(docked).toContainText("open in Office");
  // Nothing is dirty yet, so there is nothing to discard — the choice is close
  // the document or stay.
  await expect(docked.getByRole("button", { name: "Close and switch" })).toBeVisible();
  await expect(docked.getByRole("button", { name: "Discard changes" })).toHaveCount(0);
  await docked.getByRole("button", { name: "Cancel" }).click();

  // Cancel really cancelled: the window is still docked, in this workspace.
  await expect(chip(page)).toHaveText(firstName);
  await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");

  // ---- the dirty-buffer refusal -------------------------------------------
  // A buffer that is not saved here cannot be saved at all once the window is
  // looking at another project, so the switch asks the same question the
  // dirty-close guard does — and Cancel really cancels.
  await treeItem(page, NOTES_FILE).click();
  await typeInEditor(page, "unsaved edit\n");
  await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(1);

  await switchTo(page, `Open ${SECOND_WORKSPACE}`, SECOND_WORKSPACE);
  const confirm = page.getByRole("dialog", { name: "Switch workspace?" });
  await expect(confirm).toBeVisible();
  await expect(confirm).toContainText(NOTES_FILE);
  await confirm.getByRole("button", { name: "Cancel" }).click();

  // Still here, still dirty: the refusal cost nothing.
  await expect(chip(page)).toHaveText(firstName);
  await expect(treeItem(page, NOTES_FILE)).toBeVisible();
  await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(1);

  // ---- and then the switch, discarding deliberately ------------------------
  await switchTo(page, `Open ${SECOND_WORKSPACE}`, SECOND_WORKSPACE);
  await expect(page.getByRole("dialog", { name: "Switch workspace?" })).toBeVisible();
  await page.getByRole("button", { name: "Discard changes" }).click();

  // The tree followed: the other project's file is here and this one's is not.
  await expect(chip(page)).toHaveText(secondName);
  await expect(treeItem(page, SECOND_FILE)).toBeVisible();
  await expect(treeItem(page, NOTES_FILE)).toHaveCount(0);
  // The editor went with it — its buffer named a path in the workspace we left.
  await expect(page.locator(".wb-editor-tab")).toHaveCount(0);
  // And so did the docked document: settled *before* the root moved, by the
  // guard, rather than closed by an unmount nobody waited for.
  await expect(page.locator(".wb-office-native")).toHaveCount(0);

  // shortcuts.md followed: this workspace's entry is in the palette, the other
  // workspace's is not.
  const titles = await commandTitles(page);
  expect(titles).toContain(SECOND_SHORTCUT_NAME);
  expect(titles).not.toContain(SHORTCUT_NAME);

  // The layout file followed: `.workbench/layouts.json` is *in* the workspace,
  // so the saved arrangements are this project's.
  expect(await savedLayouts(page)).toContain(SECOND_LAYOUT_NAME);

  // ---- and back, in one keystroke, off the recent list ---------------------
  await switchTo(page, firstName);
  await expect(chip(page)).toHaveText(firstName);
  await expect(treeItem(page, NOTES_FILE)).toBeVisible();
  await expect(treeItem(page, SECOND_FILE)).toHaveCount(0);
  expect(await commandTitles(page)).toContain(SHORTCUT_NAME);
  expect(await savedLayouts(page)).not.toContain(SECOND_LAYOUT_NAME);

  // ---- and a window that will not close stops the switch -------------------
  // The worst version of this bug is not a document closed too eagerly, it is
  // one that could not be closed at all: Office keeps the window, the user's
  // edit is still in it, and the only surface that renders that fact is this
  // panel. Reporting it *after* the root moved would mean reporting it into a
  // panel that no longer exists — so the switch does not happen.
  //
  // Last in the journey on purpose: it deliberately leaves a wedged window and
  // the bar that reports it on screen, which no later leg should have to work
  // around. `afterAll` re-roots through the API, which is not the guarded path.
  await treeItem(page, DOCX_REFUSES_CLOSE).click();
  await expect(page.locator(".wb-office-native")).toHaveAttribute("data-state", "embedded");

  await switchTo(page, `Open ${SECOND_WORKSPACE}`, SECOND_WORKSPACE);
  const wedged = page.getByRole("dialog", { name: "Switch workspace?" });
  await expect(wedged).toContainText(DOCX_REFUSES_CLOSE);
  await wedged.getByRole("button", { name: "Close and switch" }).click();

  // Refused, and said so — and we are still in the workspace that owns the
  // window, with the panel that explains it on screen. Filtered rather than
  // `.wb-toast` alone: several earlier legs of this journey leave one up.
  await expect(
    page
      .locator(".wb-toast.is-error")
      .filter({ hasText: `Staying here: ${DOCX_REFUSES_CLOSE} could not be closed.` }),
  ).toBeVisible();
  await expect(chip(page)).toHaveText(firstName);
  await expect(treeItem(page, NOTES_FILE)).toBeVisible();
  await expect(page.locator(".wb-office-bar")).toContainText("would not close");
});
