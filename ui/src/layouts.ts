/**
 * The layout system's pure half: what a preset *is*, and what a persisted
 * layout has to survive before dockview is allowed near it.
 *
 * Free of React, the store, the api client and dockview's runtime (types only),
 * so the two rules that actually decide whether the window opens — pruning a
 * stale layout, and building a preset out of whatever is registered today — are
 * unit-tested rather than assumed (`layouts.test.ts`).
 *
 * **Why pruning exists.** dockview restores whatever the file says. Ask it for a
 * panel whose component nothing is registered under and it creates the panel
 * anyway, hands React `undefined` as the element type, and the window dies on
 * render — for one stale entry, in a file the user cannot see. Every layout that
 * comes off disk therefore goes through `pruneLayout` first: unknown panels are
 * dropped, the rest is kept, and if nothing usable is left the caller falls back
 * to the default arrangement.
 */

import type { SerializedDockview } from "dockview";

import { parsePaneId } from "./panes";
import {
  defaultLayout,
  orderPlacements,
  panelTools,
  placementOf,
  type PaneVocabulary,
  type PanelLocation,
  type PanelPlacement,
  type WorkbenchTool,
} from "./registry";

/** Mirrors `server/src/workbench_server/models/layouts.py` — the server refuses
 * anything past these, so the UI stops the user before the 422. */
export const MAX_LAYOUT_NAME_CHARS = 60;
export const MAX_SAVED_LAYOUTS = 24;

/** The arrangement the registry produces. Reserved as a layout name so
 * `shortcuts.md` can bind "back to normal" like any other layout. */
export const DEFAULT_LAYOUT_NAME = "Default";

// ---- presets ----------------------------------------------------------------

export interface PresetPanel {
  /** A registered tool's id. Unknown or disabled ids are skipped, never fatal. */
  tool: string;
  /** Override the tool's own `defaultLocation` for this arrangement. */
  location?: PanelLocation;
}

export interface LayoutPreset {
  id: string;
  /** What the QuickBar and the layout menu call it. Also what a `shortcuts.md`
   * `layout` entry names. */
  name: string;
  /** One-line explanation, shown as the QuickBar row's detail. */
  detail: string;
  /** The panels, in creation order. Omitted = the registry's default layout. */
  panels?: readonly PresetPanel[];
  /** Tool id to put in focus mode once the arrangement is built. */
  maximize?: string;
}

/**
 * The shipped arrangements. Deliberately written in terms of **tool ids**, not
 * panel geometry: each entry resolves through the registry, so a preset naming
 * a tool that is gone (or gated off by its `when`) simply builds without it —
 * and a preset whose tools are *all* gone builds nothing, which the caller
 * treats as "leave the window alone".
 */
export const LAYOUT_PRESETS: readonly LayoutPreset[] = [
  {
    id: "review",
    name: "Review",
    detail: "editor, files and the agent — no terminal",
    panels: [{ tool: "editors" }, { tool: "files" }, { tool: "agent" }],
  },
  {
    id: "focus",
    name: "Focus",
    detail: "the default arrangement, editor full screen",
    maximize: "editors",
  },
  {
    id: "agents",
    name: "Agents",
    detail: "the agent in the centre, files and a terminal around it",
    panels: [
      { tool: "agent", location: { area: "center" } },
      { tool: "files", location: { area: "left", size: 240 } },
      { tool: "terminal", location: { area: "bottom", size: 260 } },
    ],
  },
];

/**
 * A preset's placements against the tools registered right now. Empty means
 * nothing it names exists any more — the caller must not clear the dock for it.
 */
export function presetPlacements(
  tools: readonly WorkbenchTool[],
  preset: LayoutPreset,
): PanelPlacement[] {
  const panels = preset.panels;
  if (panels === undefined) return defaultLayout(tools);
  const registered = panelTools(tools);
  const placements = panels.flatMap((entry) => {
    const tool = registered.find((candidate) => candidate.id === entry.tool);
    return tool === undefined ? [] : [placementOf(tool, entry.location)];
  });
  return orderPlacements(placements);
}

