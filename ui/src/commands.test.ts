import { describe, expect, it, vi } from "vitest";

import { COMMANDS, visibleCommands } from "./commands";
import { parseChord, resolveCommand, type KeyLike } from "./keys";

// The registry reaches the store for `when`/`run`; the shapes it reads are all
// that matters here, so the module is stubbed rather than dragging Monaco and
// xterm into a unit test.
vi.mock("./store", () => ({
  useStore: {
    getState: () => ({
      activePath: null,
      openFiles: [],
      folders: [],
      terminals: [],
      activeTerminalId: null,
    }),
  },
}));

const press = (key: string, mods: Partial<KeyLike> = {}): KeyLike => ({
  key,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  ...mods,
});

describe("command registry", () => {
  it("has unique ids", () => {
    const ids = COMMANDS.map((command) => command.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("has parseable chords with a real key", () => {
    for (const command of COMMANDS) {
      for (const text of command.keys ?? []) {
        expect(parseChord(text).key, `${command.id} -> ${text}`).not.toBe("");
      }
    }
  });

  it("binds no chord twice", () => {
    const seen = new Map<string, string>();
    for (const command of COMMANDS) {
      for (const text of command.keys ?? []) {
        const chord = parseChord(text);
        const key = `${chord.ctrl}|${chord.alt}|${chord.shift}|${chord.key}`;
        expect(seen.get(key), `${text} bound twice`).toBeUndefined();
        seen.set(key, command.id);
      }
    }
  });

  // Chords the browser owns outright: a page cannot preventDefault them, so the
  // one the QuickBar advertises (keys[0]) must never be one of these — a user
  // following the displayed hint in a dev browser tab would close the whole app.
  const browserReserved = ["Ctrl+W", "Ctrl+F4", "Ctrl+T", "Ctrl+N", "Ctrl+Tab"].map(parseChord);

  it("never advertises a browser-reserved chord as the primary binding", () => {
    for (const command of COMMANDS) {
      const primary = command.keys?.[0];
      if (primary === undefined) continue;
      const chord = parseChord(primary);
      const reserved = browserReserved.some(
        (r) =>
          r.ctrl === chord.ctrl &&
          r.alt === chord.alt &&
          r.shift === chord.shift &&
          r.key === chord.key,
      );
      expect(reserved, `${command.id} advertises ${primary}`).toBe(false);
    }
  });

  it("reaches the QuickBar command mode from any surface", () => {
    for (const surface of ["other", "editor", "terminal"] as const) {
      const resolved = resolveCommand(
        press("P", { ctrlKey: true, shiftKey: true }),
        surface,
        COMMANDS,
      );
      expect(resolved?.id).toBe("quickbar.commands");
    }
  });

  it("hides session jumps while there are no sessions", () => {
    const ids = visibleCommands().map((command) => command.id);
    expect(ids).toContain("session.new");
    expect(ids.some((id) => id.startsWith("session.jump."))).toBe(false);
  });
});
