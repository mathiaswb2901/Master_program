/**
 * The "New document…" flow's pure parts and its two-step pick.
 *
 * The store is stubbed (the module reaches it at call time), and fake timers
 * flush the one-turn defer that keeps a step from being wiped by the QuickBar's
 * own `close()`. The end-to-end create -> open is proven server-side
 * (`test_documents.py`) and by the manual run recorded in the PR.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { QuickPick, QuickPickRow } from "./store";

const picks: QuickPick[] = [];
const openQuickPick = vi.fn((pick: QuickPick) => {
  picks.push(pick);
});
const createDocument = vi.fn();

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, {
    getState: () => ({ openQuickPick, createDocument }),
  }),
}));

const { DOCUMENT_KINDS, documentPath, newDocumentFlow } = await import("./documents");

beforeEach(() => {
  picks.length = 0;
  openQuickPick.mockClear();
  createDocument.mockClear();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("documentPath", () => {
  it("appends the extension for a bare name", () => {
    expect(documentPath("", "budget", "xlsx")).toBe("budget.xlsx");
    expect(documentPath("reports", "q1", "docx")).toBe("reports/q1.docx");
  });

  it("does not double an extension the user already typed (any case)", () => {
    expect(documentPath("", "budget.xlsx", "xlsx")).toBe("budget.xlsx");
    expect(documentPath("", "Budget.XLSX", "xlsx")).toBe("Budget.XLSX");
  });

  it("treats a different extension as part of the name", () => {
    expect(documentPath("", "notes.txt", "md")).toBe("notes.txt.md");
  });

  it("trims surrounding whitespace", () => {
    expect(documentPath("", "  memo  ", "md")).toBe("memo.md");
  });
});

describe("the kind catalogue", () => {
  it("offers every kind by name", () => {
    expect(DOCUMENT_KINDS.map((info) => info.kind)).toEqual([
      "docx",
      "xlsx",
      "pptx",
      "py",
      "txt",
      "md",
      "ipynb",
    ]);
    expect(DOCUMENT_KINDS.map((info) => info.name)).toEqual([
      "Word",
      "Excel",
      "PowerPoint",
      "Python",
      "Text",
      "Markdown",
      "Notebook",
    ]);
  });
});

describe("newDocumentFlow", () => {
  it("opens the kind picker (deferred past the QuickBar's own close)", () => {
    newDocumentFlow("");
    expect(openQuickPick).not.toHaveBeenCalled(); // deferred, not synchronous
    vi.runAllTimers();
    expect(openQuickPick).toHaveBeenCalledTimes(1);
    expect(picks[0].label).toBe("New document");
    expect(picks[0].rows("").map((row: QuickPickRow) => row.title)).toEqual([
      "Word",
      "Excel",
      "PowerPoint",
      "Python",
      "Text",
      "Markdown",
      "Notebook",
    ]);
  });

  it("names the document, then creates it at the chosen folder", () => {
    newDocumentFlow("reports");
    vi.runAllTimers();
    const word = picks[0].rows("").find((row) => row.title === "Word");
    expect(word).toBeDefined();
    word?.run();
    vi.runAllTimers();

    expect(picks).toHaveLength(2);
    const namePick = picks[1];
    expect(namePick.label).toBe("New Word document");

    // An empty name shows a disabled hint — Enter has nothing to do yet.
    const hint = namePick.rows("");
    expect(hint).toHaveLength(1);
    expect(hint[0].disabled).toBe(true);

    // A typed name offers a single create row that carries the extension and
    // the folder into the store call.
    const typed = namePick.rows("proposal");
    expect(typed).toHaveLength(1);
    expect(typed[0].disabled).toBeUndefined();
    expect(typed[0].title).toBe("Create reports/proposal.docx");
    typed[0].run();
    expect(createDocument).toHaveBeenCalledWith("reports/proposal.docx", "docx");
  });
});
