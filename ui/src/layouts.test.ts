/**
 * The layout system's two decisions, tested where they are made.
 *
 * 1. **Restore robustness.** A saved layout outlives the tools it names. Every
 *    assertion in the first half is a shape that used to reach dockview and
 *    take the window with it: a panel nothing is registered under, a group that
 *    empties out, an active view that is gone, a file that is not a layout at
 *    all. The rule is always the same — drop what is unknown, keep the rest,
 *    and hand back `null` (meaning "use the default") rather than something
 *    dockview will choke on.
 * 2. **Presets are built from the registry.** They name tool ids, so the second
 *    half proves an id that is not registered is simply skipped, and that the
 *    presets the app actually ships still resolve against the real `TOOLS`.
 */

import { describe, expect, it, vi } from "vitest";

import {
  DEFAULT_LAYOUT_NAME,
  LAYOUT_PRESETS,
  droppedPanelsMessage,
  droppedPanesMessage,
  presetPlacements,
  pruneLayout,
  type LayoutPreset,
} from "./layouts";
import { defaultLayout, paneVocabulary, type PaneVocabulary, type WorkbenchTool } from "./registry";

// Importing the real registry means importing the panel modules: Monaco's
// bundle and the store cannot load outside a browser, and neither is under test.
vi.mock("./monaco", () => ({
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

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => ({}) }),
  emptyPlanDraft: () => ({ choices: {}, annotations: {}, comment: "", verdict: null }),
  unchosenOptionGroups: () => [],
}));

const { TOOLS } = await import("./tools");

// ---- fixtures ---------------------------------------------------------------

type Json = Record<string, unknown>;

const leaf = (id: string, views: string[], active?: string): Json => ({
  type: "leaf",
  size: 240,
  data: { id, views, ...(active !== undefined ? { activeView: active } : {}) },
});

/** The shape dockview's `toJSON()` writes: a grid of groups, plus a flat panel
 * map keyed by panel id whose `contentComponent` is the registered tool id. */
const savedLayout = (): Json => ({
  grid: {
    root: {
      type: "branch",
      size: 600,
      data: [
        leaf("g1", ["files"], "files"),
        leaf("g2", ["editors", "scratchpad"], "editors"),
      ],
    },
    width: 1040,
    height: 600,
    orientation: "HORIZONTAL",
  },
  panels: {
    files: { id: "files", contentComponent: "files", title: "Files" },
    editors: { id: "editors", contentComponent: "editors", title: "Editor" },
    scratchpad: { id: "scratchpad", contentComponent: "scratchpad", title: "Scratchpad" },
  },
  activeGroup: "g2",
});

/** What a pane id may say: components the app can render, and which of those
 * may carry an instance key (`registry.ts`, `paneVocabulary`). */
const vocab = (components: string[], plural: string[] = []): PaneVocabulary => ({
  components: new Set(components),
  plural: new Set(plural),
});

const KNOWN = vocab(["files", "editors", "scratchpad"]);

/** `pruneLayout` returns dockview's declared type, which omits the keys its own
 * serializer writes — read the result back as plain JSON to assert on it. */
const asJson = (value: unknown): Json => value as Json;

const groups = (layout: unknown): Json[] =>
  (asJson(asJson(asJson(layout).grid).root).data as unknown[]).map((node) =>
    asJson(asJson(node).data),
  );

// ---- restoring a persisted layout -------------------------------------------

