/**
 * The command registry: one typed list that is both the keymap and the QuickBar
 * catalogue. Nothing else in the app binds a key or hardcodes an action list.
 *
 * Every command is reachable from the keyboard even without a chord, because
 * Ctrl+Shift+P lists all of them; `keys` exists for the ones worth a reflex.
 *
 * The list has two halves: the static built-ins below, and the dynamic commands
 * the user's `shortcuts.md` contributes (see `mergeCommands`). Built-ins win
 * every collision, so nothing a workspace file says can shadow `Ctrl+S`.
 *
 * Chord choices (browser-safe where it matters — the app runs in a dev browser
 * tab as well as in the Tauri shell):
 *  - Ctrl+Tab is reserved by the browser and never reaches the page, so editor
 *    tab cycling is Ctrl+PageUp/PageDown with Alt+PageUp/PageDown as the twin
 *    that also works inside Monaco and xterm (see `isIntercepted`).
 *  - Closing a tab is Alt+W. Ctrl+W closes the browser window and Ctrl+F4 is its
 *    alias in Chromium, so Ctrl+F4 stays a Tauri-only secondary — never the
 *    keycap the QuickBar advertises.
 *  - Ctrl+1..4 focus panels; in a browser tab Chrome/Firefox eat those to
 *    switch browser tabs, in the Tauri shell they arrive normally.
 *  - Alt+1..9 jump to the n-th most recent session — Alt is free in browsers
 *    and is intercepted even inside a terminal or editor.
 */

import type { DockviewApi } from "dockview";

import { chordId, parseChord, resolveCommand, surfaceOf } from "./keys";
import { shortcutCommands } from "./shortcuts";
import { useStore } from "./store";
import type { SessionInfo, ShortcutEntry } from "./types";

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

// ---- dockview handle (set once the dock is ready) ---------------------------

let dockApi: DockviewApi | null = null;

export function setDockApi(api: DockviewApi | null): void {
  dockApi = api;
}

function focusPanel(id: string): void {
  dockApi?.getPanel(id)?.api.setActive();
}

// ---- small helpers over the store -------------------------------------------

/** Folder a new session should be bound to: the active file's directory. */
function activeFolder(): string {
  const path = useStore.getState().activePath;
  return path !== null && path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}

/** Every session, newest first — live ones and resumable transcripts alike. */
function recentSessions(): SessionInfo[] {
  return useStore
    .getState()
    .folders.flatMap((group) => group.sessions)
    .sort((a, b) => b.updated_at - a.updated_at);
}

function cycleEditorTab(step: number): void {
  const s = useStore.getState();
  if (s.openFiles.length === 0) return;
  const current = s.openFiles.findIndex((f) => f.path === s.activePath);
  const index = (Math.max(current, 0) + step + s.openFiles.length) % s.openFiles.length;
  const next = s.openFiles[index];
  if (next !== undefined) s.setActiveFile(next.path);
}

const hasOpenFile = (): boolean => useStore.getState().openFiles.length > 0;

const sessionJumps: Command[] = Array.from({ length: 9 }, (_, i) => ({
  id: `session.jump.${i + 1}`,
  title: `Jump to session ${i + 1}`,
  keys: [`Alt+${i + 1}`],
  when: () => recentSessions().length > i,
  detail: () => recentSessions()[i]?.title ?? "",
  run: () => {
    const session = recentSessions()[i];
    if (session !== undefined) useStore.getState().openSession(session);
  },
}));

export const BUILTIN_COMMANDS: readonly Command[] = [
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
  {
    id: "file.save",
    title: "Save file",
    keys: ["Ctrl+S"],
    when: () => useStore.getState().activePath !== null,
    run: () => {
      const s = useStore.getState();
      if (s.activePath !== null) void s.saveFile(s.activePath);
    },
  },
  {
    id: "editor.nextTab",
    title: "Next editor tab",
    keys: ["Ctrl+PageDown", "Alt+PageDown"],
    when: hasOpenFile,
    run: () => cycleEditorTab(1),
  },
  {
    id: "editor.prevTab",
    title: "Previous editor tab",
    keys: ["Ctrl+PageUp", "Alt+PageUp"],
    when: hasOpenFile,
    run: () => cycleEditorTab(-1),
  },
  {
    id: "editor.close",
    title: "Close editor tab",
    keys: ["Alt+W", "Ctrl+F4"],
    when: () => useStore.getState().activePath !== null,
    run: () => {
      const path = useStore.getState().activePath;
      if (path !== null) useStore.getState().requestCloseFile(path);
    },
  },
  {
    id: "panel.files",
    title: "Focus Files panel",
    keys: ["Ctrl+1"],
    run: () => focusPanel("files"),
  },
  {
    id: "panel.editors",
    title: "Focus Editor panel",
    keys: ["Ctrl+2"],
    run: () => focusPanel("editors"),
  },
  {
    id: "panel.agent",
    title: "Focus Agent panel",
    keys: ["Ctrl+3"],
    run: () => focusPanel("agent"),
  },
  {
    id: "panel.terminal",
    title: "Focus Terminal panel",
    keys: ["Ctrl+4"],
    run: () => focusPanel("terminal"),
  },
  {
    id: "session.new",
    title: "New agent session here",
    detail: () => activeFolder() || "workspace root",
    run: () => void useStore.getState().createSessionIn(activeFolder()),
  },
  ...sessionJumps,
  {
    id: "terminal.new",
    title: "New terminal",
    keys: ["Alt+T"],
    run: () => useStore.getState().newTerminal(),
  },
  {
    id: "terminal.close",
    title: "Close terminal",
    when: () => useStore.getState().terminals.length > 0,
    run: () => {
      const s = useStore.getState();
      if (s.activeTerminalId !== null) s.closeTerminal(s.activeTerminalId);
    },
  },
  {
    id: "view.toggleTheme",
    title: "Toggle theme (dark/light)",
    run: () => useStore.getState().toggleTheme(),
  },
];

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
  builtins: readonly Command[],
  extra: readonly Command[],
): Command[] {
  const ids = new Set(builtins.map((command) => command.id));
  const chords = new Set(builtins.flatMap((command) => (command.keys ?? []).map(chordId)));
  const merged: Command[] = [...builtins];
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
 * gets focus so the user's next keystroke (Enter, or more typing) goes there. */
function runShortcut(entry: ShortcutEntry): void {
  focusPanel(entry.kind === "shell" ? "terminal" : "agent");
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
    merged = { entries, commands: mergeCommands(BUILTIN_COMMANDS, extra) };
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
