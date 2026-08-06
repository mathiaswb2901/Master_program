/**
 * The keyboard reference: its grouping and its search against fixtures, and
 * **the anti-rot guarantee** against the registry the app actually ships.
 *
 * The second half is the point of this file. A discovery surface built by hand
 * is wrong the day a tool changes a chord, and nothing on screen says so — the
 * user simply presses a key that no longer does what the page claims. So the
 * reference is generated, and this test fails the build if any registered
 * command is unreachable from it, or if a chord it shows is not the chord that
 * runs.
 *
 * Importing the real registry means importing the panel modules, so the two
 * things that cannot load outside a browser are stubbed exactly as
 * `registry.test.ts` stubs them: Monaco's bundle, and the store (which reads
 * `document` at import). Neither is what is under test.
 */

import { describe, expect, it, vi } from "vitest";

import {
  chordFor,
  chordTooltip,
  filterKeyReference,
  keyReference,
  rowCount,
  type KeyRefGroup,
} from "./keyref";
import type { WorkbenchTool } from "./registry";

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

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => ({ shortcuts: [] }) }),
  emptyPlanDraft: () => ({ choices: {}, annotations: {}, comment: "", verdict: null }),
  unchosenOptionGroups: () => [],
}));

const { builtinCommands } = await import("./commands");
const { TOOLS } = await import("./tools");

// ---- fixtures ---------------------------------------------------------------

const tool = (over: Partial<WorkbenchTool> & { id: string }): WorkbenchTool => ({
  title: over.id,
  ...over,
});

const command = (
  id: string,
  title: string,
  keys?: string[],
): { id: string; title: string; keys?: string[]; run: () => void } => ({
  id,
  title,
  ...(keys === undefined ? {} : { keys }),
  run: () => undefined,
});

const titles = (groups: readonly KeyRefGroup[]): string[] =>
  groups.map((group) => group.title);

const rowIds = (groups: readonly KeyRefGroup[], title: string): string[] =>
  groups.find((group) => group.title === title)?.rows.map((row) => row.id) ?? [];

// ---- grouping ---------------------------------------------------------------

describe("grouping", () => {
  const tools = [
    tool({
      id: "term",
      title: "Terminal",
      commands: [command("terminal.new", "New terminal")],
    }),
    tool({
      id: "panes",
      title: "Panes",
      commands: [command("pane.split.right", "Split right")],
    }),
  ];

  it("files each command under the tool that owns it, in registry order", () => {
    const groups = keyReference(tools, [
      command("quickbar.files", "Go to file…", ["Ctrl+K"]),
      command("pane.split.right", "Split right", ["Alt+S"]),
      command("terminal.new", "New terminal", ["Alt+T"]),
    ]);
    expect(titles(groups)).toEqual(["Window", "Terminal", "Panes"]);
    expect(rowIds(groups, "Terminal")).toEqual(["terminal.new"]);
    expect(rowIds(groups, "Panes")).toEqual(["pane.split.right"]);
  });

  it("keeps the window's own commands together, panel focus included", () => {
    const groups = keyReference(tools, [
      command("quickbar.commands", "Show all commands", ["Ctrl+Shift+P"]),
      // Derived from the registered panels rather than declared by them, so no
      // tool owns them — and Ctrl+1..N is one thing to learn, not four.
      command("panel.files", "Focus Files panel", ["Ctrl+1"]),
      command("view.toggleTheme", "Toggle theme"),
    ]);
    expect(rowIds(groups, "Window")).toEqual([
      "quickbar.commands",
      "panel.files",
      "view.toggleTheme",
    ]);
  });

  // One row per saved layout is still the Layouts tool's, and a reference that
  // filed them under "Shortcuts" (their category) would be lying about who
  // owns them and would split one tool's keymap across two headings.
  it("files a tool's runtime commands under that tool", () => {
    const withDynamic = [
      tool({
        id: "layouts",
        title: "Layouts",
        commands: [command("layout.focus", "Toggle focus mode")],
        dynamicCommands: {
          key: () => "review",
          build: () => [command("layout.apply.review", "Switch to the Review layout")],
        },
      }),
    ];
    const groups = keyReference(withDynamic, [
      command("layout.focus", "Toggle focus mode", ["Alt+M"]),
      command("layout.apply.review", "Switch to the Review layout"),
    ]);
    expect(rowIds(groups, "Layouts")).toEqual(["layout.focus", "layout.apply.review"]);
  });

  it("gives a command nobody owns its own category as a section", () => {
    const groups = keyReference(tools, [
      { ...command("shortcut.mine", "Show the marker", ["Alt+G"]), category: "Shortcuts" },
    ]);
    expect(titles(groups)).toEqual(["Shortcuts"]);
  });

  it("drops a group with nothing in it rather than showing an empty header", () => {
    expect(keyReference(tools, [])).toEqual([]);
  });

  it("carries each command's chords and its live detail onto the row", () => {
    const groups = keyReference(tools, [
      {
        ...command("terminal.new", "New terminal", ["Alt+T"]),
        detail: () => "a shell in this folder",
      },
    ]);
    expect(groups[0]?.rows[0]).toEqual({
      id: "terminal.new",
      title: "New terminal",
      detail: "a shell in this folder",
      chords: ["Alt+T"],
    });
  });

  it("keeps a command with no chord — the QuickBar is still a way to it", () => {
    const groups = keyReference(tools, [command("terminal.new", "New terminal")]);
    expect(groups[0]?.rows[0]?.chords).toEqual([]);
  });
});

