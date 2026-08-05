/**
 * Geometry for the scene graph: value scales, DAG layering, line matching.
 *
 * Everything here is a pure function of the payload, which is the point — the
 * model sends structure and numbers and *this file* decides where the pixels
 * go, so the decisions are unit-testable rather than eyeballed in a screenshot.
 * Nothing imports React, the store, or the DOM.
 *
 * Bounds are enforced server-side (`models/visuals.py`), so the loops below can
 * be honest about their cost: `matchLines` is O(before × after) over at most
 * 120 lines a side, `layerNodes` linear over at most 20 nodes and 30 edges.
 */

import type { DiagramEdge, DiagramNode } from "../types";

// ---- value axes --------------------------------------------------------------

export interface Tick {
  value: number;
  label: string;
}

/**
 * The 1-2-5 step *nearest* the raw interval (Heckbert's nicenum, rounding
 * variant) — the ticks a person would pick.
 *
 * Nearest, not next-largest: rounding 25 up to 50 leaves a 0–100 axis with
 * three gridlines and a chart you have to interpolate by eye, while rounding it
 * to 20 gives the six a reader expects. The target below is a target, not a cap.
 */
function niceStep(raw: number): number {
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalized = raw / magnitude;
  const step = normalized < 1.5 ? 1 : normalized < 3 ? 2 : normalized < 7 ? 5 : 10;
  return step * magnitude;
}

/** Decimals needed so two adjacent ticks do not print the same string. */
function decimalsFor(step: number): number {
  if (step >= 1) return 0;
  return Math.min(6, Math.ceil(-Math.log10(step)));
}

export function formatTick(value: number, step: number): string {
  const text = value.toFixed(decimalsFor(step));
  // "-0" is a rounding artefact, and on a price axis it reads as a real
  // negative hour that is not there.
  return text === `-${(0).toFixed(decimalsFor(step))}` ? text.slice(1) : text;
}

/**
 * Ticks covering [min, max], rounded outward so the domain is a whole number of
 * steps. Returns the domain it settled on: the chart must scale to *that*, not
 * to the raw data, or the top gridline floats off the plot.
 */
export function niceTicks(
  min: number,
  max: number,
  target = 4,
): { ticks: Tick[]; domain: [number, number] } {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { ticks: [], domain: [0, 1] };
  if (min === max) {
    // A flat series still needs an axis; give it one unit of air either side.
    const pad = Math.abs(min) > 0 ? Math.abs(min) * 0.1 : 1;
    min -= pad;
    max += pad;
  }
  const step = niceStep((max - min) / Math.max(1, target));
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks: Tick[] = [];
  // Multiply rather than accumulate: adding a float step 20 times drifts, and
  // the drift shows up as 59.99999999999999 on an axis label.
  const count = Math.round((hi - lo) / step);
  for (let i = 0; i <= count; i += 1) {
    const value = lo + i * step;
    ticks.push({ value, label: formatTick(value, step) });
  }
  return { ticks, domain: [lo, hi] };
}

/** Powers of ten spanning the data — a log axis with 1-2-5 ticks is unreadable
 * at this size, and the decades are what the reader is there for. */
export function logTicks(min: number, max: number): { ticks: Tick[]; domain: [number, number] } {
  const lo = Math.max(min, Number.MIN_VALUE);
  const from = Math.floor(Math.log10(lo));
  const to = Math.ceil(Math.log10(Math.max(max, lo * 10)));
  const ticks: Tick[] = [];
  for (let exponent = from; exponent <= to; exponent += 1) {
    const value = 10 ** exponent;
    ticks.push({
      value,
      label: exponent >= 0 && exponent < 5 ? String(value) : `1e${String(exponent)}`,
    });
  }
  return { ticks, domain: [10 ** from, 10 ** to] };
}

/** Domain → pixel range. Log scales map the logarithm, so the caller passes the
 * scale rather than pre-transforming and losing the tick values. */
export function scale(
  domain: [number, number],
  range: [number, number],
  kind: "linear" | "log" = "linear",
): (value: number) => number {
  const [d0, d1] = kind === "log" ? [Math.log10(domain[0]), Math.log10(domain[1])] : domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (value: number) => {
    const v = kind === "log" ? Math.log10(Math.max(value, Number.MIN_VALUE)) : value;
    return r0 + ((v - d0) / span) * (r1 - r0);
  };
}

