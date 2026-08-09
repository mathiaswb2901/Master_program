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
  /**
   * Offered but not choosable, with `detail` saying why.
   *
   * For a limit the tool knows about *before* the gesture — the agent's
   * concurrent-session ceiling is the shipped one. The row stays where it is:
   * removing it answers "where did New agent session go?" with nothing, and
   * letting it be picked spends a split on a round trip that will be refused.
   */
  disabled?: boolean;
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
  /**
   * Which ground this view draws on — and therefore whether the editor frame
   * around it becomes a **mat** (`is-document`, DESIGN.md §6.1).
   *
   * `"paper"` (the default, so nothing already registered changes) is for a
   * canvas we do **not** draw: the OnlyOffice iframe, the real Word window. The
   * paper doctrine exists precisely because we cannot theme those — DESIGN.md
   * §2.8 says never to dark-skin the document — so the frame is built to make a
   * white page read as a sheet laid on a grey mat rather than a hole in the
   * window.
   *
   * `"app"` is for a document this app renders itself, with its own tokens, in
   * whichever theme the window is in. Monaco is the precedent (§2.8: the buffer
   * is `--surface-code` in both themes, not paper), and the notebook view is the
   * first document view to say so. Wrapping it in a mat would put a mid-grey
   * surround around a surface that is already the app's, and paint the tab strip
   * at mat value above a dark code well — two unrelated greys meeting, which is
   * the exact failure the mat exists to prevent.
   */
  surface?: "paper" | "app";
}

/**
 * A tool's answer to "the workspace is about to move — are you holding
 * anything?", in two halves so the user is asked before anything is done.
 */
export interface WorkspaceSwitchGuard {
  /**
   * What this tool is holding, named the way the user would name it (a file
   * name, not a path). Empty means nothing to settle, and is the answer that
   * lets a switch go straight through without a prompt.
   *
   * Read synchronously and possibly on every keystroke-driven re-render of the
   * confirm dialog, so it must be a cheap look at state already in memory.
   */
  held: () => string[];
  /**
   * Settle it, and resolve with what could **not** be settled.
   *
   * A non-empty result cancels the switch: the window stays where it is, with
   * the panel that can explain the failure still mounted. Resolving with `[]`
   * — including when there was nothing to do — lets the switch proceed. This
   * never rejects; a tool that cannot settle something says so by naming it.
   */
  settle: () => Promise<string[]>;
}

/**
 * A tool's veto over popping a pane into a separate window (M5 item 13).
 *
 * A popped-out pane is a separate WebView2 window with **no HWND in the main
 * window's parent chain** — so a native Office window docked into a pane cannot
 * follow it out, and reparenting it there would orphan a real Word behind an
 * invisible window (the exact class of bug the host-ownership rules exist to
 * prevent). The ROADMAP's exit criterion is "either follows or refuses with a
 * reason on screen"; this is the refusal.
 *
 * Modelled on `WorkspaceSwitchGuard`: the tool that *owns the resource* declares
 * the guard, and the pane capability asks the registry rather than reaching into
 * that tool's store — the office host is not even a panel tool, so nothing but
 * the registry knows to ask it about an `editors#…` pane holding a live document.
 */
