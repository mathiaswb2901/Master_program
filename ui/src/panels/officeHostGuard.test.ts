/**
 * The pop-out guard's *wording* when more than one Office window is docked.
 *
 * The guard itself (refuse popping out the Editor pane while a real Office
 * window is docked in it) is exercised in `registry.test.ts`. What is pinned
 * here is which application the refusal names: the app supports several docked
 * documents at once (every `office` tab keeps its `NativeHost` mounted), so the
 * reason must name the one the user is actually looking at — the active tab —
 * not whichever host happened to be inserted into the map first.
 *
 * `../officeHost` is the real store (seedable, import-safe). Only the two modules
 * that cannot load outside a browser are stubbed — Monaco's bundle and the app
 * store, the latter with a settable `activePath` so a test can point the active
 * tab at either docked document.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { useOfficeHostStore } from "../officeHost";
import type { OfficeHostInfo } from "../types";

vi.mock("../monaco", () => ({
  MONO_FONT: "mono",
  editorPathProp: (path: string) => path,
  languageForPath: () => "plaintext",
  monacoThemeName: () => "workbench",
  setActiveEditor: () => undefined,
  disposeModel: () => undefined,
  setModelContent: () => null,
  defineWorkbenchTheme: () => undefined,
  loadMonaco: () => Promise.resolve({}),
  prefetchMonaco: () => undefined,
}));

const store = vi.hoisted(() => ({ activePath: null as string | null }));

vi.mock("../store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => ({ activePath: store.activePath }) }),
}));

const { officeHostTool } = await import("./OfficeHostPanel");

function hostInfo(path: string, over: Partial<OfficeHostInfo> = {}): OfficeHostInfo {
  return {
    host_id: path,
    path,
    kind: "word",
    state: "embedded",
    reason: null,
    pid: 900001,
    since: 0,
    close_failed: false,
    ...over,
  };
}

function seed(...hosts: OfficeHostInfo[]): void {
  useOfficeHostStore.setState({ hosts: Object.fromEntries(hosts.map((h) => [h.path, h])) });
}

const blocks = (paneId: string): string | null => officeHostTool.popoutGuard?.blocks(paneId) ?? null;

beforeEach(() => {
  store.activePath = null;
  useOfficeHostStore.setState({ hosts: {} });
});

describe("the pop-out refusal names the docked document the user is looking at", () => {
  it("names the active tab's application, not whichever host was inserted first", () => {
    // Excel inserted first, Word active. The refusal must say Word.
    seed(
      hostInfo("budget.xlsx", { kind: "excel" }),
      hostInfo("report.docx", { kind: "word" }),
    );
    store.activePath = "report.docx";
    const reason = blocks("editors");
    expect(reason).toContain("Microsoft Word");
    expect(reason).not.toContain("Microsoft Excel");
  });

  it("names Excel when Excel is the active docked tab", () => {
    seed(
      hostInfo("report.docx", { kind: "word" }),
      hostInfo("budget.xlsx", { kind: "excel" }),
    );
    store.activePath = "budget.xlsx";
    expect(blocks("editors")).toContain("Microsoft Excel");
  });

  it("still refuses when the active tab is not itself docked, naming any docked host", () => {
    // A non-Office tab is active; a Word window is docked in the Editor pane
    // behind it. The pane is still blocked, and the reason names the docked one.
    seed(hostInfo("report.docx", { kind: "word" }));
    store.activePath = "notes.md";
    expect(blocks("editors")).toContain("Microsoft Word");
  });

  it("allows the Editor pane once nothing is docked", () => {
    expect(blocks("editors")).toBeNull();
  });
});