describe("pruning a saved layout", () => {
  it("passes a layout whose every panel is registered through untouched", () => {
    const pruned = pruneLayout(savedLayout(), KNOWN);
    expect(pruned?.dropped).toEqual([]);
    expect(pruned?.layout).toEqual(savedLayout());
  });

  it("drops a panel nothing is registered under and keeps the rest", () => {
    const pruned = pruneLayout(savedLayout(), vocab(["files", "editors"]));
    expect(pruned?.dropped).toEqual(["scratchpad"]);
    expect(Object.keys(asJson(asJson(pruned?.layout).panels))).toEqual(["files", "editors"]);
    expect(groups(pruned?.layout).map((group) => group.views)).toEqual([
      ["files"],
      ["editors"],
    ]);
  });

  it("collapses a group whose panels have all gone", () => {
    const pruned = pruneLayout(savedLayout(), vocab(["editors", "scratchpad"]));
    expect(pruned?.dropped).toEqual(["files"]);
    expect(groups(pruned?.layout).map((group) => group.id)).toEqual(["g2"]);
  });

  it("forgets an active view that was dropped, keeping the group", () => {
    const layout = savedLayout();
    // The tab that was in front is the one that no longer exists.
    const pruned = pruneLayout(layout, vocab(["files", "scratchpad"]));
    const group = groups(pruned?.layout).find((candidate) => candidate.id === "g2");
    expect(group?.views).toEqual(["scratchpad"]);
    expect(group).not.toHaveProperty("activeView");
  });

  it("forgets an active group that was dropped", () => {
    const pruned = pruneLayout(savedLayout(), vocab(["files"]));
    expect(asJson(pruned?.layout)).not.toHaveProperty("activeGroup");
  });

  it("uses the default rather than an empty window when nothing survives", () => {
    expect(pruneLayout(savedLayout(), vocab(["nothing-we-ship"]))).toBeNull();
  });

  it("uses the default when nothing has ever been saved", () => {
    expect(pruneLayout(null, KNOWN)).toBeNull();
    expect(pruneLayout(undefined, KNOWN)).toBeNull();
  });

  it.each([
    ["a corrupt file's leftovers", { hello: "world" }],
    ["a string", "not a layout"],
    ["a list", [1, 2, 3]],
    ["a layout with no grid", { panels: {} }],
    ["a layout with no panel map", { grid: { root: {} } }],
  ])("uses the default for %s", (_name, value) => {
    expect(pruneLayout(value, KNOWN)).toBeNull();
  });

  // Focus mode is serialized as a *path* into the grid. Keeping that path after
  // the tree changed shape would put a different panel full screen — worse than
  // losing focus mode, so it only survives an untouched restore.
  it("keeps focus mode when nothing was dropped", () => {
    const layout = savedLayout();
    (layout.grid as Json).maximizedNode = { location: [1] };
    expect(asJson(pruneLayout(layout, KNOWN)?.layout).grid).toHaveProperty("maximizedNode");
  });

  it("drops focus mode when the grid lost a panel", () => {
    const layout = savedLayout();
    (layout.grid as Json).maximizedNode = { location: [1] };
    const pruned = pruneLayout(layout, vocab(["files", "editors"]));
    expect(asJson(pruned?.layout).grid).not.toHaveProperty("maximizedNode");
  });

  it("prunes floating groups the same way, and drops the empty ones", () => {
    const layout = savedLayout();
    layout.floatingGroups = [
      { data: { id: "f1", views: ["scratchpad"] }, position: { top: 0, left: 0 } },
      { data: { id: "f2", views: ["editors", "scratchpad"] }, position: { top: 0, left: 0 } },
    ];
    const pruned = pruneLayout(layout, vocab(["files", "editors"]));
    const floating = asJson(pruned?.layout).floatingGroups as Json[];
    expect(floating).toHaveLength(1);
    expect(asJson(floating[0]?.data).views).toEqual(["editors"]);
  });

  it("names what it dropped, once, in a sentence that counts correctly", () => {
    expect(droppedPanelsMessage(["office"])).toContain("that panel is");
    expect(droppedPanelsMessage(["office", "scratchpad"])).toContain("that panels are");
    expect(droppedPanelsMessage(["office", "scratchpad"])).toContain("office, scratchpad");
  });
});

// ---- a layout with more than one pane per tool -------------------------------

/**
 * The arrangement this milestone exists to make possible, in the shape the file
 * on disk actually holds it: three agent panes, two terminals and an editor.
 *
 * Everything about "which session, which file, which terminal" is carried in
 * the **panel ids** and nowhere else (`panes.ts`), so a round trip through
 * `pruneLayout` that lost or rewrote one of those ids would be an arrangement
 * that comes back looking right and pointing at the wrong things.
 */
