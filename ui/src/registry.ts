/**
 * The tool registry: what a Workbench capability *is*.
 *
 * A tool declares, in one descriptor next to its own code, everything the shell
 * needs to host it — a dockview panel, a document view, commands, the default
 * chords for those commands, status-bar items, and the agent-facing tools it
 * adds to a session's context. Nothing about a capability is spelled out in a
 * shared file any more: `App.tsx` names no panel, `commands.ts` holds no
 * panel-specific action, `StatusBar.tsx` knows no session chip. Adding one costs
 * a new module plus one line in `tools.ts`.
 *
 * Registration is **static** — `TOOLS` is an array assembled from per-tool
 * modules, so the bundler still sees every import and `tsc` still type-checks
 * every descriptor. The shape is chosen so that a later dynamic loader can hand
 * the same object in from somewhere else (see ARCHITECTURE.md, "Tool registry"):
 * everything here is data or a plain function, and no derivation below reads
 * `TOOLS` itself — they all take the array, which is what makes them testable
 * with a fixture and what makes a second, dynamically-sourced array possible.
 *
 * This module is deliberately free of React, the store, dockview's runtime and
 * `TOOLS` (types only), so the derivations are unit-tested rather than assumed.
 */

import type { DockviewApi, IDockviewPanelProps } from "dockview";
import type { FunctionComponent } from "react";

import type { Command } from "./commands";
import type { OpenFile } from "./store";

// ---- contributions ----------------------------------------------------------

/** Where a panel docks. `center` is the reference the others are placed against. */
export type PanelArea = "center" | "left" | "right" | "bottom";

export interface PanelLocation {
  area: PanelArea;
  /** Initial width (left/right) or height (bottom), in px. */
  size?: number;
}

export interface PanelContribution {
  component: FunctionComponent<IDockviewPanelProps>;
  defaultLocation: PanelLocation;
  /** Tab title; defaults to the tool's title. */
  title?: string;
  /** In the startup layout. False = opened on demand by one of its commands. */
  openByDefault?: boolean;
  /** One instance only — opening again focuses it. Default true. */
  singleton?: boolean;
}

/**
 * How a tool renders one *kind* of open file inside the editor area. This is
 * the seam the Office host panel replaces OnlyOffice through: the editor area
 * asks the registry what renders an open file, and knows nothing else about it.
 */
export interface DocumentViewContribution {
  kind: OpenFile["kind"];
  component: FunctionComponent<{ file: OpenFile }>;
  /** Wrapper class the editor area puts around it (`is-hidden` is appended). */
  hostClassName: string;
  /**
   * Keep every open file of this kind mounted for the life of its tab, showing
   * only the active one. For editors that are expensive to create (OnlyOffice)
   * a tab switch must not be a teardown.
   */
  keepMounted?: boolean;
}

export type StatusRegion = "left" | "center" | "right";

export interface StatusContribution {
  region: StatusRegion;
  component: FunctionComponent;
}

/**
 * Output shape an agent-facing tool returns. Required on every declaration, so
 * omitting it is a type error rather than an unmeasured cost — the UI half of
 * the ergonomics budget (`server/.../services/agent_tools.py` is the half the
 * SDK actually reads, and carries the same ceilings as tests).
 */
export type AgentToolOutputFormat = "compact-json" | "text" | "markdown";

export interface AgentToolDeclaration {
  name: string;
  /** One line, for humans reading the registry — not the model-facing text. */
  description: string;
  outputFormat: AgentToolOutputFormat;
}

/**
 * A tool's command, minus its chords: those live in the descriptor's
 * `shortcuts` table so a tool's keymap is one readable block, and so a user
 * keymap file later overrides exactly that layer (M5 item 3).
 */
export type ToolCommand = Omit<Command, "keys">;

export interface WorkbenchTool {
  id: string;
  /** Panel tab title, focus-command label, docs. */
  title: string;
  /** Optional glyph for the panel tab. */
  icon?: FunctionComponent;
  panel?: PanelContribution;
  documentView?: DocumentViewContribution;
  commands?: readonly ToolCommand[];
  /** Default chords per command id — the tool's whole keymap, in one place. */
  shortcuts?: Readonly<Record<string, readonly string[]>>;
  statusContributions?: readonly StatusContribution[];
  /** False takes the whole tool out: no panel, no commands, no status items. */
  when?: () => boolean;
  agentTools?: readonly AgentToolDeclaration[];
}

