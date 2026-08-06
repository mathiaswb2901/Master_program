/**
 * The emitting half of an anchor: paths, keys and the labels they read as.
 *
 * `server/tests/test_plan_anchors.py` owns the receiving half — what a
 * malformed anchor does. This file owns the decisions only the client makes:
 * *when a name is used instead of a position*, what a note is keyed by, and
 * where a text range's offsets come from.
 */

import { describe, expect, it } from "vitest";

import type { PlanArtifact, VisualNode } from "../types";
import {
  anchorKey,
  anchorLabel,
  flatLeaves,
  leafIndex,
  nameOrIndex,
  nodeAnchor,
  partAnchor,
  PLAN_ANCHOR,
  rangeAnchor,
  sliceCodePoints,
  textSegments,
} from "./anchors";

const PROSE = "The 25-hour day needs a fold tag. Everything else is unchanged.";

const scene: VisualNode = {
  kind: "visual",
  node_id: "scene",
  title: "Day-ahead result",
  blocks: [
    {
      layout: "single",
      items: [
        {
          kind: "metrics",
          title: "Day totals",
          items: [
            { label: "Revenue", value: "18 420", unit: "EUR", role: "neutral" },
            { label: "Cycles", value: "1.4", unit: "", role: "neutral" },
          ],
        },
      ],
    },
    {
      layout: "split",
      items: [
        {
          kind: "table",
          title: "Before",
          columns: [
            { label: "Hour", type: "text", unit: "" },
            { label: "Price", type: "numeric", unit: "EUR/MWh" },
          ],
          rows: [["02:00 (1st)", "-3.8"]],
          highlights: [],
        },
        {
          kind: "table",
          title: "After",
          columns: [
            { label: "Hour", type: "text", unit: "" },
            { label: "Price", type: "numeric", unit: "EUR/MWh" },
          ],
          rows: [["02:00 (2nd)", "-6.1"]],
          highlights: [],
        },
      ],
    },
  ],
};

const plan: PlanArtifact = {
  plan_id: "p1",
  title: "Åsen 2",
  summary: "",
  nodes: [
    { kind: "markdown", node_id: "intro", text: PROSE },
    scene,
    { kind: "question", node_id: "q1", text: "Backfill 2025 too?" },
  ],
};

describe("naming a part", () => {
  it("uses the label when the label addresses exactly one thing", () => {
    expect(nameOrIndex(["Hour", "Price"], 1)).toBe("Price");
  });

  // Two columns called "Price" make "Price" an address for two things. The
  // server refuses to guess between them, so the client must not offer it one.
  it("falls back to the index when a label is ambiguous", () => {
    expect(nameOrIndex(["Price", "Price"], 1)).toBe(1);
  });

  it("falls back to the index when there is no label at all", () => {
    expect(nameOrIndex(["", "Price"], 0)).toBe(0);
  });
});

describe("leaf numbering", () => {
  // Blocks are how the scene is laid out; leaves are what it contains. The
  // anchor counts leaves, and this mirrors `_leaves` in plan_anchors.py.
  it("counts leaves flat over the blocks", () => {
    expect(flatLeaves(scene).map((leaf) => leaf.kind)).toEqual(["metrics", "table", "table"]);
    expect(leafIndex(scene, 0, 0)).toBe(0);
    expect(leafIndex(scene, 1, 0)).toBe(1);
    expect(leafIndex(scene, 1, 1)).toBe(2);
  });
});

describe("anchor identity", () => {
  it("is the same string for the same part, twice", () => {
    const path = ["leaf", 2, "row", 0, "col", "Price"];
    expect(anchorKey(partAnchor("scene", path))).toBe(anchorKey(partAnchor("scene", [...path])));
  });

  it("separates the kinds so a node and a part never collide", () => {
    expect(anchorKey(nodeAnchor("scene"))).not.toBe(anchorKey(partAnchor("scene", ["leaf", 0])));
    expect(anchorKey(PLAN_ANCHOR)).not.toBe(anchorKey(nodeAnchor("")));
  });

  // A label comes from the payload, so any separator a label could contain is
  // a separator a label could forge. JSON escapes its own delimiters.
  it("cannot be forged by a label that looks like more segments", () => {
    const forged = anchorKey(partAnchor("scene", ["leaf", 0, "col", '","row","0']));
    const real = anchorKey(partAnchor("scene", ["leaf", 0, "col", "row", 0]));
    expect(forged).not.toBe(real);
  });

  it("keeps a number a number, so `1` and `\"1\"` are different addresses", () => {
    expect(anchorKey(partAnchor("scene", ["leaf", 0, "col", 1]))).not.toBe(
      anchorKey(partAnchor("scene", ["leaf", 0, "col", "1"])),
    );
  });
});

