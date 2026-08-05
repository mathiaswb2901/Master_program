/**
 * The claim the per-leaf caps do not make: **a legal card cannot wedge the
 * renderer.**
 *
 * Every cap in `models/visuals.py` bounds one leaf. None of them bounds their
 * product, and the product is what a browser actually pays: eight leaves, each
 * a chart inside *its* cap, is 8 x 6 x 400 = 19,200 SVG marks in one node, and
 * a card holds three of those — 57,600 elements with every individual cap
 * respected. That is why `MAX_VISUAL_MARKS` exists, and this file is the other
 * half of it: the server proves the ceiling is enforced
 * (`test_visuals.py::TestRenderBudget`), and this proves the ceiling is one the
 * renderer can actually draw.
 *
 * The payload below is the worst case the schema permits at that ceiling —
 * scatter charts (one `<circle>` per point, where a line emits one path),
 * full 50x8 tables, a 20-node diagram, a diff whose sides share no line — and
 * it is built to the same shape as the Python helper of the same name, so the
 * two describe one payload rather than two guesses.
 *
 * Measured on the author's machine over four runs: 18,000 marks become 20,619
 * elements in 174–275 ms. The budgets below carry a wide margin on the clock —
 * this is a regression fence against an *order-of-magnitude* change (a per-mark
 * `useId`, a per-point wrapper `<g>`, a quadratic pass over the series), not a
 * benchmark, and the element count is the assertion that actually bites.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { CodeDiffLeaf, DiagramLeaf, MetricsLeaf, TableLeaf, VisualLeaf, VisualNode } from "../types";
import { VisualView } from "./Visual";

// Mirrors the caps in `server/src/workbench_server/models/visuals.py`, the same
// way `layout.test.ts` mirrors MAX_DIFF_LINES. A change there that is not
// mirrored here shows up as a mark count that no longer lands on the total.
const MAX_SERIES = 6;
const MAX_POINTS = 400;
const MAX_TABLE_ROWS = 50;
const MAX_TABLE_COLUMNS = 8;
const MAX_DIAGRAM_NODES = 20;
const MAX_DIAGRAM_EDGES = 30;
const MAX_DIFF_LINES = 120;
const MAX_METRICS = 6;
const MAX_VISUAL_LEAVES = 8;
const MAX_VISUAL_NODES = 3;
const MAX_VISUAL_MARKS = 6000;

function fullChart(points = MAX_POINTS, series = MAX_SERIES): VisualLeaf {
  return {
    kind: "chart",
    title: "",
    x: { kind: "value", label: "", unit: "", scale: "linear" },
    y: { kind: "value", label: "", unit: "", scale: "linear" },
    y_right: null,
    series: Array.from({ length: series }, (_, s) => ({
      label: `s${String(s)}`,
      style: "scatter" as const,
      values: Array.from({ length: points }, (_, i) => i),
      x: [],
      axis: "left" as const,
    })),
  };
}

function fullTable(): TableLeaf {
  return {
    kind: "table",
    title: "",
    columns: Array.from({ length: MAX_TABLE_COLUMNS }, (_, c) => ({
      label: `c${String(c)}`,
      type: "numeric" as const,
      unit: "",
    })),
    rows: Array.from({ length: MAX_TABLE_ROWS }, (_, r) =>
      Array.from({ length: MAX_TABLE_COLUMNS }, (_, c) => `${String(r)}.${String(c)}`),
    ),
    highlights: [],
  };
}

function fullDiagram(): DiagramLeaf {
  // Forward-only edges, so it stays the acyclic DAG the layout needs.
  const edges = [1, 2].flatMap((span) =>
    Array.from({ length: MAX_DIAGRAM_NODES - span }, (_, i) => ({
      source: `n${String(i)}`,
      target: `n${String(i + span)}`,
      label: "",
    })),
  );
  return {
    kind: "diagram",
    title: "",
    nodes: Array.from({ length: MAX_DIAGRAM_NODES }, (_, i) => ({
      id: `n${String(i)}`,
      label: `N${String(i)}`,
      role: "neutral",
    })),
    edges: edges.slice(0, MAX_DIAGRAM_EDGES),
  };
}

function fullDiff(): CodeDiffLeaf {
  // No line in common, so the matcher really does emit one row per line of
  // each side — 240, not the 120 two identical sides would collapse to.
  const side = (text: string) =>
    Array.from({ length: MAX_DIFF_LINES }, () => text).join("\n");
  return { kind: "code_diff", title: "", language: "python", before: side("x = 1"), after: side("y = 2") };
}

function fullMetrics(): MetricsLeaf {
  return {
    kind: "metrics",
    title: "",
    items: Array.from({ length: MAX_METRICS }, (_, i) => ({
      label: `m${String(i)}`,
      value: "1",
      unit: "",
      role: "neutral",
    })),
  };
}

/** `MAX_VISUAL_LEAVES` leaves totalling exactly `MAX_VISUAL_MARKS` — the
 * mirror of `worst_case_blocks(MAX_VISUAL_MARKS)` in `test_visuals.py`. */
