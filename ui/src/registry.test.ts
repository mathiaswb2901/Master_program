/**
 * The tool registry: its derivations against fixtures, and its invariants
 * against the *real* `TOOLS`.
 *
 * The second half is the point. A registry that is only tested with fixtures
 * proves the functions work; these assertions prove that what the app actually
 * ships still holds together — unique ids, no chord bound twice across every
 * registered tool, and a default layout that is panel-for-panel what it was
 * before the registry existed (the "zero pixels changed" claim, at unit level).
 *
 * Importing the real registry means importing the panel modules, so the two
 * things that cannot load outside a browser are stubbed: Monaco's bundle, and
 * the store (which reads `document` at import). Neither is what is under test.
 */

import { describe, expect, it, vi } from "vitest";

import { parseChord } from "./keys";
import {
  agentToolDeclarations,
  applyDefaultLayout,
  danglingShortcutIds,
  defaultLayout,
  documentViewFor,
  documentViews,
  openToolPanel,
  panelComponents,
  panelFocusCommands,
  statusItems,
  toolCommands,
  type WorkbenchTool,
} from "./registry";

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

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => ({}) }),
  emptyPlanDraft: () => ({ choices: {}, annotations: {}, comment: "", verdict: null }),
  unchosenOptionGroups: () => [],
}));

const { TOOLS } = await import("./tools");

// ---- fixtures ---------------------------------------------------------------

const Stub = () => null;

const tool = (over: Partial<WorkbenchTool> & { id: string }): WorkbenchTool => ({
  title: over.id,
  ...over,
});

const withPanel = (id: string, area: "center" | "left" | "right" | "bottom"): WorkbenchTool =>
  tool({ id, panel: { component: Stub, defaultLocation: { area } } });

/** Enough of dockview's api to record what a layout did to it. */
function fakeDock() {
  const panels: { id: string; component: string; title?: string; position?: unknown }[] = [];
  const active: string[] = [];
  const api = {
    addPanel: (options: { id: string; component: string; title?: string }) => {
      panels.push(options);
    },
    getPanel: (id: string) =>
      panels.some((panel) => panel.id === id)
        ? { api: { setActive: () => active.push(id) } }
        : undefined,
  };
  return { api, panels, active };
}

const asDock = (dock: ReturnType<typeof fakeDock>): Parameters<typeof applyDefaultLayout>[0] =>
  dock.api as unknown as Parameters<typeof applyDefaultLayout>[0];

// ---- derivations ------------------------------------------------------------

describe("commands from a descriptor", () => {
  it("takes each command's chords from the tool's shortcut table", () => {
    const commands = toolCommands([
      tool({
        id: "demo",
        commands: [
          { id: "demo.a", title: "A", run: () => undefined },
          { id: "demo.b", title: "B", run: () => undefined },
        ],
        shortcuts: { "demo.a": ["Alt+A", "Ctrl+F4"] },
      }),
    ]);
    expect(commands.map((command) => command.keys)).toEqual([["Alt+A", "Ctrl+F4"], undefined]);
  });

  it("folds a tool's `when` into every command it owns", () => {
    let enabled = false;
    const commands = toolCommands([
      tool({
        id: "demo",
        when: () => enabled,
        commands: [
          { id: "demo.always", title: "Always", run: () => undefined },
          { id: "demo.sometimes", title: "Sometimes", when: () => true, run: () => undefined },
        ],
      }),
    ]);
    expect(commands.map((command) => command.when?.())).toEqual([false, false]);
    enabled = true;
    expect(commands.map((command) => command.when?.())).toEqual([true, true]);
  });

  it("reports a shortcut table entry that names no command of that tool", () => {
    const typo = tool({
      id: "demo",
      commands: [{ id: "demo.a", title: "A", run: () => undefined }],
      shortcuts: { "demo.typo": ["Alt+A"] },
    });
    expect(danglingShortcutIds([typo])).toEqual(["demo: demo.typo"]);
  });
});

