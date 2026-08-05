/**
 * The command registry: one typed list that is both the keymap and the QuickBar
 * catalogue. Nothing else in the app binds a key or hardcodes an action list.
 *
 * Every command is reachable from the keyboard even without a chord, because
 * Ctrl+Shift+P lists all of them; `keys` exists for the ones worth a reflex.
 *
 * The list has three sources, and they compose in this order:
 *  1. the handful of genuinely global commands below (theme, QuickBar);
 *  2. the **tool registry** — panel focus derived from the registered panels,
 *     plus every command a registered tool owns (`registry.ts`, `tools.ts`).
 *     Saving is the Editor tool's, `Alt+T` is the Terminal tool's, the session
 *     jumps are the Agent tool's — each declared next to the panel it belongs
 *     to, not here;
 *  3. the dynamic commands the user's `shortcuts.md` contributes (see
 *     `mergeCommands`). Everything above wins every collision, so nothing a
 *     workspace file says can shadow `Ctrl+S`.
 *
 * Chord choices (browser-safe where it matters — the app runs in a dev browser
 * tab as well as in the Tauri shell):
 *  - Ctrl+Tab is reserved by the browser and never reaches the page, so editor
 *    tab cycling is Ctrl+PageUp/PageDown with Alt+PageUp/PageDown as the twin
 *    that also works inside Monaco and xterm (see `isIntercepted`).
 *  - Closing a tab is Alt+W. Ctrl+W closes the browser window and Ctrl+F4 is its
 *    alias in Chromium, so Ctrl+F4 stays a Tauri-only secondary — never the
 *    keycap the QuickBar advertises.
 *  - Ctrl+1..N focus the panels in registry order; in a browser tab
 *    Chrome/Firefox eat those to switch browser tabs, in the Tauri shell they
 *    arrive normally.
 *  - Alt+1..9 jump to the n-th most recent session — Alt is free in browsers
 *    and is intercepted even inside a terminal or editor.
 */

import { focusPanel } from "./dock";
import { chordId, parseChord, resolveCommand, surfaceOf } from "./keys";
import { panelFocusCommands, shortcutHost, toolCommands } from "./registry";
import { shortcutCommands } from "./shortcuts";
import { useStore } from "./store";
import { TOOLS } from "./tools";
import type { ShortcutEntry } from "./types";

export interface Command {
  id: string;
  title: string;
  /** Chords, most-preferred first. The first one is shown in the QuickBar. */
  keys?: string[];
  /** False hides the command from the QuickBar and makes its chords inert. */
  when?: () => boolean;
  /** Extra context for the QuickBar row (right-aligned, tertiary). */
  detail?: () => string;
  /** QuickBar grouping. Built-ins are uncategorized and listed first. */
  category?: string;
  run: () => void;
}

/**
 * Commands that belong to the window itself rather than to any capability.
 * Everything panel-specific lives on its tool; if a command here starts needing
 * to know what a panel is, it is in the wrong file.
 *
 * Split into a leading and a trailing half for one reason: order is what the
 * user sees. The QuickBar sorts on relevance, which ties at zero for an empty
 * query, so an empty command palette lists these in registry order — and the
 * two QuickBar entries have always led it with the theme toggle at the end.
 * Registry-contributed commands slot in between, where the hand-written list
 * used to have them.
 */
const OPENING_COMMANDS: readonly Command[] = [
  {
    id: "quickbar.files",
    title: "Go to file…",
    keys: ["Ctrl+P", "Ctrl+K"],
    // Toggle: the same chord that opened it puts it away again.
    run: () => {
      const s = useStore.getState();
      s.setQuickBarOpen(!s.quickBarOpen);
    },
  },
  {
    id: "quickbar.commands",
    title: "Show all commands",
    keys: ["Ctrl+Shift+P"],
    // Always opens in command mode, including from an already-open file search.
    run: () => useStore.getState().setQuickBarOpen(true, ">"),
  },
];

const CLOSING_COMMANDS: readonly Command[] = [
  {
    id: "view.toggleTheme",
    title: "Toggle theme (dark/light)",
    run: () => useStore.getState().toggleTheme(),
  },
];

