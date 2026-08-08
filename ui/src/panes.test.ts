/**
 * The two rules that decide whether a user's arrangement is usable: **what a
 * pane is called**, and **which pane the keyboard goes to next**.
 *
 * Both are pure, and both are the kind of thing that is easy to get subtly
 * wrong and impossible to notice from a screenshot — a neighbour search that
 * silently prefers the pane whose group dockview happened to serialize first
 * looks fine in a 2×2 and is unusable in the 2×2-plus-a-terminal a real window
 * ends up as. So they are tested against fixed geometry rather than by clicking.
 *
 * The second half checks the id scheme against the registry the app ships: the
 * separator is only safe while no tool id contains it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import type { SessionLimits } from "./types";
import {
  cyclePane,
  oppositeDirection,
  paneId,
  paneInDirection,
  paneInstance,
  paneTool,
  parsePaneId,
  readingOrder,
  PANE_DIRECTIONS,
  PANE_SEPARATOR,
  type PaneRect,
} from "./panes";

// Importing the real registry means importing the panel modules; neither
// Monaco's bundle nor the store loads outside a browser, and neither is here.
vi.mock("./monaco", () => ({
  MONO_FONT: "mono",
  editorPathProp: (path: string) => path,
  languageForPath: () => "plaintext",
  monacoThemeName: () => "workbench",
  setActiveEditor: () => undefined,
  disposeModel: () => undefined,
  setModelContent: () => null,
  defineWorkbenchTheme: () => undefined,
  initMonaco: () => undefined,
}));

/** A store with nothing in it — the state a fresh workspace is in, and the one
 * the picker still has to have rows for. Mutable: the session ceiling is a
 * *number the server states*, so the rows it changes are asserted by moving it. */
const emptyState = {
  openFiles: [],
  folders: [],
  terminals: [],
  // A fresh workspace holds no plans, so the artifact tool offers no instance
  // rows — the same shape `useStore.getState().chats` has before any agent runs.
  chats: {} as Record<string, unknown>,
  activePath: null,
  sessionLimits: null as SessionLimits | null,
  nextTerminalPaneKey: () => "1",
  createSessionIn: () => Promise.resolve(null),
};

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => emptyState }),
  emptyPlanDraft: () => ({ choices: {}, annotations: {}, comment: "", verdict: null }),
  unchosenOptionGroups: () => [],
}));

const { TOOLS } = await import("./tools");
const { paneInstanceOptions, paneTitle, pluralPanelIds } = await import("./registry");

// ---- identity ---------------------------------------------------------------

describe("what a pane is called", () => {
  it("round-trips a tool's default pane and an instance of it", () => {
    expect(paneId("agent", null)).toBe("agent");
    expect(paneId("agent", "sess-1")).toBe("agent#sess-1");
    expect(parsePaneId("agent")).toEqual({ toolId: "agent", instance: null });
    expect(parsePaneId("agent#sess-1")).toEqual({ toolId: "agent", instance: "sess-1" });
  });

  // A path is an instance key, and a user is allowed a file called `notes#2.md`.
  // Splitting on the *first* separator is what makes that a pane rather than a
  // parse error, so it is pinned rather than assumed.
  it("keeps a separator inside the instance key", () => {
    const id = paneId("editors", "docs/notes#2.md");
    expect(parsePaneId(id)).toEqual({ toolId: "editors", instance: "docs/notes#2.md" });
    expect(paneTool(id)).toBe("editors");
    expect(paneInstance(id)).toBe("docs/notes#2.md");
  });

  it("treats an empty key as no key at all", () => {
    expect(parsePaneId("agent#")).toEqual({ toolId: "agent", instance: null });
  });

  it("round-trips every key an instance is bound to in practice", () => {
    for (const key of [
      "sess-01HXYZ",
      "src/app.ts",
      "docs/a b/c.md",
      "1",
      "a:b",
      "über.md",
    ]) {
      expect(paneInstance(paneId("editors", key))).toBe(key);
    }
  });

  // The separator is only unambiguous while no tool id contains it: `a#b` as a
  // tool id would make every pane of it address a tool called `a`.
  it("is unambiguous against the registry the app ships", () => {
    for (const tool of TOOLS) expect(tool.id).not.toContain(PANE_SEPARATOR);
  });
});

