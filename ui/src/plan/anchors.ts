/**
 * Anchors: what the renderer emits when the user points at part of an artifact.
 *
 * **An anchor is a semantic path, never a CSS selector.** The obvious way to
 * record a click is the element it landed on — an id, a selector, a rectangle.
 * All three are wrong. A selector is our stylesheet leaking into the agent's
 * input: it breaks when a class is renamed, it means nothing to a model reading
 * it, and it addresses the *page* rather than the payload. A path like
 * `["leaf", 2, "row", 14, "col", "Price"]` names **data** — it survives a
 * re-render, survives a restyle, is directly actionable by the agent (it is the
 * agent's own payload in the agent's own vocabulary), and is validated
 * server-side against the artifact before it travels
 * (`services/plan_anchors.py`), so it cannot name something the artifact does
 * not contain.
 *
 * Positional where the payload is ordered (leaves, rows, points, edges), named
 * where the payload carries a name (a column label, a series label, a diagram
 * node id) — {@link nameOrIndex} is the one place that choice is made, and it
 * falls back to the index whenever a label is missing or ambiguous, because an
 * address that resolves to two things is not an address.
 *
 * Pure: no React, no store, no DOM. Every function here is unit-tested
 * (`anchors.test.ts`), and `visual/Visual.test.tsx` asserts the paths the
 * renderer stamps are the same before and after a state change.
 */

import type {
  AnchorSegment,
  AnnotationAnchor,
  PlanArtifact,
  PlanNode,
  VisualLeaf,
  VisualNode,
} from "../types";
import { gridLabels, resolveGrid } from "../visual/timeAxis";

/** Mirrors `MAX_ANNOTATIONS` in `server/src/workbench_server/models/plans.py`. */
export const MAX_ANNOTATIONS = 40;

// ---- construction -----------------------------------------------------------

/** The whole card. What the user marks when the objection is the plan itself. */
export const PLAN_ANCHOR: AnnotationAnchor = { kind: "plan", node_id: "", path: [] };

/** A whole node — what every annotation was before parts existed, and still
 * the fallback: a question's answer and a node-level note are both this. */
export function nodeAnchor(nodeId: string): AnnotationAnchor {
  return { kind: "node", node_id: nodeId, path: [] };
}

/** A part of a drawn leaf. `path` always starts `["leaf", n]`. */
export function partAnchor(nodeId: string, path: AnchorSegment[]): AnnotationAnchor {
  return { kind: "part", node_id: nodeId, path };
}

/** A half-open range of characters in a `markdown` or `question` node's text. */
export function rangeAnchor(nodeId: string, start: number, end: number): AnnotationAnchor {
  return { kind: "range", node_id: nodeId, path: ["from", start, "to", end] };
}

/**
 * A label if it addresses exactly one entry, else the index.
 *
 * The whole semantic/positional trade in one function. `"Price"` is worth more
 * than `1` — it survives a column being inserted before it and it is what the
 * user actually pointed at — but only while it is unique and non-empty. Two
 * columns called "Price" make "Price" an ambiguous address (the server rejects
 * one), and an empty label is no address at all, so both fall back to the
 * position, which is always exact.
 */
export function nameOrIndex(labels: readonly string[], index: number): AnchorSegment {
  const label = labels[index] ?? "";
  if (label === "") return index;
  return labels.filter((other) => other === label).length === 1 ? label : index;
}

// ---- leaves -----------------------------------------------------------------

/** A visual node's leaves flat over its blocks — the order a `leaf` segment
 * counts in, and the same order `services/plan_anchors.py` counts. Blocks are
 * how the scene is laid out; the leaves are what it contains. */
export function flatLeaves(node: VisualNode): VisualLeaf[] {
  return node.blocks.flatMap((block) => block.items);
}

/** Where `node.blocks[block].items[item]` lands in {@link flatLeaves}. */
export function leafIndex(node: VisualNode, block: number, item: number): number {
  let seen = 0;
  for (let b = 0; b < block; b++) seen += node.blocks[b].items.length;
  return seen + item;
}

// ---- identity ---------------------------------------------------------------

