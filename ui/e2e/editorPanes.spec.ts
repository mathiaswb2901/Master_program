/**
 * Journey 13 — two editor panes on one file.
 *
 * The product principle this journey exists for, verbatim: *a pane is a view
 * onto a resource it does not own*. For the editor the resource is the **Monaco
 * text model** — the buffer, its undo stack and its markers — and the views are
 * the tab strip's editor and every `editors#<path>` pane. Two views onto one
 * file is the oldest reason anyone splits a window, so it is the baseline, not
 * a feature request.
 *
 * The bug this reproduces: `@monaco-editor/react` disposes the model it is
 * showing when its `<Editor>` unmounts, so closing *either* view took the model
 * out from under the other one — and Monaco's own reaction to that is brutal.
 * Every editor attached to a model registers `model.onWillDispose(() =>
 * this.setModel(null))` (`codeEditorWidget.js`), and detaching a model removes
 * the view's DOM node, so the *surviving* pane's buffer disappears outright:
 * measured on master, closing the split pane left the tab strip with its tab,
 * its filename and its unsaved-changes dot, and no editor under them at all.
 *
 * Asserts, in one continuous session:
 *  - a file opened in the tab strip can be split into a pane of its own, and
 *    both views paint the same file;
 *  - the arrangement survives a **reload** — the pane names the file in
 *    `.workbench/layouts.json` and reopens it, rather than coming back empty;
 *  - the two views share one buffer — a keystroke in one appears in the other,
 *    and one dirty mark covers both, because they are one model rather than two
 *    copies that could diverge;
 *  - the two views scroll **independently**, because scroll and cursor are the
 *    *view's* state and not the model's;
 *  - closing one view leaves the other **alive**: it takes typing, goes dirty,
 *    and Ctrl+S writes those bytes to real disk;
 *  - an external on-disk edit still reaches the surviving view (watcher ->
 *    `/ws/events` -> `setModelContent` on the registry-owned model);
 *  - and the model really does die with the *file*: closing the last tab and
 *    reopening it comes back from disk, not from a stale buffer.
 *
 * It resets the arrangement and removes its fixture on the way out — the suite
 * shares one workspace and one persisted window.
 */

import fs from "node:fs";

