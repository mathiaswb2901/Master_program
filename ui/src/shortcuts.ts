/**
 * shortcuts.md on the UI side: the pure part.
 *
 * A shortcut is *inserted*, never run. `shellInsertText` is the security-critical
 * half — see its comment. Everything here is free of React, the store and the
 * DOM so the insertion rules are unit-tested rather than assumed
 * (`shortcuts.test.ts`).
 */

import type { Command } from "./commands";
import type { ShortcutEntry } from "./types";

export const SHORTCUTS_CATEGORY = "Shortcuts";

const KIND_DETAIL: Record<ShortcutEntry["kind"], string> = {
  shell: "shell snippet",
  prompt: "prompt template",
};

/**
 * What a `shell` shortcut types into the terminal.
 *
 * Never ends with a newline, and never *contains* one: in a live PTY a newline
 * is an Enter press, so a multi-line snippet would execute its earlier lines the
 * moment it landed. The user presses Enter — always. (The server already refuses
 * multi-line shell bodies; this is the second lock on the same door.)
 */
export function shellInsertText(body: string): string {
  const firstBreak = body.search(/[\r\n]/);
  return (firstBreak === -1 ? body : body.slice(0, firstBreak)).trimEnd();
}

/** What a `prompt` shortcut leaves in the chat box: appended to what is there. */
export function promptInsertText(draft: string, body: string): string {
  const kept = draft.replace(/\s+$/, "");
  return kept === "" ? body : `${kept}\n${body}`;
}

/**
 * Registry commands for the current shortcut entries. `run` is injected so this
 * module never reaches the store — the caller (commands.ts) owns that wiring.
 */
export function shortcutCommands(
  entries: readonly ShortcutEntry[],
  run: (entry: ShortcutEntry) => void,
): Command[] {
  return entries.map((entry) => ({
    id: `shortcut.${entry.source}.${entry.name}`,
    title: entry.name,
    category: SHORTCUTS_CATEGORY,
    keys: entry.keys !== null ? [entry.keys] : undefined,
    detail: () => entry.detail ?? KIND_DETAIL[entry.kind],
    run: () => run(entry),
  }));
}