/**
 * A stable string identity for one anchor — the key a draft note is stored
 * under, and the value stamped into `data-anchor` so the DOM (and a test) can
 * say which datum an element draws.
 *
 * JSON rather than a joined string, for one reason worth stating: a label comes
 * from the payload, so any separator a label could contain is a separator a
 * label could *forge* — a column called `row0` would otherwise produce the key
 * of a different part. JSON escapes its own delimiters, so two different
 * anchors can never serialize the same, and unlike a control byte it stays
 * readable in the inspector and writable in a selector.
 *
 * Derived from the anchor and nothing else: two renders of the same part
 * produce the same key, which is what makes "edit the note already here" work
 * across a re-render.
 */
export function anchorKey(anchor: AnnotationAnchor): string {
  return JSON.stringify([anchor.kind, anchor.node_id, ...anchor.path]);
}

// ---- human labels -----------------------------------------------------------

/** `["leaf", 2, "row", 14]` -> `[["leaf", 2], ["row", 14]]`. */
function pairs(path: AnchorSegment[]): [string, AnchorSegment][] {
  const out: [string, AnchorSegment][] = [];
  for (let i = 0; i + 1 < path.length; i += 2) out.push([String(path[i]), path[i + 1]]);
  return out;
}

/** The noun for a positional segment. A named one reads as its own name. */
const SEGMENT_WORD: Record<string, string> = {
  row: "row",
  col: "column",
  series: "series",
  point: "point",
  node: "box",
  edge: "arrow",
  line: "line",
  metric: "metric",
};

/**
 * How one segment reads to a person.
 *
 * Two rules, and they are the ones the renderer's own accessible names follow
 * (`visual/Visual.tsx`), so the chip on a note and the button it came from say
 * the same thing: a **name** is spoken as itself (`Price`, not `column
 * "Price"`), and a **position** is spoken **1-based** (`row 14` is the
 * fifteenth row, and nobody reading a table counts from zero). The path stays
 * 0-based on the wire, where it is an index into a list.
 */
function segmentWords(key: string, value: AnchorSegment): string {
  if (key === "side") return String(value);
  if (typeof value === "string") return value;
  const word = SEGMENT_WORD[key] ?? key;
  return `${word} ${String(value + 1)}`;
}

/**
 * What this anchor points at, in words — the note's target as the user reads
 * it in the notes list, and the accessible name of the thing they clicked.
 *
 * Built from the artifact rather than stored on the note: a label is
 * presentation, and storing one would be a second copy of the payload to keep
 * honest with the payload.
 */
export function anchorLabel(anchor: AnnotationAnchor, plan: PlanArtifact): string {
  if (anchor.kind === "plan") return "The whole plan";
  const node = plan.nodes.find((candidate) => candidate.node_id === anchor.node_id);
  const nodeName = node === undefined ? anchor.node_id : nodeLabel(node);
  if (anchor.kind === "node") return nodeName;
  if (anchor.kind === "range") {
    const at = new Map(pairs(anchor.path));
    const from = Number(at.get("from") ?? 0);
    const to = Number(at.get("to") ?? 0);
    // Code points, like the offsets themselves — a `.slice` here would name
    // different characters than the server and the agent do.
    const text = node !== undefined && "text" in node ? sliceCodePoints(node.text, from, to) : "";
    return text === "" ? `${nodeName} — characters ${from}–${to}` : `“${text}”`;
  }
  const leaf = anchoredLeaf(anchor, plan);
  const parts = pairs(anchor.path)
    .filter(([key]) => key !== "leaf")
    .map(([key, value]) => resolvedWords(key, value, leaf));
  return [leafName(anchor, plan), ...parts].filter((piece) => piece !== "").join(", ");
}

/** The leaf an anchor points into, if it points into one at all. */
function anchoredLeaf(anchor: AnnotationAnchor, plan: PlanArtifact): VisualLeaf | undefined {
  const node = plan.nodes.find((candidate) => candidate.node_id === anchor.node_id);
  if (node === undefined || node.kind !== "visual") return undefined;
  const index = Number(new Map(pairs(anchor.path)).get("leaf") ?? -1);
  return flatLeaves(node).at(index === -1 ? Number.NaN : index);
}

/** The title of the leaf an anchor points into, or a positional fallback. */
function leafName(anchor: AnnotationAnchor, plan: PlanArtifact): string {
  const leaf = anchoredLeaf(anchor, plan);
  if (leaf === undefined) return anchor.node_id;
  const index = Number(new Map(pairs(anchor.path)).get("leaf") ?? 0);
  return leaf.title === "" ? `${leaf.kind} ${String(index + 1)}` : leaf.title;
}