// ---- geometry ---------------------------------------------------------------

/**
 * The arrangement the E2E journey builds, as boxes:
 *
 * ```
 * +--------+-----------------+--------+
 * | files  |   editor        | agentA |
 * |        +-----------------+--------+
 * |        |   terminal      | agentB |
 * +--------+-----------------+--------+
 * ```
 */
const WINDOW: PaneRect[] = [
  { id: "files", left: 0, top: 0, width: 240, height: 800 },
  { id: "editor", left: 240, top: 0, width: 700, height: 500 },
  { id: "terminal", left: 240, top: 500, width: 700, height: 300 },
  { id: "agentA", left: 940, top: 0, width: 380, height: 400 },
  { id: "agentB", left: 940, top: 400, width: 380, height: 400 },
];

const go = (from: string, direction: (typeof PANE_DIRECTIONS)[number]): string | null =>
  paneInDirection(WINDOW, from, direction);

describe("moving between panes", () => {
  it("goes to the pane next to this one", () => {
    expect(go("editor", "left")).toBe("files");
    expect(go("editor", "down")).toBe("terminal");
    expect(go("terminal", "up")).toBe("editor");
    expect(go("files", "right")).toBe("editor");
  });

  // The tie-break that matters: from the editor, *two* panes are to the right
  // and both overlap it. The one whose centre is nearest is the one the user is
  // looking at; picking the other would make the key feel random.
  it("prefers the neighbour across from you when two are equally close", () => {
    expect(go("editor", "right")).toBe("agentA");
    expect(go("terminal", "right")).toBe("agentB");
  });

  it("stops at the edge of the window rather than wrapping", () => {
    expect(go("files", "left")).toBeNull();
    expect(go("agentA", "right")).toBeNull();
    expect(go("editor", "up")).toBeNull();
    expect(go("terminal", "down")).toBeNull();
  });

  // The L-shape: nothing to the left of `agentB` overlaps it (the terminal is
  // below the editor and only *partly* beside it in a taller window). A dead
  // end in one direction reads as the key being broken, so the search falls
  // back to the nearest pane in that half-plane.
  it("still moves when no pane truly overlaps in that direction", () => {
    const lShape: PaneRect[] = [
      { id: "top", left: 0, top: 0, width: 400, height: 300 },
      { id: "bottomRight", left: 400, top: 300, width: 400, height: 300 },
    ];
    expect(paneInDirection(lShape, "bottomRight", "left")).toBe("top");
    expect(paneInDirection(lShape, "top", "right")).toBe("bottomRight");
  });

  // dockview puts a sash between panes and rounds sizes, so "starts exactly
  // where the other ends" is never true. A strict comparison made the
  // neighbour invisible at some window widths and not others.
  it("tolerates the sub-pixel gap a sash leaves", () => {
    const rects: PaneRect[] = [
      { id: "a", left: 0, top: 0, width: 499.6, height: 400 },
      { id: "b", left: 500.4, top: 0, width: 499.6, height: 400 },
    ];
    expect(paneInDirection(rects, "a", "right")).toBe("b");
    expect(paneInDirection(rects, "b", "left")).toBe("a");
  });

  it("answers nothing for a pane that is not on screen", () => {
    expect(paneInDirection(WINDOW, "gone", "left")).toBeNull();
    expect(paneInDirection([], "files", "left")).toBeNull();
  });

  it("has an opposite for every direction, and it is an involution", () => {
    for (const direction of PANE_DIRECTIONS) {
      expect(oppositeDirection(oppositeDirection(direction))).toBe(direction);
      expect(oppositeDirection(direction)).not.toBe(direction);
    }
  });
});

