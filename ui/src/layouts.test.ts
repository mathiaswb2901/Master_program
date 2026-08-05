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
  presetPlacements,
  pruneLayout,
  type LayoutPreset,
} from "./layouts";
import { defaultLayout, type WorkbenchTool } from "./registry";

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
  initMonaco: () => undefined,
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

const KNOWN = new Set(["files", "editors", "scratchpad"]);

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
    const pruned = pruneLayout(savedLayout(), new Set(["files", "editors"]));
    expect(pruned?.dropped).toEqual(["scratchpad"]);
    expect(Object.keys(asJson(asJson(pruned?.layout).panels))).toEqual(["files", "editors"]);
    expect(groups(pruned?.layout).map((group) => group.views)).toEqual([
      ["files"],
      ["editors"],
    ]);
  });

  it("collapses a group whose panels have all gone", () => {
    const pruned = pruneLayout(savedLayout(), new Set(["editors", "scratchpad"]));
    expect(pruned?.dropped).toEqual(["files"]);
    expect(groups(pruned?.layout).map((group) => group.id)).toEqual(["g2"]);
  });

  it("forgets an active view that was dropped, keeping the group", () => {
    const layout = savedLayout();
    // The tab that was in front is the one that no longer exists.
    const pruned = pruneLayout(layout, new Set(["files", "scratchpad"]));
    const group = groups(pruned?.layout).find((candidate) => candidate.id === "g2");
    expect(group?.views).toEqual(["scratchpad"]);
    expect(group).not.toHaveProperty("activeView");
  });

  it("forgets an active group that was dropped", () => {
    const pruned = pruneLayout(savedLayout(), new Set(["files"]));
    expect(asJson(pruned?.layout)).not.toHaveProperty("activeGroup");
  });

  it("uses the default rather than an empty window when nothing survives", () => {
    expect(pruneLayout(savedLayout(), new Set(["nothing-we-ship"]))).toBeNull();
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
    const pruned = pruneLayout(layout, new Set(["files", "editors"]));
    expect(asJson(pruned?.layout).grid).not.toHaveProperty("maximizedNode");
  });

  it("prunes floating groups the same way, and drops the empty ones", () => {
    const layout = savedLayout();
    layout.floatingGroups = [
      { data: { id: "f1", views: ["scratchpad"] }, position: { top: 0, left: 0 } },
      { data: { id: "f2", views: ["editors", "scratchpad"] }, position: { top: 0, left: 0 } },
    ];
    const pruned = pruneLayout(layout, new Set(["files", "editors"]));
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
