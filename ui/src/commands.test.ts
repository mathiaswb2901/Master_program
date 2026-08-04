import { beforeEach, describe, expect, it, vi } from "vitest";

import { allCommands, BUILTIN_COMMANDS, mergeCommands, visibleCommands } from "./commands";
import { parseChord, resolveCommand, type KeyLike } from "./keys";
import { shortcutCommands } from "./shortcuts";
import type { Command } from "./commands";
import type { ShortcutEntry } from "./types";

// The registry reaches the store for `when`/`run`; the shapes it reads are all
// that matters here, so the module is stubbed rather than dragging Monaco and
// xterm into a unit test.
const mocks = vi.hoisted(() => ({
  state: {
    activePath: null,
    openFiles: [] as unknown[],
    folders: [] as unknown[],
    terminals: [] as unknown[],
    activeTerminalId: null,
    shortcuts: [] as unknown[],
    runShortcut: () => undefined,
  },
}));

vi.mock("./store", () => ({ useStore: { getState: () => mocks.state } }));

const press = (key: string, mods: Partial<KeyLike> = {}): KeyLike => ({
  key,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  ...mods,
});

const entry = (over: Partial<ShortcutEntry> = {}): ShortcutEntry => ({
  name: "Status board",
  kind: "shell",
  body: "git status -sb",
  keys: null,
  detail: null,
  source: "workspace",
  ...over,
});

const chordKey = (text: string): string => {
  const chord = parseChord(text);
  return `${chord.ctrl}|${chord.alt}|${chord.shift}|${chord.key}`;
};

/** The two registry invariants, asserted over any command list. */
function expectRegistryInvariants(commands: readonly Command[]): void {
  const ids = commands.map((command) => command.id);
  expect(new Set(ids).size, "duplicate command id").toBe(ids.length);
  const seen = new Map<string, string>();
  for (const command of commands) {
    for (const text of command.keys ?? []) {
      expect(parseChord(text).key, `${command.id} -> ${text}`).not.toBe("");
      expect(seen.get(chordKey(text)), `${text} bound twice`).toBeUndefined();
      seen.set(chordKey(text), command.id);
    }
  }
}

beforeEach(() => {
  mocks.state.shortcuts = [];
});

describe("command registry", () => {
  it("has unique ids and parseable chords, none bound twice", () => {
    expectRegistryInvariants(BUILTIN_COMMANDS);
  });

  // Chords the browser owns outright: a page cannot preventDefault them, so the
  // one the QuickBar advertises (keys[0]) must never be one of these — a user
  // following the displayed hint in a dev browser tab would close the whole app.
  const browserReserved = ["Ctrl+W", "Ctrl+F4", "Ctrl+T", "Ctrl+N", "Ctrl+Tab"].map(chordKey);

  it("never advertises a browser-reserved chord as the primary binding", () => {
    for (const command of BUILTIN_COMMANDS) {
      const primary = command.keys?.[0];
      if (primary === undefined) continue;
      expect(browserReserved.includes(chordKey(primary)), `${command.id} advertises ${primary}`).toBe(
        false,
      );
    }
  });

  it("reaches the QuickBar command mode from any surface", () => {
    for (const surface of ["other", "editor", "terminal"] as const) {
      const resolved = resolveCommand(
        press("P", { ctrlKey: true, shiftKey: true }),
        surface,
        allCommands(),
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

describe("shortcuts.md extension", () => {
  it("adds one command per entry, keeping the invariants across the merge", () => {
    mocks.state.shortcuts = [
      entry({ name: "Status board", keys: "Alt+G" }),
      entry({ name: "Review", kind: "prompt", body: "Review.", keys: "Ctrl+Alt+R" }),
    ];
    const merged = allCommands();
    expectRegistryInvariants(merged);
    const added = merged.filter((command) => command.category === "Shortcuts");
    expect(added.map((command) => command.title)).toEqual(["Status board", "Review"]);
    expect(merged.length).toBe(BUILTIN_COMMANDS.length + 2);
  });

  it("binds a free chord to a shortcut", () => {
    mocks.state.shortcuts = [entry({ keys: "Alt+G" })];
    const resolved = resolveCommand(press("g", { altKey: true }), "terminal", allCommands());
    expect(resolved?.title).toBe("Status board");
  });

  it("lets a built-in win a chord collision", () => {
    mocks.state.shortcuts = [entry({ name: "Sneaky save", keys: "Ctrl+S" })];
    const merged = allCommands();
    expect(merged.find((command) => command.id === "file.save")?.keys).toContain("Ctrl+S");
    // The row survives — only the binding is dropped.
    const sneaky = merged.find((command) => command.title === "Sneaky save");
    expect(sneaky).toBeDefined();
    expect(sneaky?.keys).toBeUndefined();
    expectRegistryInvariants(merged);
  });

  it("never lets an extension shadow a built-in id", () => {
    const impostor: Command = { id: "file.save", title: "Not the real save", run: () => undefined };
    const merged = mergeCommands(BUILTIN_COMMANDS, [impostor]);
    expect(merged.length).toBe(BUILTIN_COMMANDS.length);
    expect(merged.find((command) => command.id === "file.save")?.title).toBe("Save file");
  });

  it("keeps the first of two shortcuts asking for the same chord", () => {
    const merged = mergeCommands(
      [],
      shortcutCommands(
        [entry({ name: "One", keys: "Alt+G" }), entry({ name: "Two", keys: "Alt+G" })],
        () => undefined,
      ),
    );
    expect(merged.map((command) => command.keys)).toEqual([["Alt+G"], undefined]);
    expectRegistryInvariants(merged);
  });
});
