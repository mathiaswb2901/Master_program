/**
 * The file tree as data: loaded directory listings, the rows they flatten to,
 * and the patch a watcher event applies to them.
 *
 * Two ideas carry the whole panel:
 *
 * 1. **A tree with everything collapsed is a list.** Flattening the expanded
 *    directories into an array of rows, once, turns rendering into indexing —
 *    which is what makes it virtualisable without a library, and what makes
 *    keyboard navigation "the next index" rather than a tree walk.
 * 2. **A file change is a one-row edit.** The client already receives
 *    `FileChangedEvent` with a path; the parent's listing is the only thing that
 *    can have changed, so the tree patches in place instead of asking the server
 *    to walk 5,005 files again.
 *
 * Everything here is pure and unit-tested (`filetree.test.ts`). The state lives
 * in `store.ts`, the rendering in `panels/FileTree.tsx`, and neither of them
 * repeats a rule from this file.
 */

import type { DirEntry, FileChangedEvent } from "./types";

/** path -> that directory's children. `""` is the workspace root. A path absent
 * from the map has never been listed, which is different from being empty. */
export type DirMap = Readonly<Record<string, readonly DirEntry[]>>;

/** One rendered line. `depth` is indentation; `path` is the identity. */
export interface TreeRow {
  path: string;
  name: string;
  kind: "file" | "dir";
  depth: number;
}

/**
 * The server's order, restated so a row inserted from an event lands exactly
 * where a refetch would have put it: directories first, then lowercased name.
 *
 * Deliberately **not** `localeCompare`, which is what this panel used to sort
 * with. The server sorts on `name.lower()` — plain code-point order — and a
 * locale collator does not agree with it: under `nb-NO`, the runtime locale on
 * the machine this was written on, "Aaa" collates as "Åaa" and sorts *after*
 * "deep". So the tree put a row in one place and a refetch moved it to another,
 * for any user whose locale has its own alphabet. One rule, stated in both
 * languages, is the only version of this that stays true.
 */
export function compareEntries(a: DirEntry, b: DirEntry): number {
  if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
  const left = a.name.toLowerCase();
  const right = b.name.toLowerCase();
  return left < right ? -1 : left > right ? 1 : 0;
}

export function parentPath(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut < 0 ? "" : path.slice(0, cut);
}

export function baseName(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut < 0 ? path : path.slice(cut + 1);
}

export function joinPath(parent: string, name: string): string {
  return parent === "" ? name : `${parent}/${name}`;
}

/** Sorted insert, returning the same array when the entry is already there —
 * the identity check is what keeps a `modified` event from re-rendering. */
function withEntry(entries: readonly DirEntry[], entry: DirEntry): readonly DirEntry[] {
  const at = entries.findIndex((e) => e.path === entry.path);
  if (at >= 0) return entries;
  const next = entries.slice();
  const index = next.findIndex((e) => compareEntries(entry, e) < 0);
  next.splice(index < 0 ? next.length : index, 0, entry);
  return next;
}

/**
 * Flatten the loaded, expanded directories into the rows to render.
 *
 * A directory that is expanded but not yet listed contributes its own row and
 * no children — the listing is one `scandir` away, and a placeholder row that
 * exists for 8 ms is worse than nothing.
 */
export function visibleRows(dirs: DirMap, expanded: ReadonlySet<string>): TreeRow[] {
  const rows: TreeRow[] = [];
  const walk = (path: string, depth: number): void => {
    for (const entry of dirs[path] ?? []) {
      rows.push({ path: entry.path, name: entry.name, kind: entry.kind, depth });
      if (entry.kind === "dir" && expanded.has(entry.path)) walk(entry.path, depth + 1);
    }
  };
  walk("", 0);
  return rows;
}

/**
 * Apply one watcher event to the loaded listings.
 *
 * Returns the *same* map when nothing a tree row can show has changed — which
 * is the common case (a save is a `modified` event for a path already on
 * screen), and what keeps twenty file changes from costing twenty re-renders.
 *
 * Only listings that are actually loaded are patched. An event for a file deep
 * inside a collapsed folder is genuinely nothing to do: the listing that would
 * hold it does not exist yet, and expanding that folder fetches it fresh.
 */
export function applyFileChange(dirs: DirMap, event: FileChangedEvent): DirMap {
  const path = event.path;
  if (path === "") return dirs;
  if (event.change === "deleted") {
    const parent = parentPath(path);
    const siblings = dirs[parent];
    const prefix = path + "/";
    // The path itself, plus every listing under it: deleting a directory
    // arrives as one event for the directory, and its children's listings
    // would otherwise be resurrected by the next expand.
    const stale = Object.keys(dirs).filter((key) => key === path || key.startsWith(prefix));
    const present = siblings !== undefined && siblings.some((e) => e.path === path);
    if (!present && stale.length === 0) return dirs;
    const next: Record<string, readonly DirEntry[]> = { ...dirs };
    for (const key of stale) delete next[key];
    if (present && siblings !== undefined) {
      next[parent] = siblings.filter((e) => e.path !== path);
    }
    return next;
  }
  const parent = parentPath(path);
  const siblings = dirs[parent];
  if (siblings === undefined) return dirs; // parent not loaded — nothing on screen
  const entry: DirEntry = {
    name: baseName(path),
    path,
    kind: event.is_dir ? "dir" : "file",
  };
  const patched = withEntry(siblings, entry);
  if (patched === siblings) return dirs;
  return { ...dirs, [parent]: patched };
}

/** Listings to re-read to converge with disk: the root, and everything the user
 * has open. Collapsed folders are refetched the moment they are expanded, so
 * they are deliberately not in this list — reconciliation costs one `scandir`
 * per directory that is actually on screen, not one per directory that exists. */
export function reconcileTargets(expanded: ReadonlySet<string>): string[] {
  return ["", ...expanded];
}
