import { describe, expect, it } from "vitest";

import {
  type DirMap,
  applyFileChange,
  compareEntries,
  parentPath,
  reconcileTargets,
  visibleRows,
} from "./filetree";
import type { DirEntry, FileChangedEvent } from "./types";

const dir = (path: string): DirEntry => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  kind: "dir",
});
const file = (path: string): DirEntry => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  kind: "file",
});

const change = (path: string, over: Partial<FileChangedEvent> = {}): FileChangedEvent => ({
  type: "file_changed",
  path,
  change: "added",
  hash: null,
  is_dir: false,
  origin: "watcher",
  ...over,
});

/** The workspace every case below patches: root loaded, `src` loaded. */
const loaded: DirMap = {
  "": [dir("src"), file("notes.md")],
  src: [dir("src/deep"), file("src/app.py")],
};

describe("visibleRows", () => {
  it("flattens only what is expanded", () => {
    expect(visibleRows(loaded, new Set()).map((r) => r.path)).toEqual(["src", "notes.md"]);
    expect(visibleRows(loaded, new Set(["src"])).map((r) => [r.path, r.depth])).toEqual([
      ["src", 0],
      ["src/deep", 1],
      ["src/app.py", 1],
      ["notes.md", 0],
    ]);
  });

  it("shows an expanded-but-unlisted folder as itself and nothing more", () => {
    // The listing is one scandir away; a placeholder row that lives for 8 ms is
    // worse than no row.
    const rows = visibleRows(loaded, new Set(["src", "src/deep"]));
    expect(rows.map((r) => r.path)).toEqual(["src", "src/deep", "src/app.py", "notes.md"]);
  });
});

describe("applyFileChange", () => {
  it("inserts an added file in the server's own order", () => {
    const next = applyFileChange(loaded, change("src/bid.py"));
    expect(next.src?.map((e) => e.path)).toEqual(["src/deep", "src/app.py", "src/bid.py"]);
    // Directories stay above files whatever the name.
    const withDir = applyFileChange(next, change("src/Aaa", { is_dir: true }));
    expect(withDir.src?.map((e) => e.path)).toEqual([
      "src/Aaa",
      "src/deep",
      "src/app.py",
      "src/bid.py",
    ]);
  });

  it("returns the same map when nothing a row can show changed", () => {
    // The common case — a save on a file already on screen. Identity is what
    // keeps twenty file changes from costing twenty re-renders.
    expect(applyFileChange(loaded, change("src/app.py", { change: "modified" }))).toBe(loaded);
    // A file deep inside a folder nobody has expanded: no listing to patch.
    expect(applyFileChange(loaded, change("src/deep/x.py"))).toBe(loaded);
    // A deletion of something that was never there.
    expect(applyFileChange(loaded, change("src/ghost.py", { change: "deleted" }))).toBe(loaded);
  });

  it("adds a file the client had never seen, even on a modified event", () => {
    // Convergence, not bookkeeping: whatever the change kind says, the path
    // exists and its parent is on screen.
    const next = applyFileChange(loaded, change("notes2.md", { change: "modified" }));
    expect(next[""]?.map((e) => e.path)).toEqual(["src", "notes.md", "notes2.md"]);
  });

  it("drops a deleted directory and every listing beneath it", () => {
    const deeper: DirMap = { ...loaded, "src/deep": [file("src/deep/a.py")] };
    const next = applyFileChange(deeper, change("src/deep", { change: "deleted" }));
    expect(next.src?.map((e) => e.path)).toEqual(["src/app.py"]);
    expect(next["src/deep"]).toBeUndefined();
  });

  it("never patches a path outside a loaded listing", () => {
    expect(applyFileChange({}, change("anything.txt"))).toEqual({});
    expect(applyFileChange(loaded, change(""))).toBe(loaded);
  });
});

describe("compareEntries", () => {
  it("puts directories first, then case-insensitive name order", () => {
    const sorted = [file("b"), dir("Z"), file("A"), dir("a")].sort(compareEntries);
    expect(sorted.map((e) => e.name)).toEqual(["a", "Z", "A", "b"]);
  });

  it("agrees with the server rather than with the runtime's locale", () => {
    // `localeCompare` — what this panel used to sort with — puts "Aaa" *after*
    // "deep" under nb-NO, where "aa" collates as "å". The server sorts on
    // `name.lower()`, so the tree would have moved a row on every refetch.
    expect(compareEntries(dir("Aaa"), dir("deep"))).toBeLessThan(0);
    expect(compareEntries(file("notes.md"), file("README.md"))).toBeLessThan(0);
  });
});

describe("parentPath", () => {
  it("bottoms out at the workspace root", () => {
    expect(parentPath("a/b/c.txt")).toBe("a/b");
    expect(parentPath("top.txt")).toBe("");
    expect(parentPath("")).toBe("");
  });
});

describe("reconcileTargets", () => {
  it("is the root plus what the user has open — never the whole workspace", () => {
    expect(reconcileTargets(new Set(["src", "src/deep"]))).toEqual(["", "src", "src/deep"]);
  });
});
