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

import type { ChartLeaf, DiagramLeaf, TableLeaf, VisualLeaf, VisualNode } from "../types";
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