/**
 * {@link segmentWords}, but allowed to look the value up in the leaf.
 *
 * Two positions are worth resolving because the renderer's own accessible name
 * resolves them, and a chip that disagrees with the button it came from is a
 * chip you distrust: a chart point reads as the *time* the axis draws it at,
 * and a diagram box as its label rather than the id an edge refers to it by.
 */
function resolvedWords(key: string, value: AnchorSegment, leaf: VisualLeaf | undefined): string {
  if (leaf?.kind === "chart" && key === "point" && typeof value === "number") {
    const longest = Math.max(...leaf.series.map((series) => series.values.length));
    if (leaf.x.kind === "time") return gridLabels(resolveGrid(leaf.x, longest))[value] ?? "";
  }
  if (leaf?.kind === "diagram" && key === "node") {
    const box = leaf.nodes.find((candidate) => candidate.id === value);
    if (box !== undefined) return box.label;
  }
  return segmentWords(key, value);
}

/** A plan node's own name, for the notes list. Kept short — this is a chip. */
function nodeLabel(node: PlanNode): string {
  switch (node.kind) {
    case "markdown":
      return firstWords(node.text);
    case "question":
      return firstWords(node.text);
    case "option_group":
      return node.prompt;
    case "step_list":
      return `${String(node.steps.length)} steps`;
    case "visual":
      return node.title === "" ? "Visual" : node.title;
  }
}

function firstWords(text: string, limit = 42): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}…`;
}

// ---- text ranges ------------------------------------------------------------

/**
 * A range offset counts **Unicode code points**, not JavaScript string units.
 *
 * The offsets in a range anchor are read by a Python process: the server checks
 * them against `len(text)` (`services/plan_anchors.py`) and the agent slices
 * `text[from:to]` out of the string it is holding. Python indexes code points;
 * JavaScript indexes UTF-16, where a character outside the BMP — `"🔋"` — is
 * two units rather than one. Counting units would shift the phrase the server
 * names by one for every such character before it, and shift it *silently*:
 * both ends stay in range, so the anchor validates and simply points at the
 * wrong words. That is the exact failure this whole design exists to prevent,
 * so the client counts the way the reader of the number counts.
 */
function codePointOffsets(text: string): number[] {
  // One entry per string unit, plus the end: `offsets[i]` is the code-point
  // offset of unit `i`. Both halves of a surrogate pair map to the same code
  // point, so an offset landing mid-pair still names the whole character.
  const offsets: number[] = [];
  let point = 0;
  for (let unit = 0; unit < text.length; ) {
    const size = (text.codePointAt(unit) ?? 0) > 0xffff ? 2 : 1;
    for (let half = 0; half < size; half++) offsets.push(point);
    unit += size;
    point += 1;
  }
  offsets.push(point);
  return offsets;
}

/** `String.prototype.slice`, in the units a range anchor is written in. */
export function sliceCodePoints(text: string, start: number, end: number): string {
  return [...text].slice(start, end).join("");
}

/**
 * A text node split into the phrases a user can point at, as offsets into the
 * **source string** — which is what makes a range anchor exact.
 *
 * Deliberately not derived from the rendered DOM. Markdown renders to elements
 * whose text is not the source (a `**bold**` run is four characters shorter on
 * screen), so a selection offset taken from the page would address the wrong
 * characters of the string the agent actually holds. Splitting the source and
 * letting the user pick a sentence gives an offset pair that is right by
 * construction, and it is keyboard-operable, which a drag-selection is not.
 */
export function textSegments(text: string): { start: number; end: number; text: string }[] {
  const out: { start: number; end: number; text: string }[] = [];
  // The regex reports UTF-16 positions; the anchor is written in code points.
  const points = codePointOffsets(text);
  // Sentence ends, or a line break — a markdown list item is a phrase too.
  const pattern = /[^\n]*?(?:[.!?](?=\s|$)|\n|$)/g;
  for (const match of text.matchAll(pattern)) {
    const start = match.index;
    const end = start + match[0].length;
    if (match[0].trim() === "") continue;
    // Trim the whitespace off both ends so the offsets bracket the words, not
    // the gaps between them.
    const leading = match[0].length - match[0].trimStart().length;
    const trailing = match[0].length - match[0].trimEnd().length;
    out.push({
      start: points[start + leading],
      end: points[end - trailing],
      text: match[0].trim(),
    });
    if (end >= text.length) break;
  }
  return out;
}
