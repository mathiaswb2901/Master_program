/**
 * The geometry the model never sends: axis ticks, DAG layering, line matching.
 *
 * Pinned here because these are the decisions that make a picture true or a lie
 * — a bar chart whose axis hides zero, a diagram whose arrow points backwards,
 * a diff that shows two changed lines as four.
 */

import { describe, expect, it } from "vitest";

import type { DiagramEdge, DiagramNode } from "../types";
import {
  formatTick,
  layerNodes,
  logTicks,
  matchLines,
  minimumGap,
  niceTicks,
  scale,
} from "./layout";

const node = (id: string): DiagramNode => ({ id, label: id.toUpperCase(), role: "neutral" });
const edge = (source: string, target: string): DiagramEdge => ({ source, target, label: "" });

describe("value ticks", () => {
  it("rounds outward so the domain is a whole number of steps", () => {
    const { ticks, domain } = niceTicks(-6.1, 88.4);
    expect(domain[0]).toBeLessThanOrEqual(-6.1);
    expect(domain[1]).toBeGreaterThanOrEqual(88.4);
    expect(ticks[0].value).toBe(domain[0]);
    expect(ticks.at(-1)?.value).toBe(domain[1]);
  });

  it("picks 1-2-5 steps a person would pick", () => {
    expect(niceTicks(0, 100).ticks.map((t) => t.value)).toEqual([0, 20, 40, 60, 80, 100]);
    expect(niceTicks(0, 9).ticks.map((t) => t.value)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(niceTicks(0, 1).ticks.map((t) => t.label)).toEqual([
      "0.0",
      "0.2",
      "0.4",
      "0.6",
      "0.8",
      "1.0",
    ]);
  });

  it("does not print float drift as an axis label", () => {
    // 0.1 accumulated twenty times is 2.0000000000000004; multiplying is not.
    for (const tick of niceTicks(0, 2).ticks) expect(tick.label).not.toMatch(/0000/);
  });

  it("gives a flat series an axis instead of dividing by zero", () => {
    const { ticks, domain } = niceTicks(5, 5);
    expect(domain[0]).toBeLessThan(domain[1]);
    expect(ticks.length).toBeGreaterThan(1);
  });

  it("prints -0 as 0", () => {
    expect(formatTick(-0.0001, 1)).toBe("0");
  });

  it("uses decades on a log axis, where 1-2-5 would be unreadable", () => {
    const { ticks, domain } = logTicks(3, 4000);
    expect(ticks.map((t) => t.value)).toEqual([1, 10, 100, 1000, 10000]);
    expect(domain).toEqual([1, 10000]);
  });
});

describe("scales", () => {
  it("maps a domain onto a pixel range", () => {
    const to = scale([0, 100], [200, 0]);
    expect(to(0)).toBe(200);
    expect(to(100)).toBe(0);
    expect(to(50)).toBe(100);
  });

  it("maps the logarithm on a log scale, not the value", () => {
    const to = scale([1, 1000], [0, 300], "log");
    expect(to(10)).toBeCloseTo(100);
    expect(to(100)).toBeCloseTo(200);
  });

  it("survives a degenerate domain", () => {
    expect(Number.isFinite(scale([5, 5], [0, 100])(5))).toBe(true);
  });

  it("takes the smallest gap as a bar's width on a value axis", () => {
    expect(minimumGap([0, 10, 15, 40])).toBe(5);
    expect(minimumGap([7])).toBe(1); // one point has no gap to measure
  });
});

describe("DAG layering", () => {
  it("puts a node one layer below its deepest predecessor", () => {
    // a → b → d and a → d: d must be below b, not beside it, or the a→d edge
    // would be drawn sideways through the layer it skips.
    const placed = layerNodes(
      [node("a"), node("b"), node("d")],
      [edge("a", "b"), edge("b", "d"), edge("a", "d")],
    );
    const layer = new Map(placed.map((p) => [p.id, p.layer]));
    expect(layer.get("a")).toBe(0);
    expect(layer.get("b")).toBe(1);
    expect(layer.get("d")).toBe(2);
  });

  it("spreads a layer and reports its size, so the renderer needs no canvas", () => {
    const placed = layerNodes([node("a"), node("b"), node("c")], [edge("a", "b"), edge("a", "c")]);
    const second = placed.filter((p) => p.layer === 1);
    expect(second.map((p) => p.index)).toEqual([0, 1]);
    expect(second.every((p) => p.layerSize === 2)).toBe(true);
  });

  it("keeps every edge pointing downward", () => {
    const nodes = ["feed", "curve", "opt", "bid", "gate"].map(node);
    const edges = [
      edge("feed", "curve"),
      edge("curve", "opt"),
      edge("opt", "bid"),
      edge("bid", "gate"),
      edge("feed", "opt"),
    ];
    const layer = new Map(layerNodes(nodes, edges).map((p) => [p.id, p.layer]));
    for (const e of edges) {
      expect(layer.get(e.source) ?? 0).toBeLessThan(layer.get(e.target) ?? 0);
    }
  });

  it("lays out a forest with no edges as one row", () => {
    const placed = layerNodes([node("a"), node("b")], []);
    expect(placed.every((p) => p.layer === 0)).toBe(true);
    expect(placed.map((p) => p.index)).toEqual([0, 1]);
  });

  it("terminates on a cycle the server would have rejected", () => {
    // Defence in depth, not a supported payload: acyclicity is validated in
    // models/visuals.py. What must never happen is a hung tab.
    const placed = layerNodes([node("a"), node("b")], [edge("a", "b"), edge("b", "a")]);
    expect(placed).toHaveLength(2);
  });
});

describe("line matching", () => {
  it("marks only the line that changed", () => {
    const diff = matchLines("a\nb\nc\n", "a\nB\nc\n");
    expect(diff.map((line) => line.kind)).toEqual(["same", "del", "add", "same"]);
    expect(diff[1].text).toBe("b");
    expect(diff[2].text).toBe("B");
  });

  it("numbers each side independently", () => {
    const diff = matchLines("a\nb\n", "a\nx\nb\n");
    const added = diff.find((line) => line.kind === "add");
    expect(added?.beforeNo).toBeNull();
    expect(added?.afterNo).toBe(2);
    expect(diff.at(-1)).toMatchObject({ kind: "same", beforeNo: 2, afterNo: 3 });
  });

  it("treats a trailing newline as a line ending, not an empty line", () => {
    expect(matchLines("a\n", "a\n")).toHaveLength(1);
    expect(matchLines("", "new\n").map((line) => line.kind)).toEqual(["add"]);
  });

  it("reads a pure addition as additions", () => {
    expect(matchLines("", "a\nb\n").map((line) => line.kind)).toEqual(["add", "add"]);
    expect(matchLines("a\nb\n", "").map((line) => line.kind)).toEqual(["del", "del"]);
  });

  it("stays cheap at the server's cap", () => {
    const before = Array.from({ length: 120 }, (_, i) => `line ${String(i)}`).join("\n");
    const after = before.replace("line 60", "line sixty");
    const started = Date.now();
    const diff = matchLines(before, after);
    expect(diff.filter((line) => line.kind !== "same")).toHaveLength(2);
    expect(Date.now() - started).toBeLessThan(500);
  });
});