function worstCaseNode(index: number): VisualNode {
  const fixed: VisualLeaf[] = [
    fullChart(),
    fullChart(),
    fullTable(),
    fullTable(),
    fullDiagram(),
    fullDiff(),
    fullMetrics(),
  ];
  const spent =
    2 * MAX_SERIES * MAX_POINTS +
    2 * MAX_TABLE_ROWS * MAX_TABLE_COLUMNS +
    (MAX_DIAGRAM_NODES + MAX_DIAGRAM_EDGES) +
    2 * MAX_DIFF_LINES +
    MAX_METRICS;
  const items = [...fixed, fullChart(MAX_VISUAL_MARKS - spent, 1)];
  expect(items).toHaveLength(MAX_VISUAL_LEAVES);
  return {
    kind: "visual",
    node_id: `v${String(index)}`,
    title: "",
    blocks: [
      { layout: "row", items: items.slice(0, 4) },
      { layout: "row", items: items.slice(4) },
    ],
  };
}

/** Occurrences of a global pattern — the marks the renderer actually emitted. */
function count(html: string, pattern: RegExp): number {
  return html.match(pattern)?.length ?? 0;
}

describe("a card at every cap at once", () => {
  it("draws every mark the budget paid for, and no more", () => {
    const html = renderToStaticMarkup(<VisualView node={worstCaseNode(0)} />);
    const marks = {
      points: count(html, /class="wb-vis-dot"/g),
      cells: count(html, /<td /g),
      boxes: count(html, /class="wb-vis-node"/g),
      arrows: count(html, /class="wb-vis-edge"/g),
      diffLines: count(html, /class="wb-vis-diff-line is-/g),
      figures: count(html, /class="wb-vis-metric"/g),
    };
    expect(marks).toEqual({
      points: 2 * MAX_SERIES * MAX_POINTS + (MAX_VISUAL_MARKS - 5896),
      cells: 2 * MAX_TABLE_ROWS * MAX_TABLE_COLUMNS,
      boxes: MAX_DIAGRAM_NODES,
      arrows: MAX_DIAGRAM_EDGES,
      diffLines: 2 * MAX_DIFF_LINES,
      figures: MAX_METRICS,
    });
    // The server's number and the renderer's are the same number. If they ever
    // diverge, one of the two mirrored cap lists moved without the other.
    const drawn = Object.values(marks).reduce((sum, n) => sum + n, 0);
    expect(drawn).toBe(MAX_VISUAL_MARKS);
  });

  it("renders the whole worst-case card inside a wall-clock budget", () => {
    const nodes = Array.from({ length: MAX_VISUAL_NODES }, (_, i) => worstCaseNode(i));
    const started = Date.now();
    const html = nodes.map((node) => renderToStaticMarkup(<VisualView node={node} />)).join("");
    const elapsed = Date.now() - started;

    const elements = count(html, /<[a-zA-Z]/g);
    // 18,000 marks plus the axes, legends, headers and wrappers around them.
    // Linear in the marks, which is the property under test: a renderer that
    // wrapped every point would blow this long before the clock did.
    expect(elements).toBeGreaterThan(MAX_VISUAL_NODES * MAX_VISUAL_MARKS);
    expect(elements).toBeLessThan(1.5 * MAX_VISUAL_NODES * MAX_VISUAL_MARKS);
    expect(elapsed, `${String(elapsed)}ms for ${String(elements)} elements`).toBeLessThan(4000);
  });
});
