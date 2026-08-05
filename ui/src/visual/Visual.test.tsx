/**
 * The renderer, and the safety posture that is the reason it exists.
 *
 * A model-authored artifact may not become executable, reach the network, or
 * address the agent. The schema has no field for any of that
 * (`server/tests/test_visuals.py` asserts that half); this file asserts the
 * other half — that the *renderer* cannot be talked into it either, because
 * every payload string becomes a React text node.
 *
 * Same shape as `markdown.test.tsx`, deliberately: that renderer's XSS cases
 * are the pattern, and a visual leaf is a second surface with the same rule.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { anchorKey, partAnchor } from "../plan/anchors";
import type { ChartLeaf, DiagramLeaf, TableLeaf, VisualLeaf, VisualNode } from "../types";
import type { VisualAnnotation } from "./annotate";
import { VisualView } from "./Visual";

const MARKUP = "<script>alert('xss')</script>";

function scene(...items: VisualLeaf[]): VisualNode {
  return {
    kind: "visual",
    node_id: "scene",
    title: "Scene",
    blocks: [{ layout: items.length === 1 ? "single" : "row", items }],
  };
}

function html(node: VisualNode): string {
  return renderToStaticMarkup(<VisualView node={node} />);
}

function table(rows: string[][], extra: Partial<TableLeaf> = {}): TableLeaf {
  return {
    kind: "table",
    title: "Hours",
    columns: [
      { label: "Hour", type: "text", unit: "" },
      { label: "Price", type: "numeric", unit: "EUR/MWh" },
    ],
    rows,
    highlights: [],
    ...extra,
  };
}

const chart: ChartLeaf = {
  kind: "chart",
  title: "Price and dispatch",
  x: {
    kind: "time",
    label: "",
    start: "2026-10-25T00:00:00+02:00",
    step_minutes: 60,
    timezone: "Europe/Stockholm",
  },
  y: { kind: "value", label: "Price", unit: "EUR/MWh", scale: "linear" },
  y_right: { kind: "value", label: "Dispatch", unit: "MW", scale: "linear" },
  series: [
    {
      label: "SE3",
      style: "line",
      values: Array.from({ length: 25 }, (_, i) => i - 6),
      x: [],
      axis: "left",
    },
    {
      label: "Åsen 2",
      style: "step",
      values: Array.from({ length: 25 }, () => 5),
      x: [],
      axis: "right",
    },
  ],
};

const diagram: DiagramLeaf = {
  kind: "diagram",
  title: "Pipeline",
  nodes: [
    { id: "feed", label: "TGN feed", role: "neutral" },
    { id: "opt", label: "Optimizer", role: "accent" },
  ],
  edges: [{ source: "feed", target: "opt", label: "hourly" }],
};

describe("visual renderer — injection safety", () => {
  it("renders a markup-looking table cell as text", () => {
    const out = html(scene(table([[MARKUP, "1"]])));
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("renders markup-looking labels as text everywhere a string reaches", () => {
    const surfaces: VisualLeaf[] = [
      table([["a", "1"]], { title: MARKUP }),
      {
        kind: "diagram",
        title: "",
        nodes: [{ id: "n", label: MARKUP, role: "neutral" }],
        edges: [],
      },
      {
        kind: "metrics",
        title: "",
        items: [{ label: MARKUP, value: MARKUP, unit: MARKUP, role: "error" }],
      },
      { kind: "code_diff", title: "", language: "python", before: MARKUP, after: "" },
    ];
    for (const leaf of surfaces) {
      const out = html(scene(leaf));
      expect(out).not.toContain("<script>");
      expect(out).toContain("&lt;script&gt;");
    }
  });

  it("emits no element that could fetch, embed or execute", () => {
    const out = html({
      ...scene(table([[MARKUP, "1"]]), chart),
      blocks: [
        { layout: "row", items: [table([[MARKUP, "1"]]), chart] },
        { layout: "single", items: [diagram] },
      ],
    });
    for (const tag of [
      "<script",
      "<img",
      "<iframe",
      "<object",
      "<embed",
      "<a ",
      "<form",
      "<link",
    ]) {
      expect(out).not.toContain(tag);
    }
    // No handler and no inline style either: a `style=` built from payload text
    // is the other way markup gets in.
    expect(out).not.toMatch(/\son[a-z]+=/);
    expect(out).not.toContain("style=");
    expect(out).not.toContain("href");
    expect(out).not.toContain("javascript:");
  });

  it("keeps a cell that closes a tag inside the cell", () => {
    const out = html(scene(table([["</td></tr><tr><td>injected", "1"]])));
    expect(out).toContain("&lt;/td&gt;");
    // One row in, one row out — the payload did not restructure the table.
    expect(out.match(/<tr>/g)).toHaveLength(2); // header + the single body row
  });
});

describe("visual renderer — the schema decides the presentation", () => {
  it("marks a numeric column as numeric, whatever its text looks like", () => {
    const out = html(scene(table([["00:00", "not a number"]])));
    expect(out).toContain('class="is-numeric"');
    // ...and does not mark a text column as numeric just because it reads so.
    expect(out).toContain('class="is-text"');
  });

  it("carries a screen-reader word with every role, so colour is not alone", () => {
    const out = html(
      scene(table([["00:00", "-6.1"]], { highlights: [{ row: 0, column: 1, role: "error" }] })),
    );
    expect(out).toContain("Problem");
    expect(out).toContain("is-error");
  });

  it("names the role in words on every leaf that carries one, not just tables", () => {
    // DESIGN.md §7 is "colour never the sole signal", and the roles share one
    // vocabulary across every leaf — so every leaf that tints must also say the
    // word. Tables did from the start; diagram nodes and metrics are the two
    // that tinted silently. The visible half of the pair is a heavier edge than
    // the neutral state wears, which is CSS and is asserted in the E2E journey.
    for (const [role, word] of [
      ["accent", "Highlighted"],
      ["success", "Good"],
      ["warning", "Warning"],
      ["error", "Problem"],
    ] as const) {
      const drawn = html(
        scene({
          kind: "diagram",
          title: "Pipeline",
          nodes: [
            { id: "a", label: "Plain", role: "neutral" },
            { id: "b", label: "Marked", role },
          ],
          edges: [{ source: "a", target: "b", label: "" }],
        }),
      );
      // An `svg` with role="img" has exactly one accessible name, so the word
      // has to be *in* it — a hidden span inside would never be announced.
      expect(drawn).toContain(`Marked (${word})`);
      expect(drawn).toContain(`wb-vis-node is-${role}`);
      // ...and the neutral node is not decorated with a word it has no role for.
      expect(drawn).toContain("Plain,");

      const figures = html(
        scene({
          kind: "metrics",
          title: "",
          items: [
            { label: "Plain", value: "1", unit: "", role: "neutral" },
            { label: "Marked", value: "2", unit: "", role },
          ],
        }),
      );
      expect(figures).toContain(`<span class="u-sr-only">${word}: </span>Marked`);
      expect(figures).toContain(`wb-vis-metric is-${role}`);
      expect(figures).toContain("<dt>Plain</dt>");
    }
  });

  it("draws a step series as steps and a line as a line", () => {
    const out = html(scene(chart));
    // A step path doubles each y as it holds the value across its interval.
    const paths = [...out.matchAll(/ d="([^"]+)"/g)].map((m) => m[1]);
    const step = paths.find((d) => d.split("L").length > 30);
    expect(step).toBeDefined();
    expect(out).toContain("wb-vis-swatch is-step");
    expect(out).toContain("wb-vis-swatch is-line");
  });

  it("labels the long day as 25 hours in the market's own zone", () => {
    const out = html(scene(chart));
    expect(out).toContain("25 Oct");
    expect(out).toContain("Europe/Stockholm");
    expect(out).toContain("25 h");
  });

  it("draws a diagram we laid out, from nodes the model never placed", () => {
    const out = html(scene(diagram));
    expect(out).toContain("<svg");
    expect(out).toContain("TGN feed");
    expect(out).toContain("wb-vis-node is-accent");
    // Two layers: the target sits below the source.
    const ys = [...out.matchAll(/<rect x="[\d.]+" y="([\d.]+)"/g)].map((m) => Number(m[1]));
    expect(new Set(ys).size).toBe(2);
  });

  it("renders every block layout without a leaf escaping its block", () => {
    const node: VisualNode = {
      kind: "visual",
      node_id: "scene",
      title: "",
      blocks: [
        { layout: "single", items: [table([["a", "1"]])] },
        { layout: "split", items: [table([["a", "1"]]), table([["b", "2"]])] },
        { layout: "grid", items: [diagram, chart] },
      ],
    };
    const out = html(node);
    for (const layout of ["is-single", "is-split", "is-grid"]) expect(out).toContain(layout);
  });
});

// ---- anchors -----------------------------------------------------------------

/**
 * The claim the whole anchor design rests on: **the same part emits the same
 * path**, whatever the app is doing around it.
 *
 * A selector-based anchor would fail every case below — a renamed class, a mode
 * class on an ancestor, a highlight that changes an element's classes, a note
 * that adds a marker inside it. A path names data, so none of them touch it.
 */

