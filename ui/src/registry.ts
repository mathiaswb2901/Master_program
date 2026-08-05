/**
 * The tool registry: what a Workbench capability *is*.
 *
 * A tool declares, in one descriptor next to its own code, everything the shell
 * needs to host it — a dockview panel (with its tab's glyph and badge), a
 * document view, commands, the default chords for those commands, status-bar
 * items, and which `shortcuts.md` kinds it hosts. Nothing about a capability is
 * spelled out in a shared file any more: `App.tsx` names no panel,
 * `commands.ts` holds no panel-specific action, `StatusBar.tsx` knows no
 * session chip. Adding one costs a new module plus one line in `tools.ts`.
 *
 * The agent-facing tools a capability adds are *not* declared here. That
 * registry is `server/src/workbench_server/services/agent_tools.py` — the one
 * the SDK actually reads — and a second copy on this side would be another
 * authority to keep honest with nothing reading it (ARCHITECTURE.md).
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

import type { DockviewApi, IDockviewHeaderActionsProps, IDockviewPanelProps } from "dockview";
import type { FunctionComponent } from "react";

import type { Command } from "./commands";
import { paneId, parsePaneId } from "./panes";
import type { OpenFile } from "./store";
import type { ShortcutKind } from "./types";

// ---- contributions ----------------------------------------------------------

/** Where a panel docks. `center` is the reference the others are placed against. */
export type PanelArea = "center" | "left" | "right" | "bottom";

export interface PanelLocation {
  area: PanelArea;
  /** Initial width (left/right) or height (bottom), in px. */
  size?: number;
}

/**
 * One thing a plural tool could put in a new pane — a row in the pane picker.
 *
 * `key` is a **thunk** because the two kinds of row are the same kind of row:
 * "session `abc`" answers with a key it already has, while "New terminal" mints
 * one, and "New agent session" has to go and create the session first. The
 * caller awaits it and places `toolId#<key>` (`ui/src/panels/Panes.tsx`).
 * Answering `null` means the row decided not to open anything after all — a
 * session that failed to start, for instance — and the split is abandoned
 * rather than bound to nothing.
 */
export interface PaneInstanceOption {
  /** Stable row identity for React and for the picker's filter. */
  id: string;
  title: string;
  detail?: string;
  /** Section header in the picker (DESIGN.md §6.5 categories). */
  category: string;
  key: () => string | null | Promise<string | null>;
}

/** An option with the tool it belongs to — which is all the picker needs to
 * turn a row into a pane id (`paneId(toolId, await key())`). */
export interface PaneChoice extends PaneInstanceOption {
  toolId: string;
  /**
   * This row *is* the tool's default pane, so a null key means the bare tool
   * id and not a failure. Set by the derivation below, never by a tool: it is
   * what lets the picker tell "you asked for the Agent panel" apart from "the
   * session I was going to bind this pane to could not be created".
   */
  defaultPane?: boolean;
}

/**
 * How a tool is *plural*: what a new pane of it can be bound to, and what a
 * pane calls itself once it is.
 *
 * A tool that sets `singleton: false` without this still opens more than one
 * pane — it just has nothing to offer the picker and no title for a restored
 * instance, which is why every shipped plural tool declares it.
 */
export interface PaneInstances {
  /** Rows the pane picker offers for this tool, most useful first. */
  options: () => readonly PaneInstanceOption[];
  /** Title for a pane bound to this key — including one restored from disk
   * whose binding no longer resolves, which must still read as something. */
  titleFor: (key: string) => string;
}