/** Smallest gap between consecutive x values — the width one bar or step gets
 * on a value axis (a time grid has a real step and never asks). */
export function minimumGap(xs: number[]): number {
  let gap = Infinity;
  for (let i = 1; i < xs.length; i += 1) gap = Math.min(gap, Math.abs(xs[i] - xs[i - 1]));
  return Number.isFinite(gap) && gap > 0 ? gap : 1;
}

// ---- diagram layering --------------------------------------------------------

export interface PlacedNode {
  id: string;
  layer: number;
  /** Position within the layer, and how many share it — the renderer turns the
   * pair into pixels, so the layout stays independent of the canvas size. */
  index: number;
  layerSize: number;
}

/**
 * Longest-path layering: a node sits one layer below its deepest predecessor,
 * so every edge points strictly downward and no edge is drawn backwards.
 *
 * The payload is guaranteed acyclic (validated server-side), and this depends
 * on it: the queue below drains exactly once per node. A cycle would leave
 * nodes unplaced rather than loop — they fall to layer 0, which is a visibly
 * wrong picture rather than a hung tab, but the schema is what prevents it.
 */
export function layerNodes(nodes: DiagramNode[], edges: DiagramEdge[]): PlacedNode[] {
  const layer = new Map<string, number>(nodes.map((node) => [node.id, 0]));
  const indegree = new Map<string, number>(nodes.map((node) => [node.id, 0]));
  const out = new Map<string, string[]>(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    out.get(edge.source)?.push(edge.target);
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1);
  }
  const queue = nodes.filter((node) => indegree.get(node.id) === 0).map((node) => node.id);
  for (let head = 0; head < queue.length; head += 1) {
    const id = queue[head];
    for (const target of out.get(id) ?? []) {
      layer.set(target, Math.max(layer.get(target) ?? 0, (layer.get(id) ?? 0) + 1));
      const remaining = (indegree.get(target) ?? 0) - 1;
      indegree.set(target, remaining);
      if (remaining === 0) queue.push(target);
    }
  }
  // Registry order inside a layer: the model listed its nodes in the order it
  // thinks about them, and no crossing-minimisation heuristic beats that at
  // twenty nodes.
  const counts = new Map<number, number>();
  const placed = nodes.map((node) => {
    const at = layer.get(node.id) ?? 0;
    const index = counts.get(at) ?? 0;
    counts.set(at, index + 1);
    return { id: node.id, layer: at, index, layerSize: 0 };
  });
  return placed.map((node) => ({ ...node, layerSize: counts.get(node.layer) ?? 1 }));
}

// ---- code diff ---------------------------------------------------------------

export type DiffKind = "same" | "add" | "del";

export interface DiffLine {
  kind: DiffKind;
  text: string;
  /** 1-based line numbers, null on the side the line is absent from. */
  beforeNo: number | null;
  afterNo: number | null;
}

function splitLines(body: string): string[] {
  if (body === "") return [];
  const lines = body.split("\n");
  // A trailing newline ends the last line; it is not an empty final line.
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/**
 * Longest-common-subsequence line matching, then a walk back through the table.
 *
 * A real diff rather than two panes side by side: the question a before/after
 * answers is "what changed", and two panes make the reader find it. Bounded by
 * the server's 120-line cap, so the table is at most 120 × 120 cells.
 */
export function matchLines(before: string, after: string): DiffLine[] {
  const a = splitLines(before);
  const b = splitLines(after);
  const table: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] =
        a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i], beforeNo: i + 1, afterNo: j + 1 });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      out.push({ kind: "del", text: a[i], beforeNo: i + 1, afterNo: null });
      i += 1;
    } else {
      out.push({ kind: "add", text: b[j], beforeNo: null, afterNo: j + 1 });
      j += 1;
    }
  }
  for (; i < a.length; i += 1)
    out.push({ kind: "del", text: a[i], beforeNo: i + 1, afterNo: null });
  for (; j < b.length; j += 1)
    out.push({ kind: "add", text: b[j], beforeNo: null, afterNo: j + 1 });
  return out;
}