export const GLOBAL_COMMANDS: readonly Command[] = [...OPENING_COMMANDS, ...CLOSING_COMMANDS];

/**
 * Global commands + everything the tool registry contributes, in the order the
 * QuickBar shows them (`commands.test.ts` pins it). Static — a tool's `when` is
 * settled when the registry is first derived — so it is built once, which
 * matters because this is read on every keystroke in the app.
 */
let builtins: Command[] | null = null;

export function builtinCommands(): Command[] {
  builtins ??= [
    ...OPENING_COMMANDS,
    ...toolCommands(TOOLS),
    ...panelFocusCommands(TOOLS, focusPanel),
    ...CLOSING_COMMANDS,
  ];
  return builtins;
}

// ---- dynamic extension: shortcuts.md ---------------------------------------

/**
 * Chords a dynamic (file-supplied) command may take. Alt only, mirroring the
 * server's `_resolve_chord`.
 *
 * `isIntercepted` hands us *every* Ctrl chord when focus is anywhere but Monaco
 * or xterm, and preventDefaults it — so a `shortcuts.md` asking for `Ctrl+V`
 * would take paste away from the chat box, the rename field and the QuickBar
 * itself. Alt is also the only modifier that reaches Workbench from inside the
 * terminal and the editor, so it is both the safe set and the useful one.
 */
function isBindableByFile(text: string): boolean {
  return parseChord(text).alt;
}

/**
 * Built-ins plus dynamic commands, with the registry's invariants preserved:
 * ids stay unique and no chord is bound twice. Built-ins always win — a
 * shortcut that asks for `Ctrl+S` keeps its QuickBar row and loses the chord,
 * never the other way round.
 */
export function mergeCommands(
  builtin: readonly Command[],
  extra: readonly Command[],
): Command[] {
  const ids = new Set(builtin.map((command) => command.id));
  const chords = new Set(builtin.flatMap((command) => (command.keys ?? []).map(chordId)));
  const merged: Command[] = [...builtin];
  for (const command of extra) {
    if (ids.has(command.id)) continue;
    ids.add(command.id);
    const keys = (command.keys ?? []).filter((text) => {
      if (!isBindableByFile(text)) return false;
      const id = chordId(text);
      if (chords.has(id)) return false;
      chords.add(id);
      return true;
    });
    merged.push({ ...command, keys: keys.length > 0 ? keys : undefined });
  }
  return merged;
}

/** Inserting is the only thing a shortcut may do — the surface it lands in
 * gets focus so the user's next keystroke (Enter, or more typing) goes there.
 * Which panel that is, is a registry question (`shortcutKinds` on the tool that
 * hosts the kind), not a pair of panel ids written down here. */
function runShortcut(entry: ShortcutEntry): void {
  const host = shortcutHost(TOOLS, entry.kind);
  if (host !== null) focusPanel(host);
  useStore.getState().runShortcut(entry);
}

/** The live registry: built-ins + one command per shortcuts.md entry.
 * Memoized on the entries array (replaced only by a reload) — this runs on
 * every keystroke in the app. */
let merged: { entries: readonly ShortcutEntry[]; commands: Command[] } | null = null;

export function allCommands(): Command[] {
  const entries = useStore.getState().shortcuts;
  if (merged === null || merged.entries !== entries) {
    const extra = shortcutCommands(entries, runShortcut);
    merged = { entries, commands: mergeCommands(builtinCommands(), extra) };
  }
  return merged.commands;
}

/** Commands applicable right now — what the QuickBar lists. */
export function visibleCommands(): Command[] {
  return allCommands().filter((command) => command.when?.() !== false);
}

/**
 * Capture-phase keymap: capture so a chord that IS ours wins over Monaco and
 * xterm, `isIntercepted` (via resolveCommand) so the ones that are not stay
 * invisible to us and reach the surface untouched.
 */
export function installCommandKeys(): () => void {
  const onKeyDown = (event: KeyboardEvent): void => {
    const command = resolveCommand(event, surfaceOf(event.target), allCommands());
    if (command === null) return;
    event.preventDefault();
    event.stopPropagation();
    command.run();
  };
  window.addEventListener("keydown", onKeyDown, true);
  return () => window.removeEventListener("keydown", onKeyDown, true);
}
