import { beforeEach, describe, expect, it, vi } from "vitest";

import { parseChord, resolveCommand, type KeyLike } from "./keys";
import { shortcutCommands } from "./shortcuts";
import type { Command } from "./commands";
import type { ShortcutEntry } from "./types";

// The registry reaches the store for `when`/`run`; the shapes it reads are all
// that matters here, so the module is stubbed rather than dragging Monaco and
// xterm into a unit test. Monaco is stubbed for the same reason: the built-in
// list is now assembled from the tool registry, so this file imports the real
// panel modules along with it.
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

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => mocks.state }),
  emptyPlanDraft: () => ({ choices: {}, annotations: {}, comment: "", verdict: null }),
  unchosenOptionGroups: () => [],
}));

vi.mock("./monaco", () => ({
  MONO_FONT: "mono",
  editorPathProp: (path: string) => path,
  languageForPath: () => "plaintext",
  monacoThemeName: () => "workbench",
  setActiveEditor: () => undefined,
  disposeModel: () => undefined,
  setModelContent: () => null,
  defineWorkbenchTheme: () => undefined,
  initMonaco: () => undefined,
}));

const { allCommands, builtinCommands, GLOBAL_COMMANDS, mergeCommands, visibleCommands } =
  await import("./commands");

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
  return `${String(chord.ctrl)}|${String(chord.alt)}|${String(chord.shift)}|${chord.key}`;
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
    expectRegistryInvariants(builtinCommands());
  });

  // Chords the browser owns outright: a page cannot preventDefault them, so the
  // one the QuickBar advertises (keys[0]) must never be one of these — a user
  // following the displayed hint in a dev browser tab would close the whole app.
  const browserReserved = ["Ctrl+W", "Ctrl+F4", "Ctrl+T", "Ctrl+N", "Ctrl+Tab"].map(chordKey);

  it("never advertises a browser-reserved chord as the primary binding", () => {
    for (const command of builtinCommands()) {
      const primary = command.keys?.[0];
      if (primary === undefined) continue;
      expect(
        browserReserved.includes(chordKey(primary)),
        `${command.id} advertises ${primary}`,
      ).toBe(false);
    }
  });

  // The built-ins are no longer a hand-kept constant — they are assembled from
  // the registry — but the *order* is still what a user sees: the QuickBar
  // sorts on relevance, which ties at zero for the empty query the palette
  // opens with, so this list is the palette, top to bottom. Pinned in full,
  // because reordering it silently moves the row under someone's second
  // ArrowDown. Changing it is allowed; changing it by accident is not.
  it("lists the commands in the order the QuickBar shows them", () => {
    expect(builtinCommands().map((command) => command.id)).toEqual([
      "quickbar.files",
      "quickbar.commands",
      // …the Editor tool's, then the Agent's, then the Terminal's, then the
      // Scratchpad's — registry order (`tools.ts`), never declared here.
      "file.save",
      "editor.nextTab",
      "editor.prevTab",
      "editor.close",
      "session.new",
      "session.jump.1",
      "session.jump.2",
      "session.jump.3",
      "session.jump.4",
      "session.jump.5",
      "session.jump.6",
      "session.jump.7",
      "session.jump.8",
      "session.jump.9",
      "terminal.new",
      "terminal.close",
      "office.detachHost",
      "scratchpad.open",
      // The two tools that arrange panels rather than being one, in the order
      // `tools.ts` lists them: Panes splits the window, Layouts remembers it.
      "pane.split.right",
      "pane.split.down",
      "pane.focus.left",
      "pane.focus.right",
      "pane.focus.up",
      "pane.focus.down",
      "pane.swap.left",
      "pane.swap.right",
      "pane.swap.up",
      "pane.swap.down",
      "pane.cycle",
      "pane.cycleBack",
      "pane.close",
      // The Layouts tool's *static* commands. Its per-layout rows are dynamic
      // (the saved set changes while the app runs) and so are not in here —
      // they join in `allCommands`, after everything static.
      "layout.focus",
      "layout.save",
      "panel.files",
      "panel.editors",
      "panel.agent",
      "panel.terminal",
      "view.toggleTheme",
    ]);
  });

  it("keeps every window-level command in the assembled list", () => {
    const ids = builtinCommands().map((command) => command.id);
    for (const command of GLOBAL_COMMANDS) expect(ids).toContain(command.id);
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

  it("resolves a tool's own chord through the merged list", () => {
    const resolved = resolveCommand(press("t", { altKey: true }), "terminal", allCommands());
    expect(resolved?.id).toBe("terminal.new");
  });

  it("hides session jumps while there are no sessions", () => {
    const ids = visibleCommands().map((command) => command.id);
    expect(ids).toContain("session.new");
    expect(ids.some((id) => id.startsWith("session.jump."))).toBe(false);
  });
});

