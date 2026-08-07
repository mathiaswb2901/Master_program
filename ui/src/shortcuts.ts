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

const PROMPT_DETAIL = "prompt template";

/** How much of a shell body a QuickBar row shows. Wider than the column: the
 * CSS truncates what fits, this bounds what is rendered at all. */
const PREVIEW_CHARS = 140;

/**
 * What a `shell` shortcut types into the terminal: printable text, cut at the
 * first control byte.
 *
 * In a live PTY the C0 controls are key events, not characters. Line breaks are
 * Enter; 0x0f is accept-line in readline and in PSReadLine's Emacs edit mode;
 * 0x03 aborts the line; 0x1b opens an editing escape sequence that ConPTY's
 * input parser turns back into virtual keys. Any of them would act the moment
 * the text landed, so none of them ever reach the PTY — the user presses Enter,
 * always. (The server refuses such a body outright; this is the second lock on
 * the same door.)
 */
export function shellInsertText(body: string): string {
  let stop = body.length;
  for (let i = 0; i < body.length; i += 1) {
    const code = body.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) {
      stop = i;
      break;
    }
  }
  return body.slice(0, stop).trimEnd();
}

/** What a `prompt` shortcut leaves in the chat box: appended to what is there. */
export function promptInsertText(draft: string, body: string): string {
  const kept = draft.replace(/\s+$/, "");
  return kept === "" ? body : `${kept}\n${body}`;
}

/**
 * The QuickBar row's right-hand text.
 *
 * For a shell entry that is the snippet itself, not the file's `detail:` — name
 * and detail are attacker-controlled free text from the same untrusted file as
 * the body, so a row reading "branch + short status" could type anything. The
 * row shows what will actually be typed; the label stops being load-bearing.
 * A `layout` entry shows the layout it switches to, for the same reason.
 */
export function entryDetail(entry: ShortcutEntry): string {
  if (entry.kind === "layout") return `layout · ${entry.body}`;
  // A command row shows the id it will run, not the file's `detail:` — same
  // reason as shell/layout: the label is attacker-controlled, the action is not.
  if (entry.kind === "command") return `command · ${entry.body}`;
  if (entry.kind !== "shell") return entry.detail ?? PROMPT_DETAIL;
  const text = shellInsertText(entry.body);
  return text.length > PREVIEW_CHARS ? `${text.slice(0, PREVIEW_CHARS)}…` : text;
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
    detail: () => entryDetail(entry),
    run: () => run(entry),
  }));
}