// ---- restoring a persisted layout -------------------------------------------

export interface PrunedLayout {
  layout: SerializedDockview;
  /** Component ids nothing is registered under, in the order first seen. Empty
   * on the happy path; non-empty is what the user is told about, once. */
  dropped: string[];
  /**
   * Pane ids dropped for a reason other than an unknown tool: the tool is here
   * but the *pane* is not addressable — a second `files#2` pane written by a
   * version where Files was plural, or a `terminal#1` whose id and
   * `contentComponent` disagree. Reported separately because the sentence is
   * different: nothing was removed from Workbench, this one arrangement said
   * something this Workbench cannot mean.
   */
  droppedPanes: string[];
}

type Json = Record<string, unknown>;

const isRecord = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * Vet a persisted layout against the panes this app can actually address.
 *
 * Returns null when the value is not a layout at all (corrupt, or from a
 * version whose shape we do not know) or when nothing usable survived — both
 * mean "use the default arrangement". Otherwise the layout with every unusable
 * panel removed: empty groups collapse, empty branches collapse, a dropped
 * active view is forgotten, and the whole arrangement is kept.
 *
 * **Two ways a panel can be unusable**, and they are not the same event:
 *
 *  - its `contentComponent` names a tool that is gone (removed, renamed, or
 *    gated off by `when`). dockview would create the panel anyway, hand React
 *    `undefined` as the element type, and take the window down on render;
 *  - the tool is here but the *pane id* is not one this Workbench can mean:
 *    it carries an instance key for a tool that is a singleton today, or its id
 *    and its component disagree about which tool it hosts. That pane is
 *    unreachable by every pane command — nothing can focus it, split it or
 *    close it — and a second copy of a singleton renders the same state twice.
 *    A file written by a newer version, or hand-edited, produces exactly this.
 *
 * Both are handled the same way and reported separately (`PrunedLayout`),
 * because an unknown *instance* costs one pane while an unknown *tool* is a
 * capability the user no longer has.
 *
 * `maximizedNode` is a *path* into the grid, so it is only carried over when
 * nothing was dropped — after a structural change it could point at a different
 * panel, and restoring the wrong panel full screen is worse than not restoring
 * focus mode at all. (It is also why this walks a plain JSON tree rather than
 * dockview's `SerializedDockview`: `toJSON` writes that key and the published
 * type does not declare it.)
 */
export function pruneLayout(value: unknown, known: PaneVocabulary): PrunedLayout | null {
  if (!isRecord(value)) return null;
  const grid = value.grid;
  const panels = value.panels;
  if (!isRecord(grid) || !isRecord(panels) || !isRecord(grid.root)) return null;

  const dropped: string[] = [];
  const droppedPanes: string[] = [];
  const kept: Json = {};
  for (const [id, panel] of Object.entries(panels)) {
    if (!isRecord(panel)) continue;
    const component = panel.contentComponent;
    if (typeof component !== "string") continue;
    if (!known.components.has(component)) {
      if (!dropped.includes(component)) dropped.push(component);
      continue;
    }
    if (!addressablePane(id, component, known)) {
      if (!droppedPanes.includes(id)) droppedPanes.push(id);
      continue;
    }
    kept[id] = panel;
  }
  if (Object.keys(kept).length === 0) return null;

  const root = pruneNode(grid.root, kept);
  if (root === null) return null;

  const prunedGrid: Json = { ...grid, root };
  // Only a structurally untouched grid may keep its maximized-node path.
  if (dropped.length + droppedPanes.length > 0) delete prunedGrid.maximizedNode;

  const layout: Json = { ...value, grid: prunedGrid, panels: kept };
  const survivingGroups = groupIds(root);
  if (typeof value.activeGroup === "string" && !survivingGroups.has(value.activeGroup)) {
    delete layout.activeGroup;
  }
  for (const key of ["floatingGroups", "popoutGroups"] as const) {
    const groups = value[key];
    if (!Array.isArray(groups)) continue;
    const surviving = groups.filter((group) => {
      if (!isRecord(group) || pruneGroup(group.data, kept) === null) return false;
      // A popped-out pane re-grids into the group it names on close/restore, so
      // one whose `gridReferenceGroup` did not survive pruning has nowhere to go
      // back to — dockview would fail to restore it. Drop it with the panels it
      // held; a floating group carries no such reference and keeps this pass.
      if (key === "popoutGroups" && typeof group.gridReferenceGroup === "string") {
        return survivingGroups.has(group.gridReferenceGroup);
      }
      return true;
    });
    if (surviving.length === 0) delete layout[key];
    else layout[key] = surviving.map((group) => reviseFloating(group as Json, kept));
  }
  // The one cast in the module: dockview's published `SerializedDockview` omits
  // `grid.maximizedNode`, which its own serializer writes and its deserializer
  // reads, so the tree above is intentionally wider than the declared type.
  return { layout: layout as unknown as SerializedDockview, dropped, droppedPanes };
}

