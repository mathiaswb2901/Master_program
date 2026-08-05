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
 *    actually saves and actually closes;
 *  - and the tree answers to the keyboard alone, which is not decoration: the
 *    tree is virtualised, so "Tab through every row" is gone and the arrow-key
 *    model is now the only way through it without a mouse.
 */

import { expect, test } from "@playwright/test";

import { editor, openApp, treeItem, treeMenu, typeInEditor } from "./app";
import { readWorkspaceFile, writeWorkspaceFile } from "./workspace";

const FILE = "src/bid.py";
/** Cache Directory Tagging Specification — https://bford.info/cachedir/ */
const CACHEDIR_TAG = "Signature: 8a477f597d28d172789f06886806bc55\n";
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

  await test.step("a folder that appears on disk appears in the tree", async () => {
    // The tree updates from the `file_changed` event itself — no refetch of the
    // workspace — so a directory nobody has a row for is the case that would
    // silently go missing. `git checkout`, a build, an agent: all of them make
    // a folder and put files in it.
    writeWorkspaceFile("build-live/artifact.o", "cached\n");
    await expect(treeItem(page, "build-live")).toBeVisible();
    await treeItem(page, "build-live").click();
    await expect(treeItem(page, "artifact.o")).toBeVisible();
  });

  await test.step("…and takes itself back out when it turns out to be a build cache", async () => {
    // The one change no file event can describe: the events from inside a
    // tagged directory are precisely the ones now being suppressed. The server
    // says so with `tree_invalidated`, and the client re-lists what it shows —
    // the convergence rule that lets the tree be incremental at all.
    writeWorkspaceFile("build-live/CACHEDIR.TAG", CACHEDIR_TAG);
    await expect(treeItem(page, "build-live")).toHaveCount(0);
    await expect(treeItem(page, "artifact.o")).toHaveCount(0);
    // And the rest of the tree is still there — a reconcile, not a reset.
    await expect(treeItem(page, "src")).toBeVisible();
  });

  await test.step("the tree is fully operable from the keyboard", async () => {
    /** The row that currently holds focus, by its accessible name. */
    const focused = (): Promise<string> =>
      page.evaluate(() => document.activeElement?.textContent?.trim() ?? "");

    await page.getByRole("treeitem", { name: "src", exact: true }).focus();
    // Left closes the folder the create step left open; the child goes with it.
    await page.keyboard.press("ArrowLeft");
    await expect(page.getByRole("treeitem", { name: "model.py", exact: true })).toHaveCount(0);
    // Right opens it again, Down steps into it.
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("treeitem", { name: "model.py", exact: true })).toBeVisible();
    // Two children now, in the server's own order: bid.py, then model.py.
    await page.keyboard.press("ArrowDown");
    expect(await focused()).toBe("bid.py");
    await page.keyboard.press("ArrowDown");
    expect(await focused()).toBe("model.py");

    // Enter is the row's own <button> doing its job — nothing intercepts it.
    await page.keyboard.press("Enter");
    await expect(page.locator(".wb-editor-tab").filter({ hasText: "model.py" })).toBeVisible();

    // Left from a file steps out to its parent; Home and End are the ends.
    await page.keyboard.press("ArrowLeft");
    expect(await focused()).toBe("src");

    /** Where the focused row sits in the whole tree, as it tells a screen
     * reader. Asserted instead of a filename: the DOM no longer holds every
     * row, so `aria-posinset`/`setsize` are the only statement of "the end"
     * that stays true — and they are what a screen-reader user is told. */
    const position = (): Promise<string> =>
      page.evaluate(() => {
        const row = document.activeElement;
        return `${row?.getAttribute("aria-posinset")}/${row?.getAttribute("aria-setsize")}`;
      });

    await page.keyboard.press("End");
    const [at, size] = (await position()).split("/");
    expect(at).toBe(size);
    await page.keyboard.press("Home");
    expect(await position()).toBe(`1/${size}`);
    expect(await focused()).toBe(".workbench");
  });
});