export interface PanelContribution {
  component: FunctionComponent<IDockviewPanelProps>;
  defaultLocation: PanelLocation;
  /** In the startup layout. False = opened on demand by one of its commands,
   * which is also what makes its tab closable (see `panelTabInfo`). */
  openByDefault?: boolean;
  /** One instance only — opening again focuses it. Default true. */
  singleton?: boolean;
  /** What a *second, third, N-th* pane of this tool is bound to. Meaningless
   * on a singleton; required in practice on anything that is not (`panes.ts`). */
  instances?: PaneInstances;
  /**
   * Rendered after the tab title: one aggregate signal the tool owns, for a
   * state worth seeing while the panel is behind another (DESIGN.md §6.4 —
   * dot-only, never a count). The Agent's "a session needs attention" dot is
   * the shipped one; it lives here so the tab component names no panel.
   */
  badge?: FunctionComponent;
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
 * A tool's command, minus its chords: those live in the descriptor's
 * `shortcuts` table so a tool's keymap is one readable block, and so a user
 * keymap file later overrides exactly that layer (M5 item 3).
 */
export type ToolCommand = Omit<Command, "keys">;

/**
 * Commands that come and go while the app runs — one per saved layout today,
 * one per recent workspace tomorrow. Kept separate from `commands` because the
 * static list is built once (it is read on every keystroke) and because a chord
 * has to be declared statically to be pinned by a test and to lose a collision
 * to `shortcuts.md` deterministically. **Dynamic commands therefore carry no
 * chords**; a user binds one from `shortcuts.md` instead.
 */
export interface DynamicCommands {
  /** Cheap identity of the current set. Re-read on every keystroke, so this
   * must be a string comparison, never a rebuild. */
  key: () => string;
  build: () => readonly ToolCommand[];
}

/** Joins the tools' keys in `dynamicCommandsKey`. A control byte, so no key a
 * tool composes out of user-visible names can forge a different combination. */
const KEY_SEPARATOR = "\u0001";

export interface WorkbenchTool {
  id: string;
  /** Panel tab title, focus-command label, docs. */
  title: string;
  /** Optional glyph for the panel tab. */
  icon?: FunctionComponent;
  panel?: PanelContribution;
  documentView?: DocumentViewContribution;
  commands?: readonly ToolCommand[];
  /** Commands whose *set* changes at runtime (see `DynamicCommands`). */
  dynamicCommands?: DynamicCommands;
  /** Default chords per command id — the tool's whole keymap, in one place. */
  shortcuts?: Readonly<Record<string, readonly string[]>>;
  statusContributions?: readonly StatusContribution[];
  /**
   * Which `shortcuts.md` kinds this tool's panel hosts: an entry of that kind
   * is inserted there, and the panel is brought forward first. Declared here so
   * the router in `commands.ts` names no panel — the Terminal claims `shell`,
   * the Agent `prompt`, and a tool that replaces either claims it back.
   */
  shortcutKinds?: readonly ShortcutKind[];
  /**
   * `shortcuts.md` kinds this tool *carries out* instead of inserting into a
   * panel. The Layouts tool claims `layout`, whose body is the name of an
   * arrangement — the one entry kind that acts, and the reason it may is that
   * moving panels is all it can do (`docs/shortcuts.md`).
   *
   * A kind with no handler falls back to insertion, so removing the tool that
   * claims one degrades to "nothing happens", never to the wrong surface.
   */
  shortcutActions?: Readonly<Partial<Record<ShortcutKind, (body: string) => void>>>;
  /**
   * The live dockview handle, for a tool that operates on the dock itself
   * rather than living inside it — layout restore, persistence and focus mode
   * are the ones. Called once when the dock is ready, and with `null` when it
   * goes away. Everything else should be reaching for `openToolPanel`.
   */
  onDockReady?: (api: DockviewApi | null) => void;
  /**
   * A control at the right end of **every pane's** tab strip, for a tool that
   * acts on panes rather than living in one — the split affordance is the one
   * (DESIGN.md §6.11). Contributed here so `App.tsx` hands dockview a component
   * without naming the capability that drew it; the component decides for
   * itself which panes it appears on (`isGroupActive`).
   */
  groupActions?: FunctionComponent<IDockviewHeaderActionsProps>;
  /**
   * Whether this tool is present at all: false takes out its panel, its
   * commands and its status items together.
   *
   * **Evaluated once, when the registry is first derived** (module load), not
   * live — the panel components handed to dockview, the startup layout and the
   * built-in command list are each built once, so a predicate that flips later
   * changes nothing on screen. Gate on facts that are settled by then: a build
   * flag, the host (`isTauri()`), a capability compiled in or not. Anything
   * that changes while the app runs belongs on a *command's* own `when`, which
   * is re-read on every keystroke, or inside the panel itself — which is how
   * the Office tool handles a document server that may not answer.
   */
  when?: () => boolean;
}

// ---- derivations (pure; every one takes the tool array) ----------------------

/**
 * `when`, asked once per tool and remembered — the documented boot-time
 * semantics made true by construction rather than by convention.
 *
 * The consumers snapshot at different moments: dockview gets its components
 * once, the layout is applied once, the command list is built once, while the
 * status bar re-derives on every render. Were the predicate re-read, a tool
 * that enabled itself afterwards would be *half* present — a status item and a
 * QuickBar row for a panel dockview was never told about, and a focus command
 * that never existed. One answer for the life of the array is the only
 * consistent one, and it is what the field's doc comment promises.
 */
const enabled = new WeakMap<WorkbenchTool, boolean>();

function isEnabled(tool: WorkbenchTool): boolean {
  const known = enabled.get(tool);
  if (known !== undefined) return known;
  const answer = tool.when?.() !== false;
  enabled.set(tool, answer);
  return answer;
}

/**
 * Every command of every enabled tool, with its chords resolved from the
 * owning tool's `shortcuts` table. A command's own `when` is left untouched —
 * that one *is* live, and the QuickBar and keymap re-read it on every
 * keystroke.
 */
export function toolCommands(tools: readonly WorkbenchTool[]): Command[] {
  const commands: Command[] = [];
  for (const tool of tools.filter(isEnabled)) {
    for (const command of tool.commands ?? []) {
      const keys = tool.shortcuts?.[command.id];
      commands.push({
        ...command,
        ...(keys !== undefined && keys.length > 0 ? { keys: [...keys] } : {}),
      });
    }
  }
  return commands;
}

/**
 * Every dynamic command of every enabled tool, in registry order. Never carries
 * a chord (see `DynamicCommands`), so a dynamic command can shadow nothing.
 */
export function toolDynamicCommands(tools: readonly WorkbenchTool[]): Command[] {
  return tools
    .filter(isEnabled)
    .flatMap((tool) => [...(tool.dynamicCommands?.build() ?? [])])
    .map((command) => ({ ...command }));
}

/** Identity of the current dynamic command set, for memoizing the merged list.
 * Read on every keystroke: it must stay a string join, not a rebuild. Keys are
 * separated so two tools' keys cannot run together into a third combination's. */
export function dynamicCommandsKey(tools: readonly WorkbenchTool[]): string {
  return tools
    .filter(isEnabled)
    .map((tool) => (tool.dynamicCommands === undefined ? "" : tool.dynamicCommands.key()))
    .join(KEY_SEPARATOR);
}

/** What carries out a `shortcuts.md` entry of this kind instead of inserting
 * it, or null — in which case the entry is inserted as it always was. */
export function shortcutAction(
  tools: readonly WorkbenchTool[],
  kind: ShortcutKind,
): ((body: string) => void) | null {
  for (const tool of tools.filter(isEnabled)) {
    const action = tool.shortcutActions?.[kind];
    if (action !== undefined) return action;
  }
  return null;
}

/** Hand the live dock to every tool that asked for it (`null` on teardown). */
export function notifyDockReady(
  tools: readonly WorkbenchTool[],
  api: DockviewApi | null,
): void {
  for (const tool of tools.filter(isEnabled)) tool.onDockReady?.(api);
}

/** The panel a `shortcuts.md` entry of this kind is typed into, or null if no
 * enabled tool claims the kind (then the insertion goes wherever the store
 * sends it, without stealing focus). */
export function shortcutHost(
  tools: readonly WorkbenchTool[],
  kind: ShortcutKind,
): string | null {
  const host = panelTools(tools).find((tool) => tool.shortcutKinds?.includes(kind) === true);
  return host?.id ?? null;
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

/** Tools that may have more than one pane, by id. */
export function pluralPanelIds(tools: readonly WorkbenchTool[]): Set<string> {
  return new Set(
    panelTools(tools)
      .filter((tool) => tool.panel?.singleton === false)
      .map((tool) => tool.id),
  );
}

/**
 * What a pane id is allowed to say, right now.
 *
 * `components` is what dockview can render; `plural` is which of those may
 * carry an instance key. Both are registry facts, read fresh — a persisted
 * layout is vetted against them before dockview is allowed near it
 * (`layouts.ts`).
 */
export interface PaneVocabulary {
  components: ReadonlySet<string>;
  plural: ReadonlySet<string>;
}

export function paneVocabulary(tools: readonly WorkbenchTool[]): PaneVocabulary {
  return { components: new Set(Object.keys(panelComponents(tools))), plural: pluralPanelIds(tools) };
}

/**
 * Every row the pane picker offers, in registry order.
 *
 * **Every** tool with a panel gets a row for its default pane, plural or not —
 * that row is the way back to a pane you closed, and without it closing the
 * Terminal panel would leave "Switch to the Default layout" as the only route
 * to another one. A plural tool then adds a row per thing it can be bound to
 * (each live session, each open file, a new terminal).
 */
export function paneInstanceOptions(tools: readonly WorkbenchTool[]): PaneChoice[] {
  return panelTools(tools).flatMap((tool) => {
    const base: PaneChoice = {
      toolId: tool.id,
      id: tool.id,
      title: tool.title,
      category: "Panels",
      key: () => null,
      defaultPane: true,
    };
    const instances = tool.panel?.singleton === false ? tool.panel.instances : undefined;
    if (instances === undefined) return [base];
    return [base, ...instances.options().map((option) => ({ ...option, toolId: tool.id }))];
  });
}

/**
 * What a pane calls itself. A default pane is its tool's title; an instance
 * pane asks the tool, which is also what keeps a *restored* pane readable when
 * its binding no longer resolves ("Session gone" beats a raw id).
 */
export function paneTitle(tools: readonly WorkbenchTool[], id: string): string {
  const { toolId, instance } = parsePaneId(id);
  const tool = panelTools(tools).find((candidate) => candidate.id === toolId);
  if (tool === undefined) return id;
  if (instance === null) return tool.title;
  return tool.panel?.instances?.titleFor(instance) ?? `${tool.title} ${instance}`;
}

/** The one control every pane's tab strip carries, or null. First enabled tool
 * that contributes one wins — there is room for exactly one. */
export function groupActionsComponent(
  tools: readonly WorkbenchTool[],
): FunctionComponent<IDockviewHeaderActionsProps> | undefined {
  return tools.filter(isEnabled).find((tool) => tool.groupActions !== undefined)?.groupActions;
}

export interface PanelTabInfo {
  /** Glyph before the title. */
  icon?: FunctionComponent;
  /** Aggregate signal after the title (see `PanelContribution.badge`). */
  badge?: FunctionComponent;
  /**
   * Whether the tab carries a close button. True for exactly the panels that
   * are *not* in the startup layout: one arrived because a command opened it,
   * so the tab it arrived on is the way back. A startup panel stays chrome —
   * closing one is not how you rearrange the window; "Reset layout" and the
   * named layouts are (`ui/src/panels/Layouts.tsx`).
   */
  closable: boolean;
}

/** What the panel tab renders, for a dockview panel's `component` key. Unknown
 * components (nothing registered under that id) get bare chrome. */
export function panelTabInfo(
  tools: readonly WorkbenchTool[],
  component: string,
): PanelTabInfo {
  const tool = panelTools(tools).find((candidate) => candidate.id === component);
  return {
    ...(tool?.icon !== undefined ? { icon: tool.icon } : {}),
    ...(tool?.panel?.badge !== undefined ? { badge: tool.panel.badge } : {}),
    closable: tool?.panel?.openByDefault === false,
  };
}

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

/** One panel's placement, at its own default location unless told otherwise. */
export function placementOf(tool: WorkbenchTool, location?: PanelLocation): PanelPlacement {
  return {
    id: tool.id,
    component: tool.id,
    title: tool.title,
    location: location ?? tool.panel?.defaultLocation ?? { area: "center" },
  };
}

/**
 * Creation order: `center` panels first, because everything else is placed
 * against the first of them. Stable, so registry (or preset) order survives.
 */
export function orderPlacements(placements: readonly PanelPlacement[]): PanelPlacement[] {
  const rank = (placement: PanelPlacement): number =>
    placement.location.area === "center" ? 0 : 1;
  return [...placements].sort((a, b) => rank(a) - rank(b));
}

/**
 * The startup layout, as data — which is what makes the arrangement a property
 * of the descriptors instead of a sequence of `addPanel` calls in `App.tsx`.
 */
export function defaultLayout(tools: readonly WorkbenchTool[]): PanelPlacement[] {
  return orderPlacements(layoutTools(tools).map((tool) => placementOf(tool)));
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
    title: `Focus ${tool.title} panel`,
    ...(index < 9 ? { keys: [`Ctrl+${String(index + 1)}`] } : {}),
    run: () => focus(tool.id),
  }));
}