import { expect, test, type Locator, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { launchSettled, openApp, treeItem, workspaceReady } from "./app";
import { readWorkspaceFile, workspacePath, writeWorkspaceFile } from "./workspace";

/** A file of its own, long enough that the two views can be scrolled apart. */
const FILE = "src/pane-shared.py";
const NAME = "pane-shared.py";
const LINES = 200;
/** Deliberately nothing another journey searches for. */
const BODY = Array.from({ length: LINES }, (_, i) => `ROW_${String(i).padStart(3, "0")} = ${i}`)
  .join("\n")
  .concat("\n");
const TYPED = "SHARED_MODEL_MARKER = 1\n";
const EXTERNAL = "# rewritten on disk\n";

/**
 * The pane whose tab reads `title` — the scope every assertion below is made
 * in.
 *
 * Nothing in this journey may use a bare `page.locator(".monaco-editor")`: with
 * two editors mounted that resolves to whichever dockview rendered first, so an
 * app where closing one pane broke the other would still pass. Scoping is what
 * makes "the survivor" a statement about a *particular* view.
 */
function pane(page: Page, title: string): Locator {
  return page.locator(".dv-groupview", {
    has: page.locator(".wb-panel-tab", { hasText: title }),
  });
}

const editorIn = (host: Locator): Locator => host.locator(".monaco-editor").first();

/** Put the caret in a pane's editor and type — exactly as a user would. */
async function typeIn(page: Page, host: Locator, text: string): Promise<void> {
  await editorIn(host).click();
  await page.keyboard.press("Control+Home");
  await page.keyboard.type(text);
}

/**
 * The topmost line number this view is showing.
 *
 * Monaco scrolls by translating its own layers rather than by moving a
 * scrollbar, so `scrollTop` says nothing here. The gutter does: the smallest
 * line number it has rendered is where the view is looking, which is also what
 * the user sees.
 */
async function topLine(host: Locator): Promise<number> {
  const numbers = await host.locator(".margin-view-overlays .line-numbers").allTextContents();
  const parsed = numbers.map((text) => Number(text.trim())).filter((n) => Number.isFinite(n) && n > 0);
  return parsed.length === 0 ? 0 : Math.min(...parsed);
}

test.afterAll(() => {
  fs.rmSync(workspacePath(FILE), { force: true });
});

test("two panes on one file: one model, two views, and closing one keeps the other alive", async ({
  page,
}) => {
  writeWorkspaceFile(FILE, BODY);
  await openApp(page);

  await test.step("open the file in the tab strip", async () => {
    await treeItem(page, "src").click();
    await treeItem(page, NAME).click();
    await expect(page.locator(".wb-editor-tab").filter({ hasText: NAME })).toBeVisible();
    await expect(editorIn(pane(page, "Editor"))).toBeVisible();
  });

  await test.step("split it into a pane of its own", async () => {
    await page.locator(".wb-panel-tab", { hasText: "Editor" }).first().click();
    await page.keyboard.press("Alt+S");
    const dialog = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(dialog).toBeVisible();
    await dialog.locator(".wb-qb-row", { hasText: NAME }).first().click();
    await expect(dialog).toBeHidden();
    // Two views of the same file, both painting it.
    await expect(editorIn(pane(page, NAME))).toBeVisible();
    await expect(editorIn(pane(page, "Editor"))).toContainText("ROW_000");
    await expect(editorIn(pane(page, NAME))).toContainText("ROW_000");
  });

  await test.step("both panes come back from a reload, still on that file", async () => {
    // The round trip every plural tool owes (CLAUDE.md): a layout persists, a
    // resource does not, so a restored pane has to reopen the file it names
    // rather than come back as an empty editor.
    await expect
      .poll(
        async () => {
          const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
          const current = response.state.current as { panels?: Record<string, unknown> } | null;
          return Object.keys(current?.panels ?? {});
        },
        { timeout: 10_000 },
      )
      .toContain(`editors#${FILE}`);

    await page.reload();
    await workspaceReady(page);
    await launchSettled(page);

    await expect(editorIn(pane(page, NAME))).toContainText("ROW_000");
    await expect(editorIn(pane(page, "Editor"))).toContainText("ROW_000");
  });

  await test.step("they share one buffer — same truth, one dirty mark", async () => {
    await typeIn(page, pane(page, NAME), TYPED);
    // The keystroke landed in the other view too. Same model, so there is no
    // second copy of this file that could be saved over the first.
    await expect(editorIn(pane(page, "Editor"))).toContainText("SHARED_MODEL_MARKER");
    await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(1);
  });

  await test.step("…and they scroll independently, because that is the view's own state", async () => {
    const before = await topLine(pane(page, "Editor"));
    // Ctrl+End rather than a wheel gesture: a wheel delta is translated by the
    // browser before Monaco ever sees it, and the two lines it moved here say
    // nothing about whether the *other* view followed.
    await editorIn(pane(page, NAME)).click();
    await page.keyboard.press("Control+End");
    await expect.poll(() => topLine(pane(page, NAME))).toBeGreaterThan(before + 100);
    // The strip's view did not move with it.
    expect(await topLine(pane(page, "Editor"))).toBe(before);
  });

  await test.step("a closed pane reopens where it was looking, not at line 1", async () => {
    // The other half of "view state belongs to the pane": it has to outlive the
    // pane, or the promise is only about two panes that happen to be on screen
    // together. This is the path that reads it back after a real unmount.
    //
    // Worth stating why the unmount is where it is read, because the ordering is
    // subtle and easy to get backwards. `<Editor>` is a *child* of `CodeEditor`,
    // and its own cleanup disposes the editor widget unconditionally — even with
    // `keepCurrentModel`, which only spares the *model*
    // (`@monaco-editor/react/dist/index.mjs`: `keepCurrentModel ? … :
    // editor.getModel()?.dispose(), editor.dispose()`). A widget dispose nulls
    // `_modelData`, so a `getModel()` after it returns null. What makes reading
    // at unmount correct anyway is that React runs deletion cleanups **parent
    // before child** — the opposite of mount order, and stated verbatim in its
    // source (`commitPassiveUnmountEffectsInsideOfDeletedTree_begin`: "Deletion
    // effects fire in parent -> child order"). So `CodeEditor`'s cleanup reads a
    // live editor, and the child disposes it afterwards. If that ever inverts,
    // the `getModel()` guard turns this into a silent no-op rather than a throw
    // — and *this* step is what fails, by name.
    const remembered = await topLine(pane(page, NAME));
    expect(remembered, "the split pane is scrolled well away from the top").toBeGreaterThan(100);

    await page.locator(".wb-panel-tab", { hasText: NAME }).first().click();
    await page.keyboard.press("Alt+X");
    await expect(page.locator(".wb-panel-tab", { hasText: NAME })).toHaveCount(0);

    // Reopened the same way it was made, which gives it the same pane id
    // (`editors#<path>`) — that id *is* the key the view state is filed under.
    await page.locator(".wb-panel-tab", { hasText: "Editor" }).first().click();
    await page.keyboard.press("Alt+S");
    const dialog = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(dialog).toBeVisible();
    await dialog.locator(".wb-qb-row", { hasText: NAME }).first().click();
    await expect(dialog).toBeHidden();
    await expect(editorIn(pane(page, NAME))).toBeVisible();

    // Unfixed, view state is lost with the pane and this comes back at line 1.
    await expect
      .poll(() => topLine(pane(page, NAME)), { timeout: 10_000 })
      .toBeGreaterThan(100);
    expect(Math.abs((await topLine(pane(page, NAME))) - remembered)).toBeLessThanOrEqual(2);
  });

  await test.step("closing one view leaves the other alive", async () => {
    await page.locator(".wb-panel-tab", { hasText: NAME }).first().click();
    await page.keyboard.press("Alt+X");
    await expect(page.locator(".wb-panel-tab", { hasText: NAME })).toHaveCount(0);

    // The whole point. Unfixed, the model was disposed with the pane and this
    // editor is a painted corpse: the keystrokes throw inside Monaco, the text
    // never arrives and the tab never goes dirty.
    const survivor = pane(page, "Editor");
    await expect(editorIn(survivor)).toBeVisible();
    await typeIn(page, survivor, "STILL_ALIVE = 1\n");
    await expect(editorIn(survivor)).toContainText("STILL_ALIVE");
    await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(1);
  });

  await test.step("and Ctrl+S in it writes real bytes", async () => {
    await page.keyboard.press("Control+s");
    await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(0);
    await expect.poll(() => readWorkspaceFile(FILE)).toContain("STILL_ALIVE = 1");
  });

  await test.step("an external edit still reaches the surviving view", async () => {
    writeWorkspaceFile(FILE, EXTERNAL);
    await expect(editorIn(pane(page, "Editor"))).toContainText("rewritten on disk");
    await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(0);
  });

  await test.step("the model dies with the file, not with a pane", async () => {
    // Closing the last tab is the *explicit* end of the model's life (the store
    // retires it). Reopening therefore comes back from disk.
    const tab = page.locator(".wb-editor-tab").filter({ hasText: NAME });
    await tab.hover();
    await tab.getByRole("button", { name: `Close ${NAME}` }).click();
    await expect(page.locator(".wb-editor-tab").filter({ hasText: NAME })).toHaveCount(0);

    writeWorkspaceFile(FILE, "# reopened from disk\n");
    // The reload above collapsed the tree — which folders are open is window
    // state, and this journey deliberately reloads in the middle of itself.
    await treeItem(page, "src").click();
    await expect(treeItem(page, NAME)).toBeVisible();
    await treeItem(page, NAME).click();
    await expect(editorIn(pane(page, "Editor"))).toContainText("reopened from disk");
  });

  await test.step("leave the window as the next journey expects it", async () => {
    await page.request.put("/api/layouts", {
      data: { current: null, current_name: null, saved: [] },
    });
  });
});
