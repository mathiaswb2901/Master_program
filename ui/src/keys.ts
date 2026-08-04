/**
 * Keyboard chords and the pass-through policy.
 *
 * Kept free of React, the store and (except `surfaceOf`) the DOM, so the rule
 * that decides whether a keystroke belongs to Workbench or to the surface under
 * focus is unit-tested rather than assumed — see `keys.test.ts`.
 */

/** The slice of KeyboardEvent a chord is matched against. */
export interface KeyLike {
  key: string;
  /** Physical key. Used as a fallback for digits (see `matchesKey`). */
  code?: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

export interface Chord {
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
  /** Lowercased `KeyboardEvent.key` value, e.g. "p", "1", "pagedown", "f4". */
  key: string;
}

const chordCache = new Map<string, Chord>();

/** "Ctrl+Shift+P" -> {ctrl:true, shift:true, key:"p"}. Cmd is an alias for Ctrl. */
export function parseChord(text: string): Chord {
  const cached = chordCache.get(text);
  if (cached !== undefined) return cached;
  const chord: Chord = { ctrl: false, alt: false, shift: false, key: "" };
  for (const raw of text.split("+")) {
    const part = raw.trim().toLowerCase();
    if (part === "") continue;
    if (part === "ctrl" || part === "cmd" || part === "meta") chord.ctrl = true;
    else if (part === "alt") chord.alt = true;
    else if (part === "shift") chord.shift = true;
    else chord.key = part;
  }
  chordCache.set(text, chord);
  return chord;
}

function matchesKey(event: KeyLike, key: string): boolean {
  if (key === "") return false;
  if (event.key.toLowerCase() === key) return true;
  // Alt+<digit> reports a composed character as `key` on several keyboard
  // layouts (and on macOS); the physical key is what the user actually pressed.
  return /^[0-9]$/.test(key) && event.code === `Digit${key}`;
}

/**
 * Exact-modifier match: Ctrl+P must NOT fire on Ctrl+Shift+P, or the two would
 * be the same binding. Ctrl and Cmd are treated as one modifier.
 */
export function matchesChord(event: KeyLike, chord: Chord): boolean {
  return (
    (event.ctrlKey || event.metaKey) === chord.ctrl &&
    event.altKey === chord.alt &&
    event.shiftKey === chord.shift &&
    matchesKey(event, chord.key)
  );
}

/** Where focus is: the two surfaces that own their own keyboard, or anywhere else. */
export type Surface = "editor" | "terminal" | "other";

export function surfaceOf(target: EventTarget | null): Surface {
  const element = target instanceof Element ? target : null;
  if (element === null) return "other";
  if (element.closest(".xterm") !== null) return "terminal";
  if (element.closest(".monaco-editor") !== null) return "editor";
  return "other";
}

/**
 * Whether Workbench takes this chord away from whatever has focus.
 *
 * xterm and Monaco are full keyboard applications: Ctrl+K kills a line, Ctrl+P
 * walks shell history, Ctrl+PageDown moves by page. Inside them we intercept
 * only what they do not use themselves — chords carrying Alt, or Ctrl+Shift.
 * Everywhere else any Ctrl/Alt chord is ours. Plain keys (no Ctrl, no Alt) are
 * never intercepted anywhere: typing always reaches the surface.
 *
 * Consequence, by design: Ctrl+K/Ctrl+P do not open the QuickBar while a
 * terminal or editor has focus — Ctrl+Shift+P does, from anywhere.
 */
export function isIntercepted(chord: Chord, surface: Surface): boolean {
  if (!chord.ctrl && !chord.alt) return false;
  if (surface === "other") return true;
  return chord.alt || (chord.ctrl && chord.shift);
}

export interface Bindable {
  keys?: string[];
  /** False = not applicable right now: the chord falls through, unhandled. */
  when?: () => boolean;
}

/** First command bound to this keystroke that is both applicable and ours. */
export function resolveCommand<C extends Bindable>(
  event: KeyLike,
  surface: Surface,
  commands: readonly C[],
): C | null {
  for (const command of commands) {
    for (const text of command.keys ?? []) {
      const chord = parseChord(text);
      if (!matchesChord(event, chord)) continue;
      if (!isIntercepted(chord, surface)) continue;
      if (command.when?.() === false) continue;
      return command;
    }
  }
  return null;
}

const KEYCAP_LABELS: Record<string, string> = {
  ctrl: "Ctrl",
  alt: "Alt",
  shift: "Shift",
  pageup: "PgUp",
  pagedown: "PgDn",
  escape: "Esc",
  arrowup: "↑",
  arrowdown: "↓",
};

/** Chord text -> keycap labels for rendering (DESIGN.md §6.5). */
export function chordKeycaps(text: string): string[] {
  return text
    .split("+")
    .map((raw) => raw.trim())
    .filter((part) => part !== "")
    .map((part) => {
      const lower = part.toLowerCase();
      return KEYCAP_LABELS[lower] ?? (part.length === 1 ? part.toUpperCase() : part);
    });
}