/**
 * The view for each open-file kind, in registry order — **one per kind**.
 *
 * Two tools may offer a view for the same kind, and that is the seam rather
 * than a collision: the native Office host claims `office` by registering
 * before the OnlyOffice tool, and renders OnlyOffice itself wherever it cannot
 * dock a real window. Earliest registration wins, which is the same rule
 * `documentViewFor` gives, and deduplicating *here* is what keeps the two
 * consistent — without it a `keepMounted` kind would be mounted twice, one
 * hidden editor per losing tool.
 */
export function documentViews(
  tools: readonly WorkbenchTool[],
): DocumentViewContribution[] {
  const byKind = new Map<OpenFile["kind"], DocumentViewContribution>();
  for (const tool of tools.filter(isEnabled)) {
    const view = tool.documentView;
    if (view !== undefined && !byKind.has(view.kind)) byKind.set(view.kind, view);
  }
  return [...byKind.values()];
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

/**
 * Add these placements to an empty dock, first one as the reference, and leave
 * it active. The one way an arrangement is built from descriptors — the startup
 * layout and every layout preset go through here.
 */
export function applyPlacements(api: DockviewApi, placements: readonly PanelPlacement[]): void {
  const first = placements[0];
  if (first === undefined) return;
  api.addPanel({ id: first.id, component: first.component, title: first.title });
  for (const placement of placements.slice(1)) addPlacement(api, placement, first.id);
  api.getPanel(first.id)?.api.setActive();
}

/** Build the startup layout and leave the reference panel active. */
export function applyDefaultLayout(api: DockviewApi, tools: readonly WorkbenchTool[]): void {
  applyPlacements(api, defaultLayout(tools));
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
  // A second pane of a plural tool is a pane *id*, not an ad-hoc string: the id
  // is the whole of what a restart gets back (`panes.ts`), so even the one this
  // command mints has to be in that vocabulary.
  const id = existing === undefined ? toolId : paneId(toolId, String(Date.now()));
  const placement: PanelPlacement = {
    id,
    component: toolId,
    title: tool.title,
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
