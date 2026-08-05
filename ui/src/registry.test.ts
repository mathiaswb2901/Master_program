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
import { officeHostTool } from "./panels/OfficeHostPanel";
import {
  applyDefaultLayout,
  danglingShortcutIds,
  defaultLayout,
  documentViewFor,
  documentViews,
  dynamicCommandsKey,
  notifyDockReady,
  openToolPanel,
  panelComponents,
  panelFocusCommands,
  panelTabInfo,
  panelTools,
  shortcutAction,
  shortcutHost,
  statusItems,
  toolCommands,
  toolDynamicCommands,
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

  it("keeps a command's own `when` live, and it alone", () => {
    let hasFiles = false;
    const commands = toolCommands([
      tool({
        id: "demo",
        commands: [
          { id: "demo.always", title: "Always", run: () => undefined },
          { id: "demo.sometimes", title: "Sometimes", when: () => hasFiles, run: () => undefined },
        ],
      }),
    ]);
    expect(commands.map((command) => command.when?.())).toEqual([undefined, false]);
    hasFiles = true;
    expect(commands.map((command) => command.when?.())).toEqual([undefined, true]);
  });

  // Dynamic commands exist because one row per saved layout cannot be written
  // down at build time. They are re-derived only when `key` changes, because
  // the merged list they join is rebuilt on every keystroke in the app.
  it("re-derives dynamic commands only when their key changes", () => {
    let names = ["one"];
    let builds = 0;
    const tools = [
      tool({
        id: "demo",
        dynamicCommands: {
          key: () => names.join(" "),
          build: () => {
            builds += 1;
            return names.map((name) => ({ id: `demo.${name}`, title: name, run: () => undefined }));
          },
        },
      }),
    ];
    expect(dynamicCommandsKey(tools)).toBe("one");
    expect(toolDynamicCommands(tools).map((command) => command.id)).toEqual(["demo.one"]);
    names = ["one", "two"];
    expect(dynamicCommandsKey(tools)).toBe("one two");
    expect(toolDynamicCommands(tools)).toHaveLength(2);
    expect(builds).toBe(2);
  });

  it("hands a shortcut kind to the tool that carries it out, if any", () => {
    const carried: string[] = [];
    const tools = [
      tool({ id: "quiet" }),
      tool({ id: "actor", shortcutActions: { layout: (body) => carried.push(body) } }),
    ];
    shortcutAction(tools, "layout")?.("Review");
    expect(carried).toEqual(["Review"]);
    // Nothing claims `shell`, so it stays an insertion — the caller's fallback.
    expect(shortcutAction(tools, "shell")).toBeNull();
  });

  it("drops a disabled tool's shortcut action with the rest of it", () => {
    const off = tool({
      id: "off",
      when: () => false,
      shortcutActions: { layout: () => undefined },
    });
    expect(shortcutAction([off], "layout")).toBeNull();
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

  it("drops a disabled tool's panel, commands, views and status items alike", () => {
    const off = tool({
      id: "off",
      when: () => false,
      panel: { component: Stub, defaultLocation: { area: "left" } },
      commands: [{ id: "off.go", title: "Go", run: () => undefined }],
      statusContributions: [{ region: "left", component: Stub }],
      documentView: { kind: "office", component: Stub, hostClassName: "x" },
      shortcutKinds: ["shell"],
    });
    expect(defaultLayout([off])).toEqual([]);
    expect(panelComponents([off])).toEqual({});
    expect(toolCommands([off])).toEqual([]);
    expect(statusItems([off], "left")).toEqual([]);
    expect(documentViews([off])).toEqual([]);
    expect(shortcutHost([off], "shell")).toBeNull();
  });

  // `when` is documented as a boot-time gate, not a live one. The consumers
  // snapshot at different moments — dockview's components once, the status bar
  // on every render — so a predicate that flipped later would leave a tool half
  // present: a status item and a QuickBar row for a panel dockview was never
  // told about. The answer is cached per tool; this is what pins that.
  it("settles `when` at first derivation — a later flip changes nothing", () => {
    let ready = false;
    const late = tool({
      id: "late",
      when: () => ready,
      panel: { component: Stub, defaultLocation: { area: "right" } },
      commands: [{ id: "late.go", title: "Go", run: () => undefined }],
      statusContributions: [{ region: "right", component: Stub }],
    });
    const tools = [withPanel("mid", "center"), late];
    expect(panelComponents(tools)).toEqual({ mid: Stub });

    ready = true;
    // Re-derived *after* the flip, and still gone — including the status bar,
    // which is the one consumer that asks again on every render.
    expect(Object.keys(panelComponents(tools))).toEqual(["mid"]);
    expect(toolCommands(tools)).toEqual([]);
    expect(panelFocusCommands(tools, () => undefined).map((c) => c.id)).toEqual(["panel.mid"]);
    expect(statusItems(tools, "right")).toEqual([]);
  });

  it("places a second centre panel inside the first group", () => {
    const dock = fakeDock();
    applyDefaultLayout(asDock(dock), [withPanel("mid", "center"), withPanel("also", "center")]);
    expect(dock.panels[1]).toEqual({
      id: "also",
      component: "also",
      title: "also",
      position: { referencePanel: "mid", direction: "within" },
    });
  });

  it("gives a close button to on-demand panels only", () => {
    const onDemand = tool({
      id: "extra",
      icon: Stub,
      panel: {
        component: Stub,
        defaultLocation: { area: "right" },
        openByDefault: false,
        badge: Stub,
      },
    });
    const tools = [withPanel("mid", "center"), onDemand];
    expect(panelTabInfo(tools, "mid")).toEqual({ closable: false });
    expect(panelTabInfo(tools, "extra")).toEqual({ icon: Stub, badge: Stub, closable: true });
    // Nothing registered under that id: bare chrome, never a close button.
    expect(panelTabInfo(tools, "gone")).toEqual({ closable: false });
  });

  it("routes a shortcut kind to the panel that claims it", () => {
    const tools = [
      withPanel("mid", "center"),
      tool({
        id: "shell",
        panel: { component: Stub, defaultLocation: { area: "bottom" } },
        shortcutKinds: ["shell"],
      }),
    ];
    expect(shortcutHost(tools, "shell")).toBe("shell");
    expect(shortcutHost(tools, "prompt")).toBeNull();
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

  // How the layout system gets the dock without `App.tsx` calling it by name.
  it("hands the dock to the tools that asked for it, and to no others", () => {
    const seen: (string | null)[] = [];
    const tools = [
      tool({ id: "watcher", onDockReady: (api) => seen.push(api === null ? null : "api") }),
      tool({ id: "quiet" }),
      tool({ id: "off", when: () => false, onDockReady: () => seen.push("never") }),
    ];
    const dock = fakeDock();
    notifyDockReady(tools, asDock(dock));
    notifyDockReady(tools, null);
    expect(seen).toEqual(["api", null]);
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

  /**
   * Two tools offer a view for `office`, and that is the seam working rather
   * than a collision: the native host claims the kind by being registered
   * first, and renders OnlyOffice itself on every machine that cannot dock a
   * real window. What must stay true is that *one* of them is chosen, and
   * which one.
   */
  it("resolves each open-file kind to exactly one view", () => {
    const kinds = documentViews(TOOLS).map((view) => view.kind);
    expect(kinds).toEqual(["office"]);
    // Two tools offer a view for `office` and that is the seam working: the
    // native host claims the kind by registering first and renders OnlyOffice
    // itself wherever it cannot dock a real window. Mounting both would give
    // every open document a second, hidden editor.
    expect(documentViewFor(TOOLS, "office")?.component).toBe(
      officeHostTool.documentView?.component,
    );
  });

  /**
   * Every chord the app ships, against the command it runs.
   *
   * This is the one assertion a keymap refactor cannot get past. Uniqueness,
   * parseability and `danglingShortcutIds` all catch a *typo*; none of them
   * catches a chord that quietly moved to a different command — `Ctrl+F4` left
   * on a valid-but-wrong id passes every other test in this file while a
   * user's reflex now closes something else. Moving one here takes editing the
   * literal, which is exactly the deliberate act it should be.
   */
  it("bind every shipped chord to the command it has always run", () => {
    const bound = Object.fromEntries(
      [...toolCommands(TOOLS), ...panelFocusCommands(TOOLS, () => undefined)].flatMap((command) =>
        (command.keys ?? []).map((key) => [key, command.id]),
      ),
    );
    expect(bound).toEqual({
      "Ctrl+S": "file.save",
      "Ctrl+PageDown": "editor.nextTab",
      "Alt+PageDown": "editor.nextTab",
      "Ctrl+PageUp": "editor.prevTab",
      "Alt+PageUp": "editor.prevTab",
      "Alt+W": "editor.close",
      "Ctrl+F4": "editor.close",
      "Alt+1": "session.jump.1",
      "Alt+2": "session.jump.2",
      "Alt+3": "session.jump.3",
      "Alt+4": "session.jump.4",
      "Alt+5": "session.jump.5",
      "Alt+6": "session.jump.6",
      "Alt+7": "session.jump.7",
      "Alt+8": "session.jump.8",
      "Alt+9": "session.jump.9",
      "Alt+T": "terminal.new",
      "Alt+M": "layout.focus",
      "Ctrl+1": "panel.files",
      "Ctrl+2": "panel.editors",
      "Ctrl+3": "panel.agent",
      "Ctrl+4": "panel.terminal",
    });
  });

  // Alt is the only modifier a user's `shortcuts.md` may bind, and a registered
  // chord wins every collision — so every Alt chord above is one a user cannot
  // have. The four that are taken earn it (tab close, the session jumps, a new
  // terminal); a demo tool does not, which is why the Scratchpad ships none.
  it("claim no chord for a tool that does not need one", () => {
    expect(TOOLS.find((registered) => registered.id === "scratchpad")?.shortcuts).toBeUndefined();
  });

  it("close exactly the panels that open on demand", () => {
    const closable = panelTools(TOOLS)
      .filter((registered) => panelTabInfo(TOOLS, registered.id).closable)
      .map((registered) => registered.id);
    expect(closable).toEqual(["scratchpad", "usage"]);
  });

  it("host both shortcut kinds", () => {
    expect(shortcutHost(TOOLS, "shell")).toBe("terminal");
    expect(shortcutHost(TOOLS, "prompt")).toBe("agent");
  });
});