describe("panels", () => {
  it("puts centre panels first so the rest can be placed against one", () => {
    const layout = defaultLayout([
      withPanel("left", "left"),
      withPanel("mid", "center"),
      withPanel("low", "bottom"),
    ]);
    expect(layout.map((placement) => placement.id)).toEqual(["mid", "left", "low"]);
  });

  it("leaves a panel that does not open by default out of the layout", () => {
    const extra = tool({
      id: "extra",
      panel: { component: Stub, defaultLocation: { area: "right" }, openByDefault: false },
    });
    const tools = [withPanel("mid", "center"), extra];
    expect(defaultLayout(tools).map((placement) => placement.id)).toEqual(["mid"]);
    // …but it is still a component the dock can render, and still focusable.
    expect(Object.keys(panelComponents(tools))).toEqual(["mid", "extra"]);
  });

  it("derives Ctrl+1..N focus commands in registry order", () => {
    const focused: string[] = [];
    const commands = panelFocusCommands(
      [withPanel("one", "left"), withPanel("two", "center"), withPanel("three", "bottom")],
      (id) => focused.push(id),
    );
    expect(commands.map((command) => [command.id, command.keys?.[0]])).toEqual([
      ["panel.one", "Ctrl+1"],
      ["panel.two", "Ctrl+2"],
      ["panel.three", "Ctrl+3"],
    ]);
    commands[1]?.run();
    expect(focused).toEqual(["two"]);
  });

  it("drops a disabled tool's panel, commands and status items alike", () => {
    const off = tool({
      id: "off",
      when: () => false,
      panel: { component: Stub, defaultLocation: { area: "left" } },
      statusContributions: [{ region: "left", component: Stub }],
      documentView: { kind: "office", component: Stub, hostClassName: "x" },
      agentTools: [{ name: "gone", description: "d", outputFormat: "text" }],
    });
    expect(defaultLayout([off])).toEqual([]);
    expect(panelComponents([off])).toEqual({});
    expect(statusItems([off], "left")).toEqual([]);
    expect(documentViews([off])).toEqual([]);
    expect(agentToolDeclarations([off])).toEqual([]);
  });

  it("builds the layout against the first centre panel and activates it", () => {
    const dock = fakeDock();
    applyDefaultLayout(asDock(dock), [
      tool({ id: "left", panel: { component: Stub, defaultLocation: { area: "left", size: 240 } } }),
      withPanel("mid", "center"),
      tool({ id: "low", panel: { component: Stub, defaultLocation: { area: "bottom", size: 260 } } }),
    ]);
    expect(dock.panels).toEqual([
      { id: "mid", component: "mid", title: "mid" },
      {
        id: "left",
        component: "left",
        title: "left",
        position: { referencePanel: "mid", direction: "left" },
        initialWidth: 240,
      },
      {
        id: "low",
        component: "low",
        title: "low",
        position: { referencePanel: "mid", direction: "below" },
        initialHeight: 260,
      },
    ]);
    expect(dock.active).toEqual(["mid"]);
  });

  it("focuses a singleton panel instead of opening a second one", () => {
    const tools = [withPanel("mid", "center"), withPanel("side", "right")];
    const dock = fakeDock();
    applyDefaultLayout(asDock(dock), tools);
    openToolPanel(asDock(dock), tools, "side");
    expect(dock.panels.filter((panel) => panel.component === "side")).toHaveLength(1);
    expect(dock.active).toEqual(["mid", "side"]);
  });

  it("opens a second instance of a panel that is not a singleton", () => {
    const many = tool({
      id: "many",
      panel: { component: Stub, defaultLocation: { area: "right" }, singleton: false },
    });
    const tools = [withPanel("mid", "center"), many];
    const dock = fakeDock();
    applyDefaultLayout(asDock(dock), tools);
    openToolPanel(asDock(dock), tools, "many");
    const opened = dock.panels.filter((panel) => panel.component === "many");
    expect(opened).toHaveLength(2);
    expect(opened[0]?.id).not.toBe(opened[1]?.id);
  });
});

