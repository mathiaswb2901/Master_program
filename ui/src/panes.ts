/**
 * Panes: what a pane *is*, what it is called, and how the keyboard moves
 * between them.
 *
 * Workbench is a tiling window manager that happens to host an editor. A
 * **pane** is one dockview group: it holds one tool, it can be split in two,
 * and the arrangement belongs to the user rather than to the app. This module
 * is the pure half of that — free of React, the store, dockview's runtime and
 * `TOOLS` (types only) — so the two rules that decide whether an arrangement
 * survives a restart are unit-tested rather than assumed (`panes.test.ts`).
 *
 * ## Pane identity — the one idea to keep in your head
 *
 * ```
 * paneId := toolId                    the tool's single default pane
 *         | toolId "#" instanceKey    one instance of a plural tool
 * ```
 *
 * dockview serializes **panel ids** into `.workbench/layouts.json` and nothing
 * else about a panel's contents. So the pane id *is* the persistence: whatever
 * a pane is bound to has to be expressible in that string, or it does not come
 * back. Three consequences, all deliberate:
 *
 *  - the binding is a **string chosen by the tool**, and it must still mean the
 *    same thing after a restart. `agent#<session_id>` restores that
 *    conversation; `editors#<path>` restores that file; `terminal#<n>` restores
 *    terminal *n* (see below);
 *  - no second store, no id map, no migration. A pane the user dragged, saved
 *    under a layout name and reopened a week later carries its own meaning;
 *  - a pane id is a **contract**, exactly as a tool id is. Changing what a key
 *    means renames the user's saved arrangement.
 *
 * What `terminal#2` honestly restores is the *pane*, not the process: the PTY
 * dies with the WebSocket when the page goes away and the server releases it,
 * so the pane comes back in the same place with the same number and a fresh
 * shell. No layout file can promise more than that.
 *
 * ## Why the separator is `#`
 *
 * Split on the **first** `#`, so an instance key may contain one (a file called
 * `notes#2.md`). Tool ids may not — `panes.test.ts` asserts it against the real
 * registry, because a tool id with a `#` in it would make every pane of that
 * tool address a different tool.
 */

/** Divides a pane id's tool from its instance key. */
export const PANE_SEPARATOR = "#";

export interface ParsedPaneId {
  toolId: string;
  /** null = the tool's one default pane (`agent`, not `agent#…`). */
  instance: string | null;
}

/** `("agent", "sess-1")` -> `"agent#sess-1"`; a null instance is the tool id. */
export function paneId(toolId: string, instance: string | null): string {
  return instance === null ? toolId : `${toolId}${PANE_SEPARATOR}${instance}`;
}

/** Inverse of `paneId`. An empty key (`"agent#"`) is no key at all. */
export function parsePaneId(id: string): ParsedPaneId {
  const cut = id.indexOf(PANE_SEPARATOR);
  if (cut < 0) return { toolId: id, instance: null };
  const instance = id.slice(cut + PANE_SEPARATOR.length);
  return { toolId: id.slice(0, cut), instance: instance === "" ? null : instance };
}

/** The tool a pane hosts — what `panelComponents` is keyed by. */
export const paneTool = (id: string): string => parsePaneId(id).toolId;

/** The instance key a pane carries, or null for a tool's default pane. */
export const paneInstance = (id: string): string | null => parsePaneId(id).instance;

// ---- geometry ---------------------------------------------------------------

export type PaneDirection = "left" | "right" | "up" | "down";

export const PANE_DIRECTIONS: readonly PaneDirection[] = ["left", "right", "up", "down"];

/** Where a pane's *contents* go when it trades places with a neighbour. */
export function oppositeDirection(direction: PaneDirection): PaneDirection {
  switch (direction) {
    case "left":
      return "right";
    case "right":
      return "left";
    case "up":
      return "down";
    case "down":
      return "up";
  }
}

/** One pane's box on screen, in CSS pixels. Whatever produced it — a live
 * `getBoundingClientRect()` or a fixture — this module only needs these five. */
export interface PaneRect {
  id: string;
  left: number;
  top: number;
  width: number;
  height: number;
}