export interface PopoutGuard {
  /**
   * A sentence explaining why this pane may not pop out, or null to allow it.
   *
   * Called once per panel in the group being popped out, with that panel's pane
   * id (`editors#report.docx`). Reads state already in memory — it runs on the
   * pop-out gesture, not on every render — and a non-null answer refuses the
   * whole group, so the sentence should name the way to unblock it.
   */
  blocks: (paneId: string) => string | null;
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
 * Command ids a `shortcuts.md` `command` entry must never bind, named here
 * because the tool that owns them lives in a module this layer does not edit
 * (`ui/src/panels/Workspaces.tsx`). The forward-looking mechanism is the
 * descriptor's `unsafeFromFile`, which a command declares on itself the day it
 * registers; this denylist is the safety net for the already-registered ones
 * that reach a filesystem path or re-point the path jail and cannot yet set the
 * flag at their source. `workspace.open` is ROADMAP item 5's named example:
 * a binding for it in a project's own `.workbench/shortcuts.md` would move the
 * path jail on one keystroke. `workspace.switch` and the dynamic
 * `workspace.open.<path>` recents are the same hazard.
 *
 * When those descriptors adopt `unsafeFromFile`, this set can shrink to empty
 * without touching `isBindableFromFile`'s contract.
 */
const UNSAFE_FROM_FILE_IDS: ReadonlySet<string> = new Set(["workspace.open", "workspace.switch"]);

/**
 * Prefixes of dynamic command *families* that are unsafe as a whole, because the
 * tool that mints them lives in a module this layer does not edit and so cannot
 * yet set `unsafeFromFile` at the source. Each family is denylisted rather than
 * each member, because the members come and go while the app runs:
 *  - `workspace.open.<path>` — the workspace recents, each of which re-points the
 *    path jail (`ui/src/panels/Workspaces.tsx`).
 *  - `layout.delete.<name>` — one per saved layout, and `deleteLayout` removes the
 *    named entry and `PUT`s `layouts.json`, an on-disk mutation
 *    (`ui/src/panels/Layouts.tsx`). Its sibling `layout.apply.<name>` is *not*
 *    here: switching a layout only moves panels — the same act the `layout`
 *    shortcut kind is deliberately allowed to carry out — so it stays bindable.
 */
const UNSAFE_FROM_FILE_PREFIXES: readonly string[] = ["workspace.open.", "layout.delete."];

/**
 * Whether a `shortcuts.md` `command` entry is allowed to bind this command.
 *
 * The bar is ROADMAP item 4's: a workspace file is untrusted input, so a command
 * that could reach a file must be unbindable from one. A command is refused if it
 * declares `unsafeFromFile`, is named in the denylist, or belongs to an unsafe
 * dynamic family; everything else — the overwhelming majority — is bindable, so a
 * newly registered safe command is bindable the day it registers, no code change.
 */
export function isBindableFromFile(command: Command): boolean {
  if (command.unsafeFromFile === true) return false;
  if (UNSAFE_FROM_FILE_IDS.has(command.id)) return false;
  return !UNSAFE_FROM_FILE_PREFIXES.some((prefix) => command.id.startsWith(prefix));
}

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
   * The workspace root moved (M5 item 5): drop whatever this tool cached about
   * the project that is gone, and read the new one.
   *
   * The seam exists so the switcher never has to know which capabilities keep
   * workspace-scoped state. The Layouts tool is the first user — the arrangement
   * lives in `<workspace>/.workbench/layouts.json`, so a switch means a
   * different file — and every later tool that reads something out of the
   * workspace (saved artifacts, a session browser's scope) implements it rather
   * than being wired into the switcher by hand.
   *
   * Called **after** the app-wide reset (`store.adoptWorkspace`), so the tree,
   * the sessions and the editors are already the new workspace's. `root` is
   * absolute and in the OS's own form.
   */
  onWorkspaceChanged?: (root: string) => void;
  /**
   * Live work this tool holds that a workspace switch would strand, and the way
   * to settle it — the other half of `onWorkspaceChanged`, asked *before* the
   * root moves rather than after.
   *
   * `onWorkspaceChanged` is for state a tool can simply re-read. This is for
   * state it cannot: a resource outside the browser that is still holding the
   * user's edits. The native Office host is the case that motivated it — a real
   * Word window with an unsaved paragraph in it is not a buffer this app can
   * save or discard, and it is not visible to the dirty-buffer check either,
   * because an `office` open file is never marked dirty (Word owns that
   * question, not Monaco). Left alone, the switch would unmount the panel, fire
   * a close nobody waited for, and take away the only surface that could have
   * said the close had failed.
   *
   * The switcher asks every tool rather than naming this one, for the same
   * reason it does not name Layouts: a capability that holds something across a
   * switch declares it, and the switcher stays a switcher.
   */
  workspaceSwitchGuard?: WorkspaceSwitchGuard;
  /**
   * Refuse to pop one of this tool's panes into a separate window, with a reason
   * (see `PopoutGuard`). The native Office host is the one user: a real Word
   * docked in a pane cannot follow the pane into a WebView2 window that is not
   * in the main window's parent chain, so it refuses rather than orphan it. The
   * pane capability consults the registry, never the office store directly.
   */
  popoutGuard?: PopoutGuard;
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

/** Tell every tool the workspace root moved. One tool throwing must not stop
 * the next from being told — a half-adopted window is the state where one panel
 * shows the new project and another still shows the old. */
export function notifyWorkspaceChanged(tools: readonly WorkbenchTool[], root: string): void {
  for (const tool of tools.filter(isEnabled)) {
    try {
      tool.onWorkspaceChanged?.(root);
    } catch (err) {
      console.error(`${tool.id} failed to adopt the new workspace`, err);
    }
  }
}

/** Everything the enabled tools are holding that a switch would strand, for the
 * sentence that asks the user about it. */
export function heldAcrossWorkspaceSwitch(tools: readonly WorkbenchTool[]): string[] {
  return tools.filter(isEnabled).flatMap((tool) => tool.workspaceSwitchGuard?.held() ?? []);
}

/**
 * Settle every guard, and report what would not settle.
 *
 * Sequential, not `Promise.all`: settling means closing real applications, and
 * two of them being asked to quit at once is how a "Save changes?" dialog ends
 * up behind another one where nobody sees it. A guard that throws counts as one
 * that could not settle — an exception is not consent to move the workspace.
 */
export async function settleBeforeWorkspaceSwitch(
  tools: readonly WorkbenchTool[],
): Promise<string[]> {
  const stranded: string[] = [];
  for (const tool of tools.filter(isEnabled)) {
    const guard = tool.workspaceSwitchGuard;
    if (guard === undefined) continue;
    try {
      stranded.push(...(await guard.settle()));
    } catch (err) {
      console.error(`${tool.id} could not settle before the workspace switch`, err);
      stranded.push(...guard.held());
    }
  }
  return stranded;
}

/**
 * Why the group holding these panes may not pop out, or null if it may.
 *
 * Asked of every enabled tool's `popoutGuard` for every pane in the group: the
 * office host vetoes an `editors#…` pane it holds a live window for, and it is
 * not a panel tool, so the pane capability cannot find it by walking the panels
 * alone — it asks the registry, which walks the tools. First reason wins; a
 * group is refused whole, because one native window that cannot follow is enough.
 */
export function popoutRefusal(
  tools: readonly WorkbenchTool[],
  paneIds: readonly string[],
): string | null {
  for (const tool of tools.filter(isEnabled)) {
    const guard = tool.popoutGuard;
    if (guard === undefined) continue;
    for (const id of paneIds) {
      const reason = guard.blocks(id);
      if (reason !== null) return reason;
    }
  }
  return null;
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

/**
 * Which tool owns each command id — static and dynamic alike.
 *
 * The registry already knows this; nothing had asked it before. The keyboard
 * reference does (`keyref.ts`), because "grouped by the tool that owns it" is
 * the only grouping that cannot go stale: a command moving to another tool
 * moves its row, and a tool renaming itself renames its section.
 *
 * `dynamicCommands.build()` is called here, which the memoized command list
 * deliberately avoids doing on every keystroke — this one is called when a
 * *panel renders*, which is several orders of magnitude rarer, and skipping the
 * dynamic set would file one row per saved layout under the wrong heading.
 */
export function commandOwners(
  tools: readonly WorkbenchTool[],
): Map<string, WorkbenchTool> {
  const owners = new Map<string, WorkbenchTool>();
  for (const tool of tools.filter(isEnabled)) {
    for (const command of tool.commands ?? []) owners.set(command.id, tool);
    for (const command of tool.dynamicCommands?.build() ?? []) owners.set(command.id, tool);
  }
  return owners;
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
