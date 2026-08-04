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
