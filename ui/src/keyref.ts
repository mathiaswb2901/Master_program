/**
 * The keyboard reference, **derived from the registry**.
 *
 * Workbench is a keyboard-first app whose capabilities were, until this module,
 * invisible: nothing on screen said that `Alt+S` splits a pane or that `Alt+M`
 * fills the window. The fix is not a cheat sheet — a hand-written one is wrong
 * the day a tool changes a chord, and this repo already holds the general form
 * of that argument ("a budget that lives outside the quality gate does not
 * bind"). Every tool already declares its commands and their default chords on
 * its descriptor, so the reference is a **rendering** of the registry: if a
 * command exists it appears here, and if its chord moves the surface moves with
 * it. `keyref.test.ts` fails the build when a registered command is not
 * reachable from this derivation.
 *
 * Kept free of React, the store and the DOM (`chordFor` is the one live read,
 * and it reads the command list, not a component), so the grouping and the
 * search are unit-tested rather than assumed.
 */

import { builtinCommands, type Command } from "./commands";
import { commandOwners, type WorkbenchTool } from "./registry";

/** One command, as the reference shows it. */
export interface KeyRefRow {
  /** Command id — the row's identity, and what the anti-rot test asserts on. */
  id: string;
  title: string;
  /** The command's own right-hand context, read live (`Command.detail`). */
  detail: string;
  /** Chords, most-preferred first. Empty = reachable from the QuickBar only,
   * which is a fact worth showing rather than a row worth hiding. */
  chords: readonly string[];
  /**
   * Would this command run *right now* (`Command.when`)?
   *
   * A reference exists to teach, so a gated-off command keeps its row — "jump
   * to the 3rd session" is worth knowing about before you have three sessions.
   * But a row that reads like every other one while its chord is inert teaches
   * a reflex that silently does nothing (`resolveCommand` drops it: no
   * `preventDefault`, no feedback), which is the one thing worse than not
   * teaching it. So the row says which it is, and the panel shows it.
   */
  available: boolean;
}

/** One section: the tool that owns these commands, or the window itself. */
export interface KeyRefGroup {
  id: string;
  title: string;
  rows: readonly KeyRefRow[];
}

/**
 * Commands that belong to the window rather than to a capability — the QuickBar
 * pair, the theme toggle, and the derived `Ctrl+1..N` panel focus.
 *
 * Panel focus is *derived* from the registered panels rather than declared by
 * them (`panelFocusCommands`), so no tool owns those commands. Listing them
 * with the window is also how they read: `Ctrl+1..4` is one thing to learn, not
 * one row scattered across four capabilities.
 */
const WINDOW_GROUP_ID = "window";
const WINDOW_GROUP_TITLE = "Window";

const PANEL_FOCUS_PREFIX = "panel.";

/**
 * A command's right-hand context, or nothing.
 *
 * `detail` is a thunk over live app state — the folder a session would start
 * in, the file a save would write. It is *advisory* on this surface: the row
 * exists to say the command exists and which key runs it, so one tool's thunk
 * failing must cost that row its subtitle and not the reference its list. That
 * is also what keeps the anti-rot test below honest without a fixture that has
 * to grow every time another capability adds a detail of its own.
 */
function detailOf(command: Command): string {
  try {
    return command.detail?.() ?? "";
  } catch {
    return "";
  }
}

/**
 * Is this command's gate open? Asked the same way `detailOf` asks for a
 * subtitle — live, and never allowed to cost the reference its list. A gate
 * that *throws* is a broken tool, not a closed door, so the row stays ordinary:
 * dimming a command on the strength of an exception would be a guess.
 */
function availableOf(command: Command): boolean {
  try {
    return command.when?.() !== false;
  } catch {
    return true;
  }
}

const row = (command: Command): KeyRefRow => ({
  id: command.id,
  title: command.title,
  detail: detailOf(command),
  chords: command.keys ?? [],
  available: availableOf(command),
});

/**
 * Every command, grouped by the tool that owns it.
 *
 * Ownership is the registry's own answer (`commandOwners`), which covers a
 * tool's static commands *and* the ones whose set changes while the app runs —
 * one row per saved layout is still the Layouts tool's. What no tool claims
 * falls back to its `category` (that is how a `shortcuts.md` entry lands under
 * **Shortcuts**) and then to the window.
 *
 * Order mirrors the QuickBar's (DESIGN.md §6.5) so the two surfaces read as one
 * app: the window's own commands first, then the tools in registry order, then
 * whatever categories are left, in the order they first appear.
 */