const fleetLayout = (): Json => ({
  grid: {
    root: {
      type: "branch",
      size: 900,
      data: [
        leaf("g1", ["editors#src/app.ts"], "editors#src/app.ts"),
        leaf("g2", ["agent#sess-a"], "agent#sess-a"),
        leaf("g3", ["agent#sess-b", "agent#sess-c"], "agent#sess-c"),
        leaf("g4", ["terminal#1"], "terminal#1"),
        leaf("g5", ["terminal#2"], "terminal#2"),
      ],
    },
    width: 1600,
    height: 900,
    orientation: "HORIZONTAL",
  },
  panels: {
    "editors#src/app.ts": {
      id: "editors#src/app.ts",
      contentComponent: "editors",
      title: "app.ts",
    },
    "agent#sess-a": { id: "agent#sess-a", contentComponent: "agent", title: "Pricing" },
    "agent#sess-b": { id: "agent#sess-b", contentComponent: "agent", title: "Backfill" },
    "agent#sess-c": { id: "agent#sess-c", contentComponent: "agent", title: "Review" },
    "terminal#1": { id: "terminal#1", contentComponent: "terminal", title: "Terminal 1" },
    "terminal#2": { id: "terminal#2", contentComponent: "terminal", title: "Terminal 2" },
  },
  activeGroup: "g2",
});

const FLEET = vocab(["editors", "agent", "terminal"], ["editors", "agent", "terminal"]);

describe("a layout with several panes of the same tool", () => {
  it("round-trips every instance, id for id", () => {
    const pruned = pruneLayout(fleetLayout(), FLEET);
    expect(pruned?.dropped).toEqual([]);
    expect(pruned?.droppedPanes).toEqual([]);
    // Not "six panels survived" — *these* six, because the id is the binding.
    expect(pruned?.layout).toEqual(fleetLayout());
  });

  // The instance keys are the user's own strings: a session id, a path. None of
  // them may need escaping to survive the file, and a path with a `#` in it is
  // a file a user is allowed to have.
  it("keeps an instance key that contains the separator", () => {
    const layout = fleetLayout();
    (layout.panels as Json)["editors#notes#2.md"] = {
      id: "editors#notes#2.md",
      contentComponent: "editors",
      title: "notes#2.md",
    };
    ((layout.grid as Json).root as Json).data = [
      ...(((layout.grid as Json).root as Json).data as unknown[]),
      leaf("g6", ["editors#notes#2.md"], "editors#notes#2.md"),
    ];
    const pruned = pruneLayout(layout, FLEET);
    expect(pruned?.droppedPanes).toEqual([]);
    expect(Object.keys(asJson(asJson(pruned?.layout).panels))).toContain("editors#notes#2.md");
  });

  /**
   * The version-drift case, which is the one that used to be silent.
   *
   * A file written when the Agent was plural, read by a build where it is not,
   * would restore three panes all rendering the singleton panel — three copies
   * of the same conversation, none of them addressable by a pane command. It is
   * exactly as unusable as an unknown component and is dropped the same way,
   * with its own sentence: nothing was removed from Workbench.
   */
  it("drops an instance pane of a tool that is a singleton today", () => {
    const pruned = pruneLayout(fleetLayout(), vocab(["editors", "agent", "terminal"], ["agent"]));
    expect(pruned?.dropped).toEqual([]);
    expect(pruned?.droppedPanes).toEqual(["editors#src/app.ts", "terminal#1", "terminal#2"]);
    expect(Object.keys(asJson(asJson(pruned?.layout).panels))).toEqual([
      "agent#sess-a",
      "agent#sess-b",
      "agent#sess-c",
    ]);
  });

  it("drops a pane whose id and component disagree about the tool", () => {
    const layout = fleetLayout();
    (layout.panels as Json)["terminal#9"] = {
      id: "terminal#9",
      contentComponent: "agent",
      title: "Confused",
    };
    const pruned = pruneLayout(layout, FLEET);
    expect(pruned?.droppedPanes).toEqual(["terminal#9"]);
  });

  it("still takes the whole tool out when its component is gone", () => {
    const pruned = pruneLayout(fleetLayout(), vocab(["editors", "terminal"], ["editors", "terminal"]));
    expect(pruned?.dropped).toEqual(["agent"]);
    expect(pruned?.droppedPanes).toEqual([]);
  });

  it("gives up focus mode when it dropped a pane, not only a tool", () => {
    const layout = fleetLayout();
    (layout.grid as Json).maximizedNode = { location: [1] };
    const pruned = pruneLayout(layout, vocab(["editors", "agent", "terminal"], ["agent"]));
    expect(asJson(pruned?.layout).grid).not.toHaveProperty("maximizedNode");
  });

  it("counts panes correctly in the sentence it shows for them", () => {
    expect(droppedPanesMessage(["agent#x"])).toContain("1 pane");
    expect(droppedPanesMessage(["agent#x", "agent#y"])).toContain("2 panes");
  });

  // The vocabulary is not a fixture: it is derived from the registry the app
  // ships, and this is what ties the two halves of this file together.
  it("is derived from the real registry", () => {
    const real = paneVocabulary(TOOLS);
    expect([...real.plural].sort()).toEqual([
      "agent",
      "conversations",
      "editors",
      "review",
      "terminal",
      "visual",
    ]);
    expect(real.components.has("files")).toBe(true);
    expect(real.plural.has("files")).toBe(false);
  });
});