const right = (rect: PaneRect): number => rect.left + rect.width;
const bottom = (rect: PaneRect): number => rect.top + rect.height;
const centreX = (rect: PaneRect): number => rect.left + rect.width / 2;
const centreY = (rect: PaneRect): number => rect.top + rect.height / 2;

/**
 * Sub-pixel slack. dockview's grid puts a sash between panes and rounds sizes,
 * so "starts where the other ends" is never exactly equal — and a strict
 * comparison would make the neighbour to the right invisible on some window
 * widths but not others.
 */
const EPSILON = 2;

/** True when the two panes share any extent on the axis across the movement. */
function overlaps(from: PaneRect, to: PaneRect, direction: PaneDirection): boolean {
  return direction === "left" || direction === "right"
    ? to.top < bottom(from) - EPSILON && from.top < bottom(to) - EPSILON
    : to.left < right(from) - EPSILON && from.left < right(to) - EPSILON;
}

/** How far past this pane's edge the candidate begins. Negative = not past it. */
function gap(from: PaneRect, to: PaneRect, direction: PaneDirection): number {
  switch (direction) {
    case "left":
      return from.left - right(to);
    case "right":
      return to.left - right(from);
    case "up":
      return from.top - bottom(to);
    case "down":
      return to.top - bottom(from);
  }
}

/** Distance along the *other* axis — the tie-break between two panes stacked
 * beside each other, so "right" from a tall pane lands on the one you are
 * looking at rather than on whichever the grid serialized first. */
function offAxis(from: PaneRect, to: PaneRect, direction: PaneDirection): number {
  return direction === "left" || direction === "right"
    ? Math.abs(centreY(to) - centreY(from))
    : Math.abs(centreX(to) - centreX(from));
}

/**
 * The pane the keyboard moves to, or null at the edge of the window.
 *
 * Two passes, in this order, because they answer different questions:
 *
 *  1. panes that begin past this one's edge **and overlap it** on the other
 *    axis — "the one next to me". Nearest gap wins, then nearest centre;
 *  2. failing that, any pane past the edge at all — the L-shaped case, where
 *    the arrangement has no true neighbour in that direction but the user still
 *    means "go that way". Nearest centre wins.
 *
 * Without the second pass a pane in the corner of a three-pane arrangement is a
 * dead end in one direction, which reads as the key being broken.
 */
export function paneInDirection(
  rects: readonly PaneRect[],
  fromId: string,
  direction: PaneDirection,
): string | null {
  const from = rects.find((rect) => rect.id === fromId);
  if (from === undefined) return null;
  const beyond = rects.filter(
    (rect) => rect.id !== fromId && gap(from, rect, direction) > -EPSILON,
  );
  const adjacent = beyond.filter((rect) => overlaps(from, rect, direction));
  const pool = adjacent.length > 0 ? adjacent : beyond;
  let best: PaneRect | null = null;
  let bestScore = 0;
  for (const rect of pool) {
    const score =
      adjacent.length > 0
        ? gap(from, rect, direction) * 1_000_000 + offAxis(from, rect, direction)
        : offAxis(from, rect, direction) + gap(from, rect, direction);
    if (best === null || score < bestScore) {
      best = rect;
      bestScore = score;
    }
  }
  return best?.id ?? null;
}

/**
 * Panes in reading order — top row left to right, then the next row down.
 *
 * This is the order `Alt+O` cycles in, and it is deliberately *screen* order
 * rather than the grid's tree order: a user cycling panes is looking at the
 * window, not at dockview's serialization. Rows are bucketed by top edge with
 * the same slack the neighbour search uses, so two panes whose tops differ by a
 * sash still count as one row.
 */
export function readingOrder(rects: readonly PaneRect[]): string[] {
  return [...rects]
    .sort((a, b) =>
      Math.abs(a.top - b.top) <= EPSILON ? a.left - b.left : a.top - b.top,
    )
    .map((rect) => rect.id);
}

/** The next pane in reading order, wrapping. `step` of -1 goes back. */
export function cyclePane(
  rects: readonly PaneRect[],
  fromId: string,
  step: number,
): string | null {
  const order = readingOrder(rects);
  if (order.length === 0) return null;
  const at = order.indexOf(fromId);
  const next = ((at < 0 ? 0 : at + step) % order.length + order.length) % order.length;
  return order[next] ?? null;
}