function annotating(overrides: Partial<VisualAnnotation> = {}): VisualAnnotation {
  return { nodeId: "scene", notes: {}, editing: null, onPick: () => undefined, ...overrides };
}

/** The keys as the DOM will hold them: an anchor key is JSON, and React escapes
 * its quotes on the way into the attribute (a browser hands them back decoded,
 * which is what the E2E journey reads). */
function anchorsIn(node: VisualNode, annotation: VisualAnnotation): string[] {
  const out = renderToStaticMarkup(<VisualView node={node} annotation={annotation} />);
  return [...out.matchAll(/data-anchor="([^"]+)"/g)].map((match) =>
    match[1].replaceAll("&quot;", '"'),
  );
}

/** The path of one table cell, as the renderer stamped it. */
function cellAnchor(node: VisualNode, annotation: VisualAnnotation, row: number): string {
  const wanted = anchorKey(partAnchor("scene", ["leaf", 0, "row", row, "col", "Price"]));
  const found = anchorsIn(node, annotation).find((key) => key === wanted);
  expect(found, `no anchor for row ${String(row)}`).toBeDefined();
  return wanted;
}

describe("visual renderer — anchors", () => {
  const rows = [
    ["02:00 (1st)", "-3.8"],
    ["02:00 (2nd)", "-6.1"],
  ];

  it("emits a path for every anchorable part, one per datum", () => {
    const keys = anchorsIn(scene(table(rows)), annotating());
    // Two rows: a row handle and two cells each.
    expect(keys).toHaveLength(6);
    expect(keys).toContain(anchorKey(partAnchor("scene", ["leaf", 0, "row", 1])));
    expect(keys).toContain(
      anchorKey(partAnchor("scene", ["leaf", 0, "row", 1, "col", "Price"])),
    );
  });

  it("keeps the same path across a state change", () => {
    const node = scene(table(rows));
    const before = cellAnchor(node, annotating(), 1);
    // Two real state changes in the card: a note lands on a *neighbouring*
    // cell (re-render, new markers, new classes) and the editor opens on it.
    const neighbour = anchorKey(partAnchor("scene", ["leaf", 0, "row", 0, "col", "Price"]));
    const after = cellAnchor(
      node,
      annotating({ notes: { [neighbour]: "wrong fold" }, editing: neighbour }),
      1,
    );
    expect(after).toBe(before);
  });

  it("keeps the same path when the payload restyles the very cell it names", () => {
    // A highlight is pure presentation — it changes the cell's classes and adds
    // a screen-reader word. An anchor that moved here would be a selector by
    // another name.
    const plain = cellAnchor(scene(table(rows)), annotating(), 1);
    const styled = cellAnchor(
      scene(table(rows, { highlights: [{ row: 1, column: 1, role: "error" }] })),
      annotating(),
      1,
    );
    expect(styled).toBe(plain);
  });

  it("names a column by its label, and by its index when the label is ambiguous", () => {
    const ambiguous = table(rows, {
      columns: [
        { label: "Price", type: "text", unit: "" },
        { label: "Price", type: "numeric", unit: "EUR/MWh" },
      ],
    });
    const keys = anchorsIn(scene(ambiguous), annotating());
    expect(keys).toContain(anchorKey(partAnchor("scene", ["leaf", 0, "row", 0, "col", 1])));
  });

  it("counts leaves flat over blocks, so the second block's table is leaf 1", () => {
    const node: VisualNode = {
      kind: "visual",
      node_id: "scene",
      title: "",
      blocks: [
        { layout: "single", items: [table([["a", "1"]])] },
        { layout: "single", items: [table([["b", "2"]])] },
      ],
    };
    const keys = anchorsIn(node, annotating());
    expect(keys).toContain(anchorKey(partAnchor("scene", ["leaf", 1, "row", 0, "col", "Price"])));
  });

  it("anchors a diagram box by its id and an edge by its position", () => {
    const keys = anchorsIn(scene(diagram), annotating());
    expect(keys).toContain(anchorKey(partAnchor("scene", ["leaf", 0, "node", "opt"])));
    expect(keys).toContain(anchorKey(partAnchor("scene", ["leaf", 0, "edge", 0])));
  });

  it("anchors a diff line by its side and line number in the payload", () => {
    const diff: VisualLeaf = {
      kind: "code_diff",
      title: "",
      language: "python",
      before: "hours = range(24)\nfor h in hours:\n",
      after: "hours = delivery_hours(day, tz)\nfor h in hours:\n",
    };
    const keys = anchorsIn(scene(diff), annotating());
    expect(keys).toContain(
      anchorKey(partAnchor("scene", ["leaf", 0, "side", "before", "line", 0])),
    );
    expect(keys).toContain(anchorKey(partAnchor("scene", ["leaf", 0, "side", "after", "line", 0])));
  });

  it("costs nothing when there is no annotation to make", () => {
    // The render budget (`budget.test.tsx`) is 18,000 marks; a wrapper element
    // per mark would eat it. Outside annotate mode, an unnoted part is not an
    // element at all.
    const out = html(scene(table(rows), chart, diagram));
    expect(out).not.toContain("data-anchor");
    expect(out).not.toContain("wb-vis-part");
  });

  it("shows a note in place while the mode is off", () => {
    // A note you can only see by turning a mode back on is a note you will not
    // act on before deciding.
    const noted = anchorKey(partAnchor("scene", ["leaf", 0, "row", 1, "col", "Price"]));
    const annotation: VisualAnnotation = {
      nodeId: "scene",
      notes: { [noted]: "wrong fold" },
      editing: null,
      onPick: null,
    };
    expect(anchorsIn(scene(table(rows)), annotation)).toEqual([noted]);
    const out = renderToStaticMarkup(
      <VisualView node={scene(table(rows))} annotation={annotation} />,
    );
    expect(out).toContain("wrong fold");
    // Inert: no button, no tab stop, nothing to click by accident.
    expect(out).not.toContain("<button");
  });

  it("keeps markup in a cell as text with an anchor on it", () => {
    // The scene graph's threat model, re-run with annotations present: an
    // anchor adds a wrapper, and the wrapper must not become a way in.
    const out = renderToStaticMarkup(
      <VisualView node={scene(table([[MARKUP, "1"]]))} annotation={annotating()} />,
    );
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
    for (const tag of ["<img", "<iframe", "<a ", "<form"]) expect(out).not.toContain(tag);
    expect(out).not.toMatch(/\son[a-z]+=/);
    expect(out).not.toContain(" style=");
  });
});