// ---- popping a pane out to its own window (M5 item 13) -----------------------

/**
 * A saved layout with one pane popped out to its own window. dockview serializes
 * a popout as an entry under `popoutGroups`, whose `data` is the group in the
 * other window and whose `gridReferenceGroup` names the grid group it re-grids
 * into when the window closes or the layout restores (`dockviewComponent.toJSON`).
 */
const poppedOutLayout = (gridReferenceGroup: string): Json => ({
  grid: {
    root: {
      type: "branch",
      size: 600,
      data: [leaf("g1", ["files"], "files"), leaf("g2", ["editors"], "editors")],
    },
    width: 1040,
    height: 600,
    orientation: "HORIZONTAL",
  },
  panels: {
    files: { id: "files", contentComponent: "files", title: "Files" },
    editors: { id: "editors", contentComponent: "editors", title: "Editor" },
    scratchpad: { id: "scratchpad", contentComponent: "scratchpad", title: "Scratchpad" },
  },
  activeGroup: "g1",
  popoutGroups: [
    {
      data: { id: "gp1", views: ["scratchpad"], activeView: "scratchpad" },
      gridReferenceGroup,
      position: { top: 100, left: 200, width: 640, height: 480 },
      url: "/popout.html",
    },
  ],
});

const KNOWN_PLUS_SCRATCHPAD = vocab(["files", "editors", "scratchpad"]);

describe("pruning a layout with a popped-out pane", () => {
  it("round-trips a popout whose reference group survives", () => {
    const layout = poppedOutLayout("g2");
    const pruned = pruneLayout(layout, KNOWN_PLUS_SCRATCHPAD);
    expect(pruned?.dropped).toEqual([]);
    expect(pruned?.droppedPanes).toEqual([]);
    // Kept whole: the popped-out pane, its window position and the group it
    // returns to all survive the trip so the next launch reopens it there.
    expect(asJson(pruned?.layout).popoutGroups).toEqual(layout.popoutGroups);
  });

  it("drops a popout whose gridReferenceGroup no longer exists", () => {
    // The reference names a grid group this layout never had — nowhere to
    // re-grid, so restoring it would fail. The panes in the grid are untouched.
    const pruned = pruneLayout(poppedOutLayout("gGONE"), KNOWN_PLUS_SCRATCHPAD);
    expect(pruned?.dropped).toEqual([]);
    expect(asJson(pruned?.layout)).not.toHaveProperty("popoutGroups");
    expect(Object.keys(asJson(asJson(pruned?.layout).panels)).sort()).toEqual([
      "editors",
      "files",
      "scratchpad",
    ]);
  });

  it("drops a popout whose reference group collapsed when its panels went away", () => {
    // g2 held only `editors`; with editors unregistered g2 collapses out of the
    // grid, so the popout that pointed at it has lost its home the same way.
    const pruned = pruneLayout(poppedOutLayout("g2"), vocab(["files", "scratchpad"]));
    expect(pruned?.dropped).toEqual(["editors"]);
    expect(asJson(pruned?.layout)).not.toHaveProperty("popoutGroups");
  });

  it("still drops a popout when its own pane is unaddressable, reference or not", () => {
    // The popped-out pane itself is gone (scratchpad unregistered): the entry
    // goes regardless of the reference, by the same `pruneGroup` pass floating
    // groups use.
    const pruned = pruneLayout(poppedOutLayout("g2"), vocab(["files", "editors"]));
    expect(pruned?.dropped).toEqual(["scratchpad"]);
    expect(asJson(pruned?.layout)).not.toHaveProperty("popoutGroups");
  });
});