describe("document views and status items", () => {
  it("answers which view renders a file kind", () => {
    const view = { kind: "office" as const, component: Stub, hostClassName: "wb-office-host" };
    const tools = [tool({ id: "office", documentView: view })];
    expect(documentViewFor(tools, "office")).toBe(view);
    expect(documentViewFor(tools, "text")).toBeNull();
  });

  it("keeps status items in registry order, per region", () => {
    const tools = [
      tool({ id: "a", statusContributions: [{ region: "left", component: Stub }] }),
      tool({
        id: "b",
        statusContributions: [
          { region: "right", component: Stub },
          { region: "left", component: Stub },
        ],
      }),
    ];
    expect(statusItems(tools, "left").map((item) => item.key)).toEqual(["a:left:0", "b:left:1"]);
    expect(statusItems(tools, "right").map((item) => item.key)).toEqual(["b:right:0"]);
  });
});

// ---- the registry the app actually ships ------------------------------------

describe("the registered tools", () => {
  it("have unique ids", () => {
    const ids = TOOLS.map((registered) => registered.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("own unique command ids and never bind a chord twice", () => {
    const commands = [...panelFocusCommands(TOOLS, () => undefined), ...toolCommands(TOOLS)];
    const ids = commands.map((command) => command.id);
    expect(new Set(ids).size, "duplicate command id").toBe(ids.length);
    const seen = new Map<string, string>();
    for (const command of commands) {
      for (const text of command.keys ?? []) {
        const chord = parseChord(text);
        const key = `${String(chord.ctrl)}|${String(chord.alt)}|${String(chord.shift)}|${chord.key}`;
        expect(chord.key, `${command.id} -> ${text}`).not.toBe("");
        expect(seen.get(key), `${text} bound twice`).toBeUndefined();
        seen.set(key, command.id);
      }
    }
  });

  it("bind every chord they declare to a command they own", () => {
    expect(danglingShortcutIds(TOOLS)).toEqual([]);
  });

  // The layout the app opened with before the registry existed, panel for
  // panel and pixel for pixel. A tool added later must not change it: a new
  // panel either joins this list on purpose, or ships `openByDefault: false`.
  it("produce exactly the default layout the app shipped before", () => {
    expect(defaultLayout(TOOLS)).toEqual([
      { id: "editors", component: "editors", title: "Editor", location: { area: "center" } },
      {
        id: "files",
        component: "files",
        title: "Files",
        location: { area: "left", size: 240 },
      },
      {
        id: "agent",
        component: "agent",
        title: "Agent",
        location: { area: "right", size: 380 },
      },
      {
        id: "terminal",
        component: "terminal",
        title: "Terminal",
        location: { area: "bottom", size: 260 },
      },
    ]);
  });

  it("keep Ctrl+1..4 on Files / Editor / Agent / Terminal", () => {
    expect(
      panelFocusCommands(TOOLS, () => undefined).map((command) => [
        command.title,
        command.keys?.[0],
      ]),
    ).toEqual([
      ["Focus Files panel", "Ctrl+1"],
      ["Focus Editor panel", "Ctrl+2"],
      ["Focus Agent panel", "Ctrl+3"],
      ["Focus Terminal panel", "Ctrl+4"],
    ]);
  });

  it("render every open-file kind exactly once", () => {
    const kinds = documentViews(TOOLS).map((view) => view.kind);
    expect(kinds).toEqual(["office"]);
    expect(new Set(kinds).size).toBe(kinds.length);
  });

  // The ergonomics budget's UI half: a tool description is loaded into every
  // session's context, so it is a cost paid on every request (CLAUDE.md). The
  // server registry carries the same ceiling over the model-facing text
  // (server/tests/test_agent_tools.py); this one binds the declarations here.
  it("declare agent tools within the description budget", () => {
    const declared = agentToolDeclarations(TOOLS);
    expect(declared.map((agentTool) => agentTool.name)).toEqual([
      "get_workspace_state",
      "present_plan",
    ]);
    const names = declared.map((agentTool) => agentTool.name);
    expect(new Set(names).size).toBe(names.length);
    for (const agentTool of declared) {
      expect(agentTool.description.length, agentTool.name).toBeLessThanOrEqual(120);
    }
  });
});