/**
 * Can this app address the pane this id names?
 *
 * The id has to agree with the panel's component about which tool it hosts, and
 * only a tool that is plural *today* may carry an instance key. Everything else
 * about the key — whether that session still exists, whether the file is still
 * on disk — is the pane's own business, deliberately: sessions and files load
 * long after the layout does, so a restore that vetted them would drop panes
 * for being early rather than for being wrong.
 */
function addressablePane(id: string, component: string, known: PaneVocabulary): boolean {
  const { toolId, instance } = parsePaneId(id);
  if (toolId !== component) return false;
  return instance === null || known.plural.has(component);
}

function reviseFloating(group: Json, kept: Json): Json {
  const data = pruneGroup(group.data, kept);
  return data === null ? group : { ...group, data };
}

/** One grid node, pruned. null = nothing left in it. */
function pruneNode(node: unknown, kept: Json): Json | null {
  if (!isRecord(node)) return null;
  if (node.type === "branch") {
    if (!Array.isArray(node.data)) return null;
    const children = node.data
      .map((child) => pruneNode(child, kept))
      .filter((child): child is Json => child !== null);
    return children.length === 0 ? null : { ...node, data: children };
  }
  const data = pruneGroup(node.data, kept);
  return data === null ? null : { ...node, data };
}

/** One group's view list, pruned. null = the group has no panels left. */
function pruneGroup(data: unknown, kept: Json): Json | null {
  if (!isRecord(data) || !Array.isArray(data.views)) return null;
  const views = data.views.filter((view) => typeof view === "string" && view in kept);
  if (views.length === 0) return null;
  const group: Json = { ...data, views };
  if (typeof group.activeView !== "string" || !views.includes(group.activeView)) {
    delete group.activeView;
  }
  return group;
}

function groupIds(node: Json, into: Set<string> = new Set()): Set<string> {
  if (node.type === "branch" && Array.isArray(node.data)) {
    for (const child of node.data) if (isRecord(child)) groupIds(child, into);
    return into;
  }
  if (isRecord(node.data) && typeof node.data.id === "string") into.add(node.data.id);
  return into;
}

/** Sentence for the toast raised when a restore lost panels. Singular/plural
 * matters here: a user reading "1 panels" stops believing the rest of it. */
export function droppedPanelsMessage(dropped: readonly string[]): string {
  const what = dropped.length === 1 ? "panel is" : "panels are";
  return `Restored your layout without ${dropped.join(", ")} — that ${what} no longer part of Workbench.`;
}

/** Sentence for the other half: the tools are all here, these *panes* were not
 * ones this Workbench can address (`addressablePane`). */
export function droppedPanesMessage(dropped: readonly string[]): string {
  const what = dropped.length === 1 ? "pane" : "panes";
  return `Restored your layout without ${String(dropped.length)} ${what} this Workbench cannot open (${dropped.join(", ")}).`;
}
