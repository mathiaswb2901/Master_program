/**
 * The notebook view: a `.ipynb` read as a document (ROADMAP M4 tail, the half
 * PR #62 deferred).
 *
 * Three journeys, and each one is a claim the server tests cannot make —
 * `server/tests/test_notebook.py` proves what parses, this proves what a person
 * sees when they click the file:
 *
 *  1. **A notebook opens as a notebook.** Clicking a `.ipynb` in the tree lands
 *     on rendered markdown, coloured code and real outputs — an image painted
 *     from its own bytes, a traceback, a stderr stream — and *not* on raw JSON
 *     in Monaco, which is exactly what item 16's exit line asks for. It also
 *     asserts the **absence** that makes this honest: there is no run control
 *     anywhere in the panel, because there is no kernel behind one.
 *  2. **Two notebooks are two notebooks** (the plural-tool requirement,
 *     CLAUDE.md product principle 4). Two panes, each bound to its own file,
 *     independent, and still themselves after a reload — because the binding is
 *     the dockview panel id and the panel ids are what `.workbench/layouts.json`
 *     holds (`ui/src/panes.ts`). Every assertion here is scoped to one pane; an
 *     unscoped locator would pass with one notebook on screen twice.
 *  3. **The two failures a user hits by accident.** A file that is gone (the
 *     tombstone, with the action that recovers it) and a file that will not
 *     parse (the degraded card, carrying the server's own reason and handing
 *     over the raw file). Neither is ever a blank pane.
 *
 * The workspace is shared by every spec (`workers: 1`), so this seeds its own
 * notebooks and removes them — and resets the layout — in `afterAll`. Names sort
 * after `notes.md` on purpose: the fake agent's scripted `Read` targets the
 * first file in the workspace by name and journey 4 asserts which one that is.
 */

import fs from "node:fs";

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { openApp, treeItem, workspaceReady } from "./app";
import { removeWorkspaceFile, workspacePath } from "./workspace";

const ANALYSIS = "sample-analysis.ipynb";
const SECOND = "sample-second.ipynb";
const BROKEN = "sample-broken.ipynb";
const SEEDED = [ANALYSIS, SECOND, BROKEN];

/** A 1x1 transparent PNG: the smallest thing that is genuinely an image, so the
 * `data:` URI path is exercised with bytes a browser really paints. */
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk" +
  "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

const MARKDOWN_HEADING = "Spot price spread";
const STREAM_TEXT = "mean spread 4.2 EUR/MWh";
const STDERR_TEXT = "FutureWarning: resample rule";
const ERROR_NAME = "ZeroDivisionError";
const SECOND_HEADING = "Battery availability";

interface Cell {
  cell_type: string;
  metadata: Record<string, never>;
  source: string;
  execution_count?: number | null;
  outputs?: unknown[];
}

const notebook = (cells: Cell[]): string =>
  JSON.stringify({
    cells,
    metadata: {
      kernelspec: { display_name: "Python 3 (ipykernel)", language: "python", name: "python3" },
      language_info: { name: "python", version: "3.11.9" },
    },
    nbformat: 4,
    nbformat_minor: 4,
  });

const ANALYSIS_SOURCE = notebook([
  { cell_type: "markdown", metadata: {}, source: `# ${MARKDOWN_HEADING}\n\nDay-ahead, hourly.` },
  {
    cell_type: "code",
    metadata: {},
    execution_count: 1,
    source: 'import pandas as pd\n\n# the spread we care about\nprint("summary")\n',
    outputs: [{ output_type: "stream", name: "stdout", text: `${STREAM_TEXT}\n` }],
  },
  {
    cell_type: "code",
    metadata: {},
    execution_count: 2,
    source: "spread.plot()\n",
    outputs: [
      {
        output_type: "display_data",
        metadata: {},
        data: { "image/png": PNG_BASE64, "text/plain": "<Figure size 640x480 with 1 Axes>" },
      },
      { output_type: "stream", name: "stderr", text: `${STDERR_TEXT}\n` },
    ],
  },
  {
    cell_type: "code",
    metadata: {},
    execution_count: null,
    source: "1 / 0\n",
    outputs: [
      {
        output_type: "error",
        ename: ERROR_NAME,
        evalue: "division by zero",
        traceback: ["[0;31mTraceback[0m", "[0;32m----> 1[0m 1/0"],
      },
    ],
  },
]);

