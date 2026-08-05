/**
 * Journey 1 — files: the full disk-is-the-source-of-truth loop.
 *
 * Asserts, in one continuous session:
 *  - creating a file from the tree context menu opens it in a Monaco tab;
 *  - typing marks the tab dirty and Ctrl+S writes the bytes to real disk;
 *  - an edit made on disk *by the test* reaches a clean buffer on its own
 *    (watcher -> /ws/events -> reload), visible in the editor;
 *  - the same external edit against a dirty buffer raises the conflict bar
 *    instead of silently overwriting either side;
 *  - closing a dirty tab opens the DirtyCloseModal, and "Save and close"
 *    actually saves and actually closes.
 */

import { expect, test } from "@playwright/test";

import { editor, openApp, treeMenu, typeInEditor } from "./app";
import { readWorkspaceFile, writeWorkspaceFile } from "./workspace";

const FILE = "src/bid.py";
const TYPED = "PRICE = 42";
const EXTERNAL = "# edited on disk\n";
const EXTERNAL_AGAIN = "# edited on disk again\n";

test("create, save, watcher reload, conflict and dirty close", async ({ page }) => {
  await openApp(page);

  await test.step("create a file from the tree context menu", async () => {
    await treeMenu(page, "src", "New file…");
    const nameInput = page.getByRole("textbox", { name: "New file name" });
    await nameInput.fill("bid.py");
    await nameInput.press("Enter");
    await expect(page.locator(".wb-editor-tab").filter({ hasText: "bid.py" })).toBeVisible();
    await expect(editor(page)).toBeVisible();
  });

  await test.step("type and save with Ctrl+S", async () => {
    await typeInEditor(page, TYPED);
    await expect(page.locator(".wb-editor-tab.is-dirty")).toBeVisible();
    await page.keyboard.press("Control+s");
    await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(0);
    // The bytes, not just the badge: disk is the source of truth.
    await expect.poll(() => readWorkspaceFile(FILE)).toContain(TYPED);
  });

  await test.step("an edit on disk reloads a clean buffer", async () => {
    writeWorkspaceFile(FILE, EXTERNAL);
    await expect(editor(page)).toContainText("edited on disk");
    await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(0);
  });

  await test.step("an edit on disk against a dirty buffer raises the conflict bar", async () => {
    await typeInEditor(page, "LOCAL = 1\n");
    await expect(page.locator(".wb-editor-tab.is-dirty")).toBeVisible();
    writeWorkspaceFile(FILE, EXTERNAL_AGAIN);
    await expect(page.locator(".wb-conflict-bar")).toContainText("unsaved edits");
    // Neither side was thrown away: the buffer still holds what was typed.
    await expect(editor(page)).toContainText("LOCAL = 1");
  });

  await test.step("keep mine clears the conflict and keeps the buffer dirty", async () => {
    await page.getByRole("button", { name: "Keep mine" }).click();
    await expect(page.locator(".wb-conflict-bar")).toHaveCount(0);
    await expect(page.locator(".wb-editor-tab.is-dirty")).toBeVisible();
  });

  await test.step("closing a dirty tab asks, and Save and close saves", async () => {
    // A dirty tab shows its unsaved dot and reveals the × on hover (editor.css).
    const tab = page.locator(".wb-editor-tab").filter({ hasText: "bid.py" });
    await tab.hover();
    await tab.getByRole("button", { name: "Close bid.py" }).click();
    const modal = page.getByRole("dialog", { name: "Close bid.py?" });
    await expect(modal).toBeVisible();
    await modal.getByRole("button", { name: "Save and close" }).click();
    await expect(modal).toHaveCount(0);
    await expect(page.locator(".wb-editor-tab").filter({ hasText: "bid.py" })).toHaveCount(0);
    await expect.poll(() => readWorkspaceFile(FILE)).toContain("LOCAL = 1");
  });
});
