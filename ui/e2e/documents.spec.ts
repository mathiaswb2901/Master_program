/**
 * New documents (M5 item 16): the tree creates valid *documents*, not empty files.
 *
 * The formats' validity is proven exhaustively server-side
 * (`server/tests/test_documents.py` opens each OOXML zip and parses the
 * notebook); this journey is the browser wiring — the affordance a user actually
 * reaches for:
 *  - the QuickBar's "New document…" command, its two-step pick (kind by name,
 *    then a name), creating a Markdown file that opens in Monaco;
 *  - the file tree's own "New document…" context-menu entry, creating a real
 *    PowerPoint that lands on disk as a valid OOXML zip and opens in a doc tab —
 *    not the zero-byte file Word would refuse.
 *
 * The suite runs every spec against one shared workspace (`workers: 1`), so this
 * cleans up the two files it creates in `afterAll` — a later spec navigating the
 * tree must see the workspace it seeded, not this one's leftovers.
 */

import fs from "node:fs";

import { expect, test } from "@playwright/test";

import { editor, openApp, treeItem, treeMenu } from "./app";
import { readWorkspaceFile, workspacePath } from "./workspace";

const MD_FILE = "newdoc-notes.md";
const PPTX_FILE = "src/newdoc-deck.pptx";

test.afterAll(() => {
  for (const relative of [MD_FILE, PPTX_FILE]) {
    fs.rmSync(workspacePath(relative), { force: true });
  }
});

test("New document via the QuickBar: pick a kind by name, name it, open it", async ({ page }) => {
  await openApp(page);

  await test.step("run the New document command", async () => {
    await page.keyboard.press("Control+Shift+P");
    const palette = page.getByRole("dialog", { name: "Quick open" });
    await palette.locator(".wb-qb-input").fill(">New document");
    await palette.locator(".wb-qb-row", { hasText: "New document" }).first().click();
  });

  await test.step("choose Markdown by name", async () => {
    // The command reopens the palette as a kind picker (its own dialog label).
    const kinds = page.getByRole("dialog", { name: "New document" });
    await expect(kinds.locator(".wb-qb-row").first()).toBeVisible();
    await kinds.locator(".wb-qb-row", { hasText: "Markdown" }).first().click();
  });

  await test.step("name it, and it opens in Monaco as a real empty file", async () => {
    const namer = page.getByRole("dialog", { name: "New Markdown document" });
    await namer.locator(".wb-qb-input").fill("newdoc-notes");
    await namer.locator(".wb-qb-row", { hasText: `Create ${MD_FILE}` }).first().click();

    await expect(page.locator(".wb-editor-tab").filter({ hasText: MD_FILE })).toBeVisible();
    await expect(editor(page)).toBeVisible();
    await expect(treeItem(page, MD_FILE)).toBeVisible();
    // A real file on disk, empty — the trivial kinds carry no template.
    expect(readWorkspaceFile(MD_FILE)).toBe("");
  });
});

test("New document from the tree menu writes a valid OOXML PowerPoint", async ({ page }) => {
  await openApp(page);

  await treeMenu(page, "src", "New document…");
  const kinds = page.getByRole("dialog", { name: "New document" });
  await kinds.locator(".wb-qb-row", { hasText: "PowerPoint" }).first().click();

  const namer = page.getByRole("dialog", { name: "New PowerPoint document" });
  await namer.locator(".wb-qb-input").fill("newdoc-deck");
  await namer.locator(".wb-qb-row", { hasText: `Create ${PPTX_FILE}` }).first().click();

  // It opens in a document tab (the Office host surface — degraded to OnlyOffice
  // or the honest card when no server is configured; the tab is the proof it
  // opened, not raw JSON or a broken panel).
  await expect(page.locator(".wb-editor-tab").filter({ hasText: "newdoc-deck.pptx" })).toBeVisible();

  // The whole point of item 16: on disk it is a valid OOXML zip, not a zero-byte
  // file. `PK` is the zip local-file-header magic; a few KB is a real deck.
  await expect.poll(() => fs.existsSync(workspacePath(PPTX_FILE))).toBe(true);
  const bytes = fs.readFileSync(workspacePath(PPTX_FILE));
  expect(bytes.length).toBeGreaterThan(1000);
  expect(bytes.subarray(0, 2).toString("latin1")).toBe("PK");
});