const SECOND_SOURCE = notebook([
  { cell_type: "markdown", metadata: {}, source: `# ${SECOND_HEADING}\n\nPer asset, per hour.` },
  {
    cell_type: "code",
    metadata: {},
    execution_count: 9,
    source: "availability = fleet.groupby('asset').mean()\n",
    outputs: [{ output_type: "stream", name: "stdout", text: "12 assets\n" }],
  },
]);

/** Not JSON at all, and cut off mid-record — a real half-written file. */
const BROKEN_SOURCE = '{"cells": [{"cell_type": "code", "source": "x = ';

function seed(): void {
  fs.writeFileSync(workspacePath(ANALYSIS), ANALYSIS_SOURCE, "utf-8");
  fs.writeFileSync(workspacePath(SECOND), SECOND_SOURCE, "utf-8");
  fs.writeFileSync(workspacePath(BROKEN), BROKEN_SOURCE, "utf-8");
}

test.beforeAll(seed);

test.afterAll(async () => {
  for (const name of SEEDED) removeWorkspaceFile(name);
  // This journey persists panes, and six specs run after it — a notebook pane
  // left behind would make `panes.spec.ts` count one panel too many. Same
  // reset, and the same reason, as that journey's own `afterAll`.
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

/** The notebook as the *editor area* renders it — the document-view host, which
 * is the class only that path puts on screen. Scoped, so an assertion never
 * matches a notebook pane that happens to be open beside it. */
const documentHost = (page: Page): Locator => page.locator(".wb-notebook-host");

/** The dockview group whose tab carries this title — how one notebook pane is
 * addressed without an unscoped locator on a pane-internal class. */
const paneByTitle = (page: Page, title: string): Locator =>
  page.locator(".dv-groupview", { has: page.locator(".wb-panel-tab", { hasText: title }) });

/** Run a QuickBar command by its row title. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/** Open one of the workspace's notebooks into a pane of its own. */
async function openNotebook(page: Page, name: string): Promise<void> {
  await runCommand(page, "Open notebook…");
  const picker = page.getByRole("dialog", { name: "Open notebook" });
  await picker.locator(".wb-qb-row", { hasText: name }).first().click();
  await expect(picker).toBeHidden();
  await expect(paneByTitle(page, name).locator(".wb-nb-name")).toHaveText(name);
}

/** Pane ids as they are on disk — the strings the next launch will trust. */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

test("a .ipynb opens as a notebook document, not as raw JSON", async ({ page }) => {
  await openApp(page);

  await test.step("clicking it in the tree lands on the notebook view", async () => {
    await treeItem(page, ANALYSIS).click();
    // It opens as a document tab in the editor area — the same place a `.docx`
    // opens — because `.ipynb` is its own `OpenFile` kind now.
    await expect(page.locator(".wb-editor-tab", { hasText: ANALYSIS })).toBeVisible();
    await expect(documentHost(page).locator(".wb-nb-name")).toHaveText(ANALYSIS);
    // The thing item 16's exit line rules out: no Monaco buffer for this file.
    // Scoped to the editor frame the notebook is in, so a leftover file pane
    // from another journey cannot make this pass or fail for the wrong reason.
    await expect(
      page
        .locator(".wb-editor-body", { has: page.locator(".wb-notebook-host") })
        .locator(".monaco-editor"),
    ).toHaveCount(0);
  });

  const notebookPanel = documentHost(page);

  await test.step("markdown is rendered, not shown as source", async () => {
    await expect(
      notebookPanel.locator('.wb-nb-cell[data-cell-type="markdown"] h1'),
    ).toHaveText(MARKDOWN_HEADING);
  });

  await test.step("code cells carry their execution count and are coloured", async () => {
    const code = notebookPanel.locator('.wb-nb-cell[data-cell-type="code"]');
    await expect(code).toHaveCount(3);
    await expect(code.nth(0).locator(".wb-nb-count")).toHaveText("[1]");
    // The cell that was never run says so, and does not borrow a number.
    await expect(code.nth(2).locator(".wb-nb-count")).toHaveText("[ ]");
    // Highlighting is real: `import` is a keyword span and the comment is its
    // own, from the pure token pass in `ui/src/notebook.ts`.
    await expect(code.nth(0).locator(".wb-nb-t-keyword").first()).toHaveText("import");
    await expect(code.nth(0).locator(".wb-nb-t-comment").first()).toContainText(
      "the spread we care about",
    );
  });

  await test.step("outputs render as what they are", async () => {
    const code = notebookPanel.locator('.wb-nb-cell[data-cell-type="code"]');
    await expect(code.nth(0).locator(".wb-nb-out-text")).toContainText(STREAM_TEXT);

    // An image is painted from its own bytes: a `data:` URI, never a URL the
    // network could be asked for. Asserted as *rendered*, not merely present —
    // a broken image still has a src.
    const image = code.nth(1).locator(".wb-nb-out.is-image img");
    await expect(image).toHaveAttribute("src", /^data:image\/png;base64,/);
    expect(await image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);

    // stderr is told apart from stdout, and the figure's useless `<Figure size
    // 640x480>` text repr never won over the image beside it.
    await expect(code.nth(1).locator(".wb-nb-out.is-stderr")).toContainText(STDERR_TEXT);
    await expect(notebookPanel).not.toContainText("Figure size 640x480");

    // A traceback keeps its name and loses its ANSI escapes.
    await expect(code.nth(2).locator(".wb-nb-out.is-error")).toContainText(ERROR_NAME);
    await expect(code.nth(2).locator(".wb-nb-out.is-error")).not.toContainText("[0;31m");
  });

  await test.step("there is no run affordance, because there is no kernel", async () => {
    // The absence is the feature (ROADMAP M4). A button that cannot run is a
    // lie about what the app does, so this asserts none exists — by role, so a
    // link or a menu item styled as one would fail too.
    await expect(notebookPanel.getByRole("button", { name: /run|execute/i })).toHaveCount(0);
    await expect(notebookPanel.getByRole("link", { name: /run|execute/i })).toHaveCount(0);
    await expect(notebookPanel.locator(".wb-nb-readonly")).toHaveText("Read-only");
  });

  await test.step("the kernel it was written against is named", async () => {
    await expect(notebookPanel.locator(".wb-nb-meta")).toContainText("Python 3 (ipykernel)");
    await expect(notebookPanel.locator(".wb-nb-meta")).toContainText("4 cells");
  });
});

test("two notebook panes are two notebooks, through a reload", async ({ page }) => {
  await openApp(page);

  await openNotebook(page, ANALYSIS);
  await openNotebook(page, SECOND);

  const analysis = paneByTitle(page, ANALYSIS);
  const second = paneByTitle(page, SECOND);

  await test.step("each pane shows its own notebook", async () => {
    // Scoped to the pane, always. An unscoped `.wb-nb h1` would pass with the
    // same notebook on screen twice, which is the bug this test exists for.
    await expect(analysis.locator('.wb-nb-cell[data-cell-type="markdown"] h1')).toHaveText(
      MARKDOWN_HEADING,
    );
    await expect(second.locator('.wb-nb-cell[data-cell-type="markdown"] h1')).toHaveText(
      SECOND_HEADING,
    );
    // …and neither has borrowed the other's cells.
    await expect(analysis.locator(".wb-nb-scroll")).not.toContainText(SECOND_HEADING);
    await expect(second.locator(".wb-nb-scroll")).not.toContainText(MARKDOWN_HEADING);
    await expect(analysis.locator('.wb-nb-cell[data-cell-type="code"]')).toHaveCount(3);
    await expect(second.locator('.wb-nb-cell[data-cell-type="code"]')).toHaveCount(1);
    await expect(second.locator(".wb-nb-count")).toHaveText("[9]");
  });

  await test.step("the binding that persists is the pane id", async () => {
    await expect
      .poll(() => persistedPaneIds(page), { timeout: 10_000 })
      .toEqual(expect.arrayContaining([`notebook#${ANALYSIS}`, `notebook#${SECOND}`]));
  });

  await test.step("a reload brings both back, still pointed at their own files", async () => {
    await page.reload();
    await workspaceReady(page);
    await expect(paneByTitle(page, ANALYSIS).locator(".wb-nb-name")).toHaveText(ANALYSIS);
    await expect(paneByTitle(page, SECOND).locator(".wb-nb-name")).toHaveText(SECOND);
    await expect(
      paneByTitle(page, ANALYSIS).locator('.wb-nb-cell[data-cell-type="markdown"] h1'),
    ).toHaveText(MARKDOWN_HEADING);
    await expect(
      paneByTitle(page, SECOND).locator('.wb-nb-cell[data-cell-type="markdown"] h1'),
    ).toHaveText(SECOND_HEADING);
  });

  await test.step("what is on disk is what is shown", async () => {
    // Disk is the single source of truth (CLAUDE.md): a notebook is the output
    // of something that ran elsewhere, so "it changed while you were reading
    // it" is the normal case. The pane follows without being asked.
    fs.writeFileSync(
      workspacePath(SECOND),
      SECOND_SOURCE.replace(SECOND_HEADING, "Rewritten by something else"),
      "utf-8",
    );
    await expect(
      paneByTitle(page, SECOND).locator('.wb-nb-cell[data-cell-type="markdown"] h1'),
    ).toHaveText("Rewritten by something else");
    // The other pane is untouched — one notebook changing is one pane changing.
    await expect(
      paneByTitle(page, ANALYSIS).locator('.wb-nb-cell[data-cell-type="markdown"] h1'),
    ).toHaveText(MARKDOWN_HEADING);
  });
});

test("a notebook that is gone, and one that will not parse", async ({ page }) => {
  await openApp(page);

  await test.step("a file that disappears becomes a named tombstone, not a blank pane", async () => {
    await openNotebook(page, ANALYSIS);
    const pane = paneByTitle(page, ANALYSIS);
    await expect(pane.locator('.wb-nb-cell[data-cell-type="markdown"] h1')).toBeVisible();

    // The notebook was read moments ago, so the server may still have it open —
    // a plain unlink is EBUSY on Windows inside that window (`removeWorkspaceFile`).
    removeWorkspaceFile(ANALYSIS);

    // Named — the pane says *which* notebook is missing and where it was —
    // and it offers the one action that recovers it. A file does come back: a
    // branch switch, a checkout, a sync that finished.
    const tombstone = pane.locator(".wb-nb-state.is-empty");
    await expect(tombstone).toContainText(`${ANALYSIS} is not in this workspace`);
    await expect(tombstone.getByRole("button", { name: "Reopen" })).toBeVisible();

    seed(); // it comes back
    await tombstone.getByRole("button", { name: "Reopen" }).click();
    await expect(pane.locator('.wb-nb-cell[data-cell-type="markdown"] h1')).toHaveText(
      MARKDOWN_HEADING,
    );
  });

  await test.step("a corrupt notebook is a degraded card that says what failed", async () => {
    await treeItem(page, BROKEN).click();
    const card = documentHost(page).locator(".wb-nb-state.is-failed");
    await expect(card).toContainText(`Cannot read ${BROKEN}`);
    // The server's own sentence, rendered rather than re-worded.
    await expect(card).toContainText("not a readable notebook");
  });

  await test.step("…and hands over the raw file, which is the only way left to see it", async () => {
    // The escape hatch matters *because* `.ipynb` no longer opens in Monaco:
    // without it, a half-written notebook would be unreachable from the app.
    await documentHost(page)
      .locator(".wb-nb-state.is-failed")
      .getByRole("button", { name: "Open as JSON" })
      .click();
    await expect(page.locator(".wb-editor-tab.is-active", { hasText: BROKEN })).toBeVisible();
    await expect(page.locator(".wb-editor-body .monaco-editor").first()).toBeVisible();
    await expect(page.locator(".wb-editor-body")).toContainText('"cells"');
    await expect(documentHost(page)).toHaveCount(0);
  });
});