// ---- derivations (pure; every one takes the tool array) ----------------------

const isEnabled = (tool: WorkbenchTool): boolean => tool.when?.() !== false;

/** `a && b`, with `undefined` meaning "always". */
function bothApply(
  a: (() => boolean) | undefined,
  b: (() => boolean) | undefined,
): (() => boolean) | undefined {
  if (a === undefined) return b;
  if (b === undefined) return a;
  return () => a() && b();
}

/**
 * Every registered command, with its chords resolved from the owning tool's
 * `shortcuts` table.
 *
 * A tool's `when` is folded into each of its commands rather than filtering the
 * list here: the result is then static (safe to build once and cache) while the
 * predicate is still evaluated live by the QuickBar and the keymap.
 */
export function toolCommands(tools: readonly WorkbenchTool[]): Command[] {
  const commands: Command[] = [];
  for (const tool of tools) {
    for (const command of tool.commands ?? []) {
      const keys = tool.shortcuts?.[command.id];
      commands.push({
        ...command,
        when: bothApply(tool.when, command.when),
        ...(keys !== undefined && keys.length > 0 ? { keys: [...keys] } : {}),
      });
    }
  }
  return commands;
}

/** Chord ids named by a `shortcuts` table that no command of that tool owns.
 * A typo there would silently drop a binding, so a test fails on a non-empty
 * result rather than the chord quietly not existing. */
export function danglingShortcutIds(tools: readonly WorkbenchTool[]): string[] {
  const dangling: string[] = [];
  for (const tool of tools) {
    const owned = new Set((tool.commands ?? []).map((command) => command.id));
    for (const id of Object.keys(tool.shortcuts ?? {})) {
      if (!owned.has(id)) dangling.push(`${tool.id}: ${id}`);
    }
  }
  return dangling;
}

/** Tools contributing a panel right now, in registry order. */
export function panelTools(tools: readonly WorkbenchTool[]): WorkbenchTool[] {
  return tools.filter((tool) => tool.panel !== undefined && isEnabled(tool));
}

/** `components` for `<DockviewReact>`: panel id -> its component. */
export function panelComponents(
  tools: readonly WorkbenchTool[],
): Record<string, FunctionComponent<IDockviewPanelProps>> {
  const components: Record<string, FunctionComponent<IDockviewPanelProps>> = {};
  for (const tool of panelTools(tools)) {
    if (tool.panel !== undefined) components[tool.id] = tool.panel.component;
  }
  return components;
}

export const panelTitle = (tool: WorkbenchTool): string => tool.panel?.title ?? tool.title;

export interface PanelPlacement {
  /** dockview panel id. Equal to `component` for every singleton panel. */
  id: string;
  /** Key into `panelComponents` — always the tool id. */
  component: string;
  title: string;
  location: PanelLocation;
}

/** Tools in the startup layout, in registry order (= panel-focus order). */
function layoutTools(tools: readonly WorkbenchTool[]): WorkbenchTool[] {
  return panelTools(tools).filter((tool) => tool.panel?.openByDefault !== false);
}

const placementOf = (tool: WorkbenchTool): PanelPlacement => ({
  id: tool.id,
  component: tool.id,
  title: panelTitle(tool),
  location: tool.panel?.defaultLocation ?? { area: "center" },
});

/**
 * The startup layout, as data, in creation order: `center` panels first —
 * everything else is placed against the first of them — which is what makes the
 * arrangement a property of the descriptors instead of a sequence of `addPanel`
 * calls in `App.tsx`.
 */
export function defaultLayout(tools: readonly WorkbenchTool[]): PanelPlacement[] {
  const placements = layoutTools(tools).map(placementOf);
  const rank = (placement: PanelPlacement): number =>
    placement.location.area === "center" ? 0 : 1;
  return placements.sort((a, b) => rank(a) - rank(b));
}

/**
 * Focus commands for the panels in the startup layout: `Ctrl+1..9` in registry
 * order. Derived, not a fixed list — the four defaults are simply the first
 * four registered panels, and a fifth would get `Ctrl+5` by existing.
 */
