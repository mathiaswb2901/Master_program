import { describe, expect, it } from "vitest";

import { toolTargetPath } from "./toolTarget";
import type { TreeNode } from "./types";

const dir = (name: string, path: string, children: TreeNode[]): TreeNode => ({
  name,
  path,
  kind: "dir",
  children,
});

const file = (name: string, path: string): TreeNode => ({
  name,
  path,
  kind: "file",
  children: null,
});

const tree: TreeNode = dir("ws", "", [
  dir("se3", "se3", [file("model.py", "se3/model.py"), file("bids.py", "se3/bids.py")]),
  dir("se4", "se4", [file("model.py", "se4/model.py")]),
  file("README.md", "README.md"),
]);

describe("toolTargetPath", () => {
  it("resolves a workspace-relative summary", () => {
    expect(toolTargetPath("Edit: se3/model.py", tree)).toBe("se3/model.py");
  });

  it("resolves an absolute path by suffix", () => {
    expect(toolTargetPath("Read: C:/Users/a/ws/se3/bids.py", tree)).toBe("se3/bids.py");
  });

  it("normalizes Windows separators", () => {
    expect(toolTargetPath("Edit: C:\\Users\\a\\ws\\se3\\model.py", tree)).toBe("se3/model.py");
  });

  it("strips surrounding quotes", () => {
    expect(toolTargetPath('Edit: "README.md"', tree)).toBe("README.md");
  });

  it("refuses ambiguous suffixes rather than opening the wrong file", () => {
    expect(toolTargetPath("Edit: /elsewhere/ws/model.py", tree)).toBeNull();
  });

  it("returns null without a tree, a colon, or a value", () => {
    expect(toolTargetPath("Edit: se3/model.py", null)).toBeNull();
    expect(toolTargetPath("Bash", tree)).toBeNull();
    expect(toolTargetPath("Edit:   ", tree)).toBeNull();
  });

  it("returns null for a path outside the tree", () => {
    expect(toolTargetPath("Read: se3/missing.py", tree)).toBeNull();
  });
});