export function keyReference(
  tools: readonly WorkbenchTool[],
  commands: readonly Command[],
): KeyRefGroup[] {
  const owners = commandOwners(tools);
  const groups = new Map<string, KeyRefGroup & { rows: KeyRefRow[] }>();
  const group = (id: string, title: string): KeyRefRow[] => {
    const existing = groups.get(id);
    if (existing !== undefined) return existing.rows;
    const created = { id, title, rows: [] as KeyRefRow[] };
    groups.set(id, created);
    return created.rows;
  };
  // Seeded first so the window leads even when its commands arrive later; an
  // empty group is dropped below, so seeding costs nothing if it stays empty.
  // Group ids are namespaced, so a tool called `window` is still its own
  // section and a tool and a category of the same name cannot become one.
  group(WINDOW_GROUP_ID, WINDOW_GROUP_TITLE);
  for (const tool of tools) group(`tool:${tool.id}`, tool.title);

  for (const command of commands) {
    const owner = owners.get(command.id);
    if (owner !== undefined) {
      group(`tool:${owner.id}`, owner.title).push(row(command));
    } else if (command.id.startsWith(PANEL_FOCUS_PREFIX) || command.category === undefined) {
      group(WINDOW_GROUP_ID, WINDOW_GROUP_TITLE).push(row(command));
    } else {
      group(`category:${command.category}`, command.category).push(row(command));
    }
  }
  return [...groups.values()].filter((candidate) => candidate.rows.length > 0);
}

/** A chord as one comparable token: `Alt+Shift+S` and `alt shift s` are the
 * same thing to someone searching for it. */
const squash = (text: string): string => text.toLowerCase().replace(/[\s+]/g, "");

function rowMatches(query: string, groupTitle: string, candidate: KeyRefRow): boolean {
  const haystack = `${candidate.title} ${candidate.detail} ${groupTitle}`.toLowerCase();
  if (haystack.includes(query)) return true;
  const squashed = squash(query);
  return candidate.chords.some((chord) => squash(chord).includes(squashed));
}

/**
 * The reference, filtered.
 *
 * Substring rather than the QuickBar's fuzzy scorer, and deliberately: a
 * reference is read, not raced. Fuzzy matching would answer "alt" with
 * everything containing an a, an l and a t in that order, which in a list whose
 * rows are all commands is most of them.
 *
 * Three things match: the row's text, the **chord** (so `alt+s`, `alt s` and
 * `alts` all find the split), and the group's title (so `panes` shows the pane
 * system's whole keymap). Groups keep their order and their identity; a group
 * with nothing left is dropped rather than left as an empty header.
 */
export function filterKeyReference(
  groups: readonly KeyRefGroup[],
  query: string,
): KeyRefGroup[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...groups];
  return groups
    .map((group) => ({
      ...group,
      rows: group.rows.filter((candidate) => rowMatches(needle, group.title, candidate)),
    }))
    .filter((group) => group.rows.length > 0);
}

/** How many rows a filtered reference is showing — the count the panel states
 * so an empty search says "none" rather than showing blankness. */
export const rowCount = (groups: readonly KeyRefGroup[]): number =>
  groups.reduce((total, group) => total + group.rows.length, 0);

/**
 * The primary chord of a registered command, or `""`.
 *
 * The one live read in this module, and the reason it exists: a control whose
 * tooltip names its chord teaches the keyboard path to someone using the mouse
 * — but a *hardcoded* chord in a tooltip is the same staleness this module
 * exists to remove, one control at a time. Every tooltip that names a chord
 * asks for it here (DESIGN.md §6.12).
 *
 * Static commands only (`builtinCommands`), which is exactly right: a chord has
 * to be declared statically to be bound at all (`registry.ts`, `DynamicCommands`).
 */
export function chordFor(commandId: string): string {
  return builtinCommands().find((command) => command.id === commandId)?.keys?.[0] ?? "";
}

/** `"Split right"` + `pane.split.right` -> `"Split right — Alt+S"`, and just
 * `"Split right"` for a command that has no chord. */
export function chordTooltip(label: string, commandId: string): string {
  const chord = chordFor(commandId);
  return chord === "" ? label : `${label} — ${chord}`;
}