export function panelFocusCommands(
  tools: readonly WorkbenchTool[],
  focus: (id: string) => void,
): Command[] {
  return layoutTools(tools).map((tool, index) => ({
    id: `panel.${tool.id}`,
    title: `Focus ${panelTitle(tool)} panel`,
    ...(index < 9 ? { keys: [`Ctrl+${String(index + 1)}`] } : {}),
    run: () => focus(tool.id),
  }));
}

/** Views for the open-file kinds, in registry order. */
export function documentViews(
  tools: readonly WorkbenchTool[],
): DocumentViewContribution[] {
  return tools
    .filter(isEnabled)
    .map((tool) => tool.documentView)
    .filter((view): view is DocumentViewContribution => view !== undefined);
}

export function documentViewFor(
  tools: readonly WorkbenchTool[],
  kind: OpenFile["kind"],
): DocumentViewContribution | null {
  return documentViews(tools).find((view) => view.kind === kind) ?? null;
}

export interface StatusItem {
  /** Stable React key: one tool may contribute to more than one region. */
  key: string;
  component: FunctionComponent;
}

/** Status-bar items for one region, in registry order. */
export function statusItems(
  tools: readonly WorkbenchTool[],
  region: StatusRegion,
): StatusItem[] {
  const items: StatusItem[] = [];
  for (const tool of tools) {
    if (!isEnabled(tool)) continue;
    // Keyed by the contribution's index in the tool's own list, not in the
    // filtered one, so a tool's items keep their identity when it gains one in
    // another region.
    (tool.statusContributions ?? []).forEach((contribution, index) => {
      if (contribution.region !== region) return;
      items.push({
        key: `${tool.id}:${region}:${String(index)}`,
        component: contribution.component,
      });
    });
  }
  return items;
}

/** Every agent-facing tool the registry declares, in registry order. */
export function agentToolDeclarations(
  tools: readonly WorkbenchTool[],
): AgentToolDeclaration[] {
  return tools.filter(isEnabled).flatMap((tool) => [...(tool.agentTools ?? [])]);
}

// ---- dockview application (thin: the placements above, applied) -------------

const DIRECTIONS = { left: "left", right: "right", bottom: "below" } as const;

function addPlacement(api: DockviewApi, placement: PanelPlacement, reference: string): void {
  const { area, size } = placement.location;
  const common = {
    id: placement.id,
    component: placement.component,
    title: placement.title,
  };
  if (area === "center") {
    api.addPanel({ ...common, position: { referencePanel: reference, direction: "within" } });
    return;
  }
  api.addPanel({
    ...common,
    position: { referencePanel: reference, direction: DIRECTIONS[area] },
    ...(area === "bottom" ? { initialHeight: size } : { initialWidth: size }),
  });
}

/** Build the startup layout and leave the reference panel active. */
export function applyDefaultLayout(api: DockviewApi, tools: readonly WorkbenchTool[]): void {
  const placements = defaultLayout(tools);
  const first = placements[0];
  if (first === undefined) return;
  api.addPanel({ id: first.id, component: first.component, title: first.title });
  for (const placement of placements.slice(1)) addPlacement(api, placement, first.id);
  api.getPanel(first.id)?.api.setActive();
}

/**
 * Open (or focus) a registered panel that is not in the startup layout — how a
 * tool whose panel is opened on demand gets on screen. A singleton panel that
 * already exists is brought forward rather than duplicated.
 */
export function openToolPanel(
  api: DockviewApi,
  tools: readonly WorkbenchTool[],
  toolId: string,
): void {
  const tool = panelTools(tools).find((candidate) => candidate.id === toolId);
  if (tool?.panel === undefined) return;
  const existing = api.getPanel(toolId);
  if (existing !== undefined && tool.panel.singleton !== false) {
    existing.api.setActive();
    return;
  }
  const id = existing === undefined ? toolId : `${toolId}:${String(Date.now())}`;
  const placement: PanelPlacement = {
    id,
    component: toolId,
    title: panelTitle(tool),
    location: tool.panel.defaultLocation,
  };
  const reference = defaultLayout(tools)[0]?.id;
  if (reference === undefined || api.getPanel(reference) === undefined) {
    api.addPanel({ id, component: toolId, title: placement.title });
  } else {
    addPlacement(api, placement, reference);
  }
  api.getPanel(id)?.api.setActive();
}