// ---- search -----------------------------------------------------------------

describe("search", () => {
  const groups = keyReference(
    [
      tool({
        id: "panes",
        title: "Panes",
        commands: [command("pane.split.right", "x"), command("pane.split.down", "x")],
      }),
      tool({ id: "term", title: "Terminal", commands: [command("terminal.new", "x")] }),
    ],
    [
      command("pane.split.right", "Split this pane to the right…", ["Alt+S"]),
      command("pane.split.down", "Split this pane downwards…", ["Alt+Shift+S"]),
      command("terminal.new", "New terminal", ["Alt+T"]),
    ],
  );

  it("returns everything for an empty query", () => {
    expect(rowCount(filterKeyReference(groups, "   "))).toBe(3);
  });

  it("matches the command's text", () => {
    expect(rowCount(filterKeyReference(groups, "split"))).toBe(2);
  });

  // The reason someone opens this surface is often "what was that chord?" —
  // typed the way it is written, or the way it is pressed.
  it("matches a chord however it is typed", () => {
    for (const query of ["Alt+T", "alt+t", "alt t", "altt"]) {
      expect(rowCount(filterKeyReference(groups, query)), query).toBe(1);
    }
  });

  it("matches a group's name and keeps that whole group", () => {
    const found = filterKeyReference(groups, "panes");
    expect(titles(found)).toEqual(["Panes"]);
    expect(found[0]?.rows).toHaveLength(2);
  });

  it("says none rather than showing an empty section", () => {
    expect(filterKeyReference(groups, "nothing here")).toEqual([]);
  });
});

// ---- the registry the app actually ships ------------------------------------

describe("the shipped registry", () => {
  const reference = (): KeyRefGroup[] => keyReference(TOOLS, builtinCommands());

  /**
   * The anti-rot guarantee.
   *
   * Every command the app registers is reachable from the discovery surface,
   * and every chord shown there is the chord that runs. A command added without
   * a home, a tool renamed out from under its section, or a chord edited in one
   * place and not the other all land here rather than on a user pressing a key
   * that does nothing.
   */
  it("shows every registered command, with the chords that actually run", () => {
    const shown = new Map(
      reference().flatMap((group) => group.rows.map((row) => [row.id, row] as const)),
    );
    for (const registered of builtinCommands()) {
      const row = shown.get(registered.id);
      expect(row, `${registered.id} is unreachable from the keyboard reference`).toBeDefined();
      expect(row?.chords, `${registered.id} shows the wrong chord`).toEqual(registered.keys ?? []);
    }
  });

  it("puts every command in a section named after the window or a real tool", () => {
    const names = new Set(["Window", ...TOOLS.map((registered) => registered.title)]);
    for (const group of reference()) {
      expect(names.has(group.title) || group.id.startsWith("category:"), group.title).toBe(true);
    }
  });

  // The chord this whole surface is opened by. If it moves, the welcome card,
  // the status chip and the reference's own footer follow it — because all
  // three ask `chordFor`, and this is what proves the answer is not empty.
  it("answers the chord of a registered command, and nothing for an unknown one", () => {
    expect(chordFor("keys.open")).toBe("Alt+K");
    expect(chordFor("pane.split.right")).toBe("Alt+S");
    expect(chordFor("nothing.here")).toBe("");
  });

  it("builds a tooltip that names the chord, and one that does not when there is none", () => {
    expect(chordTooltip("Split right", "pane.split.right")).toBe("Split right — Alt+S");
    expect(chordTooltip("Open Scratchpad", "scratchpad.open")).toBe("Open Scratchpad");
  });

  /**
   * The four affordances the welcome card offers have to be real commands.
   *
   * They are named by id in `panels/Keyboard.tsx`, which is the one place this
   * feature hardcodes anything — a row whose command does not exist is silently
   * dropped at render, so without this the welcome could quietly lose a step.
   */
  it("registers every command the welcome card offers", () => {
    const ids = new Set(builtinCommands().map((registered) => registered.id));
    for (const id of ["quickbar.files", "pane.split.right", "session.new", "quickbar.commands"]) {
      expect(ids.has(id), `the welcome card offers ${id}, which nothing registers`).toBe(true);
    }
  });
});
