/**
 * Resolving a tool-call summary to a workspace file, so "Edit: se3/model.py"
 * becomes a link that opens the real editor tab. Pure and dependency-free —
 * unit-tested in `toolTarget.test.ts`.
 */

import type { TreeNode } from "./types";

const treePathsCache = new WeakMap<TreeNode, string[]>();

function treeFilePaths(tree: TreeNode): string[] {
  const cached = treePathsCache.get(tree);
  if (cached !== undefined) return cached;
  const out: string[] = [];
  const walk = (node: TreeNode): void => {
    if (node.kind === "file") out.push(node.path);
    else (node.children ?? []).forEach(walk);
  };
  walk(tree);
  treePathsCache.set(tree, out);
  return out;
}

/**
 * Workspace file a tool summary refers to, or null. Summaries look like
 * "Edit: <value>" where the value may be workspace-relative or absolute.
 * An ambiguous suffix match (two files with the same tail) resolves to null:
 * opening the wrong file is worse than not offering the link.
 */
export function toolTargetPath(summary: string, tree: TreeNode | null): string | null {
  if (tree === null) return null;
  const colon = summary.indexOf(": ");
  if (colon < 0) return null;
  const value = summary
    .slice(colon + 2)
    .trim()
    .replace(/^["']|["']$/g, "")
    .replace(/\\/g, "/");
  if (value === "") return null;
  const paths = treeFilePaths(tree);
  if (paths.includes(value)) return value;
  const suffixMatches = paths.filter((p) => value.endsWith("/" + p));
  return suffixMatches.length === 1 ? suffixMatches[0] : null;
}