describe("cycling", () => {
  // Screen order, not the grid's tree order: someone pressing Alt+O is looking
  // at the window.
  it("reads the window left to right, top to bottom", () => {
    // `agentB` starts higher than `terminal` (400 against 500), so it is read
    // first even though it sits further right — rows, then columns.
    expect(readingOrder(WINDOW)).toEqual(["files", "editor", "agentA", "agentB", "terminal"]);
  });

  it("wraps in both directions", () => {
    expect(cyclePane(WINDOW, "files", 1)).toBe("editor");
    expect(cyclePane(WINDOW, "terminal", 1)).toBe("files");
    expect(cyclePane(WINDOW, "files", -1)).toBe("terminal");
  });

  it("starts from the first pane when the current one has gone", () => {
    expect(cyclePane(WINDOW, "gone", 1)).toBe("files");
    expect(cyclePane([], "files", 1)).toBeNull();
  });

  it("treats a row whose tops differ by a sash as one row", () => {
    const rects: PaneRect[] = [
      { id: "right", left: 500, top: 1.4, width: 400, height: 300 },
      { id: "left", left: 0, top: 0, width: 499, height: 300 },
    ];
    expect(readingOrder(rects)).toEqual(["left", "right"]);
  });
});

// ---- what the picker offers -------------------------------------------------

describe("the picker's rows, against the registry the app ships", () => {
  it("offers a row for every tool with a panel, and instances for the plural ones", () => {
    const options = paneInstanceOptions(TOOLS);
    const plural = pluralPanelIds(TOOLS);
    // Every panel tool is represented: the singletons by one row each, the
    // plural ones by whatever they can be bound to right now (a store with no
    // sessions, files or terminals still offers "New terminal" and "New agent
    // session").
    expect(options.some((option) => option.toolId === "files")).toBe(true);
    expect(options.filter((option) => option.toolId === "files")).toHaveLength(1);
    expect([...plural].sort()).toEqual([
      "agent",
      "conversations",
      "editors",
      "review",
      "terminal",
      "visual",
    ]);
    expect(options.some((option) => option.id === "terminal.new")).toBe(true);
    expect(options.some((option) => option.id === "agent.new")).toBe(true);
  });

  it("gives every row a section, so the picker is never a flat wall", () => {
    for (const option of paneInstanceOptions(TOOLS)) {
      expect(option.category, option.id).not.toBe("");
    }
  });

  // A singleton's row answers with no key, which is what makes its pane id the
  // bare tool id — the same id the default layout uses, so picking `Files`
  // moves the Files pane rather than opening a second one.
  it("gives a singleton no instance key", async () => {
    const files = paneInstanceOptions(TOOLS).find((option) => option.toolId === "files");
    expect(await files?.key()).toBeNull();
  });

  // The ceiling is discovered *before* the gesture or not at all: picking the
  // row spends a split on a round trip the server refuses, and `placeChoice`
  // then abandons the split with only a toast to explain it.
  describe("the agent's concurrent-session ceiling", () => {
    const newSessionRow = (): { detail?: string; disabled?: boolean } | undefined =>
      paneInstanceOptions(TOOLS).find((option) => option.id === "agent.new");

    afterEach(() => {
      emptyState.sessionLimits = null;
    });

    it("offers the row while a slot is free", () => {
      emptyState.sessionLimits = { max_concurrent: 4, active: 3 };
      expect(newSessionRow()?.disabled).toBe(false);
    });

    it("greys it at the ceiling, and says the cap and the setting", () => {
      emptyState.sessionLimits = { max_concurrent: 4, active: 4 };
      const row = newSessionRow();
      expect(row?.disabled).toBe(true);
      expect(row?.detail).toContain("4 of 4");
      expect(row?.detail).toContain("WORKBENCH_MAX_CONCURRENT_SESSIONS");
    });

    // No numbers yet is not "you are at the limit": the listing has not landed,
    // and refusing a session the server would have granted is the worse error.
    it("predicts nothing before the first listing lands", () => {
      expect(emptyState.sessionLimits).toBeNull();
      expect(newSessionRow()?.disabled).toBe(false);
      expect(newSessionRow()?.detail).toBe("workspace root");
    });
  });

  it("names a pane from its id, falling back to something readable", () => {
    expect(paneTitle(TOOLS, "files")).toBe("Files");
    expect(paneTitle(TOOLS, "terminal")).toBe("Terminal");
    expect(paneTitle(TOOLS, "terminal#3")).toBe("Terminal 3");
    // A tool that is gone: the id itself, never `undefined` in a tab.
    expect(paneTitle(TOOLS, "missioncontrol#7")).toBe("missioncontrol#7");
  });
});
