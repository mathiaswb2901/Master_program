import { describe, expect, it } from "vitest";

import {
  chordKeycaps,
  isIntercepted,
  matchesChord,
  parseChord,
  resolveCommand,
  type Bindable,
  type KeyLike,
} from "./keys";

const press = (key: string, mods: Partial<KeyLike> = {}): KeyLike => ({
  key,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  ...mods,
});

describe("parseChord", () => {
  it("splits modifiers from the key", () => {
    expect(parseChord("Ctrl+Shift+P")).toEqual({ ctrl: true, alt: false, shift: true, key: "p" });
  });

  it("treats Cmd as Ctrl so one chord covers both platforms", () => {
    expect(parseChord("Cmd+K")).toEqual(parseChord("Ctrl+K"));
  });

  it("keeps multi-character key names", () => {
    expect(parseChord("Ctrl+PageDown").key).toBe("pagedown");
    expect(parseChord("Ctrl+F4").key).toBe("f4");
  });
});

describe("matchesChord", () => {
  it("matches modifiers exactly, so Ctrl+P is not Ctrl+Shift+P", () => {
    const ctrlP = parseChord("Ctrl+P");
    const ctrlShiftP = parseChord("Ctrl+Shift+P");
    expect(matchesChord(press("p", { ctrlKey: true }), ctrlP)).toBe(true);
    expect(matchesChord(press("P", { ctrlKey: true, shiftKey: true }), ctrlP)).toBe(false);
    expect(matchesChord(press("P", { ctrlKey: true, shiftKey: true }), ctrlShiftP)).toBe(true);
    expect(matchesChord(press("p", { ctrlKey: true }), ctrlShiftP)).toBe(false);
  });

  it("accepts Cmd for Ctrl", () => {
    expect(matchesChord(press("k", { metaKey: true }), parseChord("Ctrl+K"))).toBe(true);
  });

  it("falls back to the physical key for digits", () => {
    // Alt+1 reports a composed character on several layouts.
    expect(matchesChord(press("¡", { altKey: true, code: "Digit1" }), parseChord("Alt+1"))).toBe(
      true,
    );
    expect(matchesChord(press("¡", { altKey: true, code: "Digit2" }), parseChord("Alt+1"))).toBe(
      false,
    );
  });

  it("is case-insensitive on the key", () => {
    expect(matchesChord(press("W", { altKey: true }), parseChord("Alt+W"))).toBe(true);
  });
});

describe("pass-through policy", () => {
  it("never intercepts plain keys, anywhere", () => {
    for (const surface of ["other", "editor", "terminal"] as const) {
      expect(isIntercepted(parseChord("Enter"), surface)).toBe(false);
      expect(isIntercepted(parseChord("Shift+A"), surface)).toBe(false);
    }
  });

  it("leaves plain Ctrl chords to xterm and Monaco", () => {
    // Ctrl+K kills a line, Ctrl+P walks shell history — theirs, not ours.
    expect(isIntercepted(parseChord("Ctrl+K"), "terminal")).toBe(false);
    expect(isIntercepted(parseChord("Ctrl+P"), "editor")).toBe(false);
    expect(isIntercepted(parseChord("Ctrl+PageDown"), "editor")).toBe(false);
  });

  it("takes Ctrl chords everywhere else", () => {
    expect(isIntercepted(parseChord("Ctrl+K"), "other")).toBe(true);
    expect(isIntercepted(parseChord("Ctrl+S"), "other")).toBe(true);
  });

  it("always takes Alt and Ctrl+Shift chords", () => {
    for (const surface of ["other", "editor", "terminal"] as const) {
      expect(isIntercepted(parseChord("Ctrl+Shift+P"), surface)).toBe(true);
      expect(isIntercepted(parseChord("Alt+1"), surface)).toBe(true);
      expect(isIntercepted(parseChord("Alt+PageDown"), surface)).toBe(true);
    }
  });
});

describe("resolveCommand", () => {
  const commands: (Bindable & { id: string })[] = [
    { id: "quickbar.files", keys: ["Ctrl+P", "Ctrl+K"] },
    { id: "quickbar.commands", keys: ["Ctrl+Shift+P"] },
    { id: "file.save", keys: ["Ctrl+S"], when: () => false },
    { id: "editor.close", keys: ["Ctrl+F4", "Alt+W"] },
  ];

  it("resolves any of a command's chords", () => {
    expect(resolveCommand(press("k", { ctrlKey: true }), "other", commands)?.id).toBe(
      "quickbar.files",
    );
    expect(resolveCommand(press("F4", { ctrlKey: true }), "other", commands)?.id).toBe(
      "editor.close",
    );
  });

  it("does not confuse Ctrl+P with Ctrl+Shift+P", () => {
    expect(resolveCommand(press("P", { ctrlKey: true, shiftKey: true }), "other", commands)?.id).toBe(
      "quickbar.commands",
    );
  });

  it("passes plain Ctrl chords through to a focused terminal", () => {
    expect(resolveCommand(press("p", { ctrlKey: true }), "terminal", commands)).toBeNull();
    // …while the Alt twin still reaches the app from inside the terminal.
    expect(resolveCommand(press("w", { altKey: true }), "terminal", commands)?.id).toBe(
      "editor.close",
    );
  });

  it("skips commands whose `when` is false instead of swallowing the key", () => {
    expect(resolveCommand(press("s", { ctrlKey: true }), "other", commands)).toBeNull();
  });

  it("ignores unbound keystrokes", () => {
    expect(resolveCommand(press("q", { ctrlKey: true }), "other", commands)).toBeNull();
  });
});

describe("chordKeycaps", () => {
  it("renders human keycap labels", () => {
    expect(chordKeycaps("Ctrl+Shift+P")).toEqual(["Ctrl", "Shift", "P"]);
    expect(chordKeycaps("Ctrl+PageDown")).toEqual(["Ctrl", "PgDn"]);
    expect(chordKeycaps("Alt+1")).toEqual(["Alt", "1"]);
  });
});