// ---- presets ----------------------------------------------------------------

const Stub = () => null;

const panelTool = (id: string): WorkbenchTool => ({
  id,
  title: id,
  panel: { component: Stub, defaultLocation: { area: "right" } },
});

describe("presets built from the registry", () => {
  const preset = (over: Partial<LayoutPreset>): LayoutPreset => ({
    id: "test",
    name: "Test",
    detail: "",
    ...over,
  });

  it("places each named tool, at the location the preset asks for", () => {
    const placements = presetPlacements(
      [panelTool("alpha"), panelTool("beta")],
      preset({
        panels: [{ tool: "alpha", location: { area: "center" } }, { tool: "beta" }],
      }),
    );
    expect(placements).toEqual([
      { id: "alpha", component: "alpha", title: "alpha", location: { area: "center" } },
      { id: "beta", component: "beta", title: "beta", location: { area: "right" } },
    ]);
  });

  // The degradation the ROADMAP calls out: a preset naming a tool that was
  // removed, renamed, or gated off by `when` must build without it.
  it("skips a tool that is not registered instead of breaking the switch", () => {
    const placements = presetPlacements(
      [panelTool("alpha")],
      preset({ panels: [{ tool: "alpha" }, { tool: "gone" }] }),
    );
    expect(placements.map((placement) => placement.id)).toEqual(["alpha"]);
  });

  it("builds nothing at all when none of its tools exist", () => {
    expect(presetPlacements([panelTool("alpha")], preset({ panels: [{ tool: "gone" }] }))).toEqual(
      [],
    );
  });

  it("puts the centre panel first, whatever order the preset lists", () => {
    const placements = presetPlacements(
      [panelTool("alpha"), panelTool("beta")],
      preset({ panels: [{ tool: "alpha" }, { tool: "beta", location: { area: "center" } }] }),
    );
    expect(placements.map((placement) => placement.id)).toEqual(["beta", "alpha"]);
  });

  it("falls back to the registry's default layout when it names no panels", () => {
    const tools = [panelTool("alpha"), panelTool("beta")];
    expect(presetPlacements(tools, preset({}))).toEqual(defaultLayout(tools));
  });
});

describe("the presets the app ships", () => {
  it("all resolve against the real registry", () => {
    for (const shipped of LAYOUT_PRESETS) {
      expect(presetPlacements(TOOLS, shipped).length, shipped.name).toBeGreaterThan(0);
    }
  });

  it("only ask to maximize a panel they actually place", () => {
    for (const shipped of LAYOUT_PRESETS) {
      if (shipped.maximize === undefined) continue;
      const ids = presetPlacements(TOOLS, shipped).map((placement) => placement.id);
      expect(ids, shipped.name).toContain(shipped.maximize);
    }
  });

  it("have unique names that no built-in name collides with", () => {
    const names = [DEFAULT_LAYOUT_NAME, ...LAYOUT_PRESETS.map((shipped) => shipped.name)];
    expect(new Set(names.map((name) => name.toLowerCase())).size).toBe(names.length);
  });
});
