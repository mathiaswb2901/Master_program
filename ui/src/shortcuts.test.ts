import { describe, expect, it, vi } from "vitest";

import { promptInsertText, shellInsertText, shortcutCommands } from "./shortcuts";
import type { ShortcutEntry } from "./types";

const entry = (over: Partial<ShortcutEntry> = {}): ShortcutEntry => ({
  name: "Status board",
  kind: "shell",
  body: "git status -sb",
  keys: null,
  detail: null,
  source: "workspace",
  ...over,
});

describe("shell insertion", () => {
  // The security invariant: a shortcut inserts, the user executes.
  it("never ends with a newline", () => {
    for (const body of ["git status -sb", "git status -sb\n", "git status -sb\r\n", "ls  \n\n"]) {
      expect(shellInsertText(body).endsWith("\n")).toBe(false);
      expect(shellInsertText(body).endsWith("\r")).toBe(false);
    }
  });

  it("keeps a single-line snippet verbatim", () => {
    expect(shellInsertText("git status -sb\n")).toBe("git status -sb");
  });

  it("carries no newline into the shell at all", () => {
    // Unreachable through the API (the server refuses multi-line shell bodies),
    // but a newline mid-body would run the earlier lines on insert.
    expect(shellInsertText("cd repo\nrm -rf build")).toBe("cd repo");
    expect(shellInsertText("a\nb\nc")).not.toContain("\n");
  });
});

describe("prompt insertion", () => {
  it("replaces an empty draft", () => {
    expect(promptInsertText("", "Review the diff.")).toBe("Review the diff.");
    expect(promptInsertText("   \n", "Review the diff.")).toBe("Review the diff.");
  });

  it("appends to what the user already typed", () => {
    expect(promptInsertText("Context first.", "Review the diff.")).toBe(
      "Context first.\nReview the diff.",
    );
  });

  it("keeps prompt bodies multi-line", () => {
    expect(promptInsertText("", "One.\nTwo.")).toBe("One.\nTwo.");
  });
});

describe("shortcut commands", () => {
  it("maps an entry to a categorized command with its chord", () => {
    const commands = shortcutCommands([entry({ keys: "Alt+G", detail: "branch + status" })], vi.fn());
    expect(commands).toHaveLength(1);
    expect(commands[0]?.id).toBe("shortcut.workspace.Status board");
    expect(commands[0]?.title).toBe("Status board");
    expect(commands[0]?.category).toBe("Shortcuts");
    expect(commands[0]?.keys).toEqual(["Alt+G"]);
    expect(commands[0]?.detail?.()).toBe("branch + status");
  });

  it("leaves an unbound entry chordless and describes it by kind", () => {
    const commands = shortcutCommands([entry({ kind: "prompt", body: "Review." })], vi.fn());
    expect(commands[0]?.keys).toBeUndefined();
    expect(commands[0]?.detail?.()).toBe("prompt template");
  });

  it("runs through the injected handler, never on its own", () => {
    const run = vi.fn();
    const shortcut = entry();
    shortcutCommands([shortcut], run)[0]?.run();
    expect(run).toHaveBeenCalledWith(shortcut);
  });
});