describe("shortcuts.md extension", () => {
  it("adds one command per entry, keeping the invariants across the merge", () => {
    // Against the registry's own list (built-ins *and* the dynamic rows the
    // Layouts tool contributes), not against `builtinCommands()` alone — the
    // claim is "two more rows than without the file", whatever else is there.
    mocks.state.shortcuts = [];
    const withoutFile = allCommands().length;
    mocks.state.shortcuts = [
      entry({ name: "Status board", keys: "Alt+G" }),
      entry({ name: "Review", kind: "prompt", body: "Review.", keys: "Ctrl+Alt+R" }),
    ];
    const merged = allCommands();
    expectRegistryInvariants(merged);
    const added = merged.filter((command) => command.category === "Shortcuts");
    expect(added.map((command) => command.title)).toEqual(["Status board", "Review"]);
    expect(merged.length).toBe(withoutFile + 2);
  });

  // Dynamic rows come from the registry, so they are subject to every rule the
  // static ones are — and they arrive before the file's, which is what puts the
  // "Layouts" section above "Shortcuts" in an unfiltered palette.
  it("lists the registry's dynamic commands ahead of the file's", () => {
    mocks.state.shortcuts = [entry({ name: "Status board" })];
    const categories = allCommands()
      .map((command) => command.category)
      .filter((category): category is string => category !== undefined);
    expect([...new Set(categories)]).toEqual(["Panes", "Layouts", "Shortcuts"]);
  });

  it("never gives a dynamic command a chord", () => {
    const dynamic = allCommands().filter((command) => command.id.startsWith("layout.apply."));
    expect(dynamic.length).toBeGreaterThan(0);
    for (const command of dynamic) expect(command.keys).toBeUndefined();
  });

  it("binds a free chord to a shortcut", () => {
    mocks.state.shortcuts = [entry({ keys: "Alt+G" })];
    const resolved = resolveCommand(press("g", { altKey: true }), "terminal", allCommands());
    expect(resolved?.title).toBe("Status board");
  });

  it("lets a registered command win a chord collision", () => {
    mocks.state.shortcuts = [entry({ name: "Sneaky terminal", keys: "Alt+T" })];
    const merged = allCommands();
    expect(merged.find((command) => command.id === "terminal.new")?.keys).toContain("Alt+T");
    // The row survives — only the binding is dropped.
    const sneaky = merged.find((command) => command.title === "Sneaky terminal");
    expect(sneaky).toBeDefined();
    expect(sneaky?.keys).toBeUndefined();
    expectRegistryInvariants(merged);
  });

  // Outside Monaco and xterm `isIntercepted` hands us every Ctrl chord and
  // preventDefaults it, so a file binding one of these would take paste (or
  // select-all, or undo) away from the chat box, the rename field and the
  // QuickBar's own input. The server refuses them; this is the second lock.
  it.each(["Ctrl+V", "Ctrl+C", "Ctrl+X", "Ctrl+A", "Ctrl+Z", "Ctrl+Shift+V", "G"])(
    "never lets a file take %s",
    (chord) => {
      mocks.state.shortcuts = [entry({ name: "Paste thief", keys: chord })];
      const grabby = allCommands().find((command) => command.title === "Paste thief");
      expect(grabby).toBeDefined();
      expect(grabby?.keys).toBeUndefined();
    },
  );

  it("still lets the editing chords reach the surface", () => {
    mocks.state.shortcuts = [entry({ name: "Paste thief", keys: "Ctrl+V" })];
    expect(resolveCommand(press("v", { ctrlKey: true }), "other", allCommands())).toBeNull();
  });

  it("never lets an extension shadow a registered id", () => {
    const impostor: Command = { id: "file.save", title: "Not the real save", run: () => undefined };
    const merged = mergeCommands(builtinCommands(), [impostor]);
    expect(merged.length).toBe(builtinCommands().length);
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