describe("what a note says it is about", () => {
  // 1-based, and a name spoken as itself — the same two rules the renderer's
  // own accessible names follow, so the chip on a note and the button it came
  // from say the same thing.
  it("names the leaf and the part, in words", () => {
    const label = anchorLabel(partAnchor("scene", ["leaf", 2, "row", 0, "col", "Price"]), plan);
    expect(label).toBe("After, row 1, Price");
  });

  it("counts positions from one, because nobody reads a table from zero", () => {
    expect(anchorLabel(partAnchor("scene", ["leaf", 2, "row", 0, "col", 1]), plan)).toBe(
      "After, row 1, column 2",
    );
  });

  it("names the node for a whole-node anchor", () => {
    expect(anchorLabel(nodeAnchor("scene"), plan)).toBe("Day-ahead result");
  });

  it("quotes the phrase for a range", () => {
    expect(anchorLabel(rangeAnchor("intro", 4, 16), plan)).toBe("“25-hour day ”");
  });

  it("says so for the plan as a whole", () => {
    expect(anchorLabel(PLAN_ANCHOR, plan)).toBe("The whole plan");
  });

  // A card can be re-presented and a note can outlive the node it named; the
  // label degrades to the id rather than throwing inside a chat column.
  it("degrades to the node id when the node is gone", () => {
    expect(anchorLabel(nodeAnchor("ghost"), plan)).toBe("ghost");
  });
});

describe("text ranges", () => {
  /**
   * The offsets are into the **source string**, which is the whole reason this
   * function exists rather than a DOM selection: markdown renders to text that
   * is not the source, so a selection offset would slice the wrong characters
   * out of the string the agent is holding.
   */
  it("slices the source, exactly", () => {
    for (const segment of textSegments(PROSE)) {
      expect(sliceCodePoints(PROSE, segment.start, segment.end)).toBe(segment.text);
    }
  });

  it("counts code points, so an emoji cannot shift the range", () => {
    // The offsets are read by Python, which indexes code points; JavaScript
    // indexes UTF-16, where "🔋" is two units rather than one. Counting units
    // would put the second sentence at 20–37 — in range, so accepted, and one
    // character off for every astral character before it. The Python half of
    // this same string is asserted in `server/tests/test_plan_anchors.py`.
    const astral = "Battery 🔋 is full. Charge it anyway.";
    const [first, second] = textSegments(astral);
    expect(second.text).toBe("Charge it anyway.");
    expect(second.start).toBe([...astral].indexOf("C"));
    // Python's `len()`, not JavaScript's `.length` — they differ here by one.
    expect(second.end).toBe([...astral].length);
    expect(second.end).toBeLessThan(astral.length);
    for (const segment of [first, second]) {
      expect(sliceCodePoints(astral, segment.start, segment.end)).toBe(segment.text);
    }
  });

  it("labels a range by the characters the server would slice", () => {
    const astral = "Battery 🔋 is full. Charge it anyway.";
    const astralPlan: PlanArtifact = {
      ...plan,
      nodes: [{ kind: "markdown", node_id: "note", text: astral }],
    };
    const segment = textSegments(astral)[1];
    const label = anchorLabel(rangeAnchor("note", segment.start, segment.end), astralPlan);
    expect(label).toBe("“Charge it anyway.”");
  });

  it("splits prose into sentences", () => {
    expect(textSegments(PROSE).map((s) => s.text)).toEqual([
      "The 25-hour day needs a fold tag.",
      "Everything else is unchanged.",
    ]);
  });

  it("treats a line as a phrase, so a list item is pickable", () => {
    expect(textSegments("- one\n- two").map((s) => s.text)).toEqual(["- one", "- two"]);
  });

  it("produces nothing for whitespace", () => {
    expect(textSegments("   \n  ")).toEqual([]);
  });

  it("covers a string with no sentence end at all", () => {
    const segments = textSegments("no full stop here");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toEqual({ start: 0, end: 17, text: "no full stop here" });
  });
});
