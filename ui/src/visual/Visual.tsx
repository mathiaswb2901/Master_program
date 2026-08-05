/**
 * The scene-graph renderer: every pixel of a `visual` plan node.
 *
 * Nothing here interprets a string. Every value from the payload becomes a
 * React *text node* or a number we computed a coordinate from — there is no
 * `dangerouslySetInnerHTML`, no `<img>`, no `<a href>`, no `<iframe>`, no
 * `style` attribute built from payload text, and no fetch. That is the whole
 * reason Workbench draws its own artifacts instead of hosting a model's markup:
 * a table cell reading `<script>alert(1)</script>` shows those characters
 * (asserted in `Visual.test.tsx` and in the E2E journey), and a card that
 * renders issues no network request at all.
 *
 * Geometry lives in `layout.ts` and `timeAxis.ts` — pure, and unit-tested there
 * rather than eyeballed here. This file is arrangement and tokens: DESIGN.md
 * §6.3 for the 760px column, principle 7 for tabular figures, §7 for "colour is
 * never the sole signal" (every role pairs with a sign, a label or a border).
 *
 * No charting dependency. The entry chunk is already 4 MB and 88% Monaco; the
 * five leaf kinds are a few hundred lines of SVG, which is less than any
 * library's *lazy* chunk would cost, and it is the only way the palette really
 * comes from `tokens.css` in both themes.
 */

import { useEffect, useId, useRef, useState, type RefObject } from "react";

import { nameOrIndex } from "../plan/anchors";
import type {
  ChartLeaf,
  ChartSeries,
  CodeDiffLeaf,
  DiagramLeaf,
  MetricsLeaf,
  TableLeaf,
  TimeAxis,
  ValueAxis,
  VisualBlock,
  VisualLeaf,
  VisualNode,
  VisualRole,
} from "../types";
import {
  AnnotateProvider,
  Part,
  SvgPart,
  useAnnotation,
  type VisualAnnotation,
} from "./annotate";
import { layerNodes, logTicks, matchLines, minimumGap, niceTicks, scale } from "./layout";
import { gridCaption, gridLabels, resolveGrid, timeTicks } from "./timeAxis";

/**
 * Where a leaf sits in its node, flat over the blocks — the first segment of
 * every anchor a leaf emits (`../plan/anchors.ts`). Passed down rather than
 * derived inside each view so there is exactly one place the numbering is
 * decided, and it is the same place `services/plan_anchors.py` counts from.
 */
type LeafAt = { at: number };

/** Whether parts are pickable right now — annotate mode, in one word. */
function usePickable(): boolean {
  return useAnnotation()?.onPick != null;
}

/** How a part names itself out loud: `Before, row 2, column Price`. */
function partLabel(title: string, kind: string, ...rest: string[]): string {
  return [title === "" ? kind : title, ...rest].join(", ");
}

/** Role → the class that carries its token pair. Never a colour literal. */
function roleClass(role: VisualRole): string {
  return role === "neutral" ? "" : ` is-${role}`;
}

/**
 * Screen-reader word for a role, so colour is never the only signal (§7).
 *
 * Every surface that carries a role carries this word too — a table cell and a
 * metric in a `u-sr-only` span, a diagram node inside the drawing's accessible
 * name (an `svg` with `role="img"` has *one* accessible name, so a hidden span
 * in the markup would never be announced). The visible half of the pair is a
 * heavier edge than the same element wears with no role: `visual.css` moves a
 * table cell from no inset to 2px, a diagram box from a 1px stroke to 2px, and
 * a metric's rule from 2px to 4px.
 */
const ROLE_WORD: Record<VisualRole, string> = {
  neutral: "",
  accent: "Highlighted",
  success: "Good",
  warning: "Warning",
  error: "Problem",
};

/** A node's label as the drawing's accessible name says it: `Bid file (Good)`. */
function roleLabel(label: string, role: VisualRole): string {
  return role === "neutral" ? label : `${label} (${ROLE_WORD[role]})`;
}

function LeafTitle({ text }: { text: string }) {
  return text === "" ? null : <h4 className="wb-vis-title">{text}</h4>;
}

/**
 * The drawing surface's real width in CSS pixels.
 *
 * Drawn at true size rather than scaled from a fixed viewBox: this card lives
 * in a dock panel whose width has nothing to do with the viewport's, and a
 * 700-unit chart squeezed into a 340px panel renders its 11px labels at 5px.
 * Measuring means the type is always the size DESIGN.md says it is, and the
 * axis simply gets fewer ticks when there is less room.
 *
 * The fallback is what server rendering and the unit tests see, so the
 * component is still a pure function of its props there.
 */
function useDrawWidth(fallback: number, floor: number): [RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const host = ref.current;
    if (host === null) return;
    const observer = new ResizeObserver((entries) => {
      const measured = entries[0]?.contentRect.width ?? 0;
      if (measured > 0) setWidth(Math.max(floor, Math.round(measured)));
    });
    observer.observe(host);
    return () => {
      observer.disconnect();
    };
  }, [floor]);
  return [ref, width];
}

// ---- table -------------------------------------------------------------------

function TableView({ leaf, at }: { leaf: TableLeaf } & LeafAt) {
  // Row-level highlights first so a cell-level one can still win on top of it.
  const rowRole = new Map<number, VisualRole>();
  const cellRole = new Map<string, VisualRole>();
  for (const highlight of leaf.highlights) {
    if (highlight.column === null) rowRole.set(highlight.row, highlight.role);
    else cellRole.set(`${String(highlight.row)}:${String(highlight.column)}`, highlight.role);
  }
  // A column is addressed by its label when that label addresses exactly one
  // column, else by its index — `nameOrIndex` is the whole of that decision.
  const labels = leaf.columns.map((column) => column.label);
  const pickable = usePickable();
  return (
    <figure className="wb-vis-leaf">
      <LeafTitle text={leaf.title} />
      <div className="wb-vis-scroll">
        <table className="wb-vis-table">
          <thead>
            <tr>
              {pickable && (
                <th scope="col" className="wb-vis-pick-col">
                  <span className="u-sr-only">Annotate row</span>
                </th>
              )}
              {leaf.columns.map((column, c) => (
                <th key={c} scope="col" className={`is-${column.type}`}>
                  <span className="wb-vis-th-label">{column.label}</span>
                  {column.unit !== "" && <span className="wb-vis-unit">{column.unit}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {leaf.rows.map((row, r) => (
              <tr key={r} className={roleClass(rowRole.get(r) ?? "neutral").trim() || undefined}>
                {pickable && (
                  <td className="wb-vis-pick-col">
                    <Part
                      path={["leaf", at, "row", r]}
                      label={partLabel(leaf.title, "table", `row ${String(r + 1)}`)}
                    >
                      <span aria-hidden="true">¶</span>
                    </Part>
                  </td>
                )}
                {row.map((cell, c) => {
                  const role = cellRole.get(`${String(r)}:${String(c)}`) ?? "neutral";
                  return (
                    <td key={c} className={`is-${leaf.columns[c].type}${roleClass(role)}`}>
                      {role !== "neutral" && <span className="u-sr-only">{ROLE_WORD[role]}: </span>}
                      <Part
                        path={["leaf", at, "row", r, "col", nameOrIndex(labels, c)]}
                        label={partLabel(
                          leaf.title,
                          "table",
                          `row ${String(r + 1)}`,
                          labels[c] === "" ? `column ${String(c + 1)}` : labels[c],
                        )}
                      >
                        {cell}
                      </Part>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}

// ---- chart -------------------------------------------------------------------

/** Width used before the container has been measured (SSR, unit tests) and the
 * narrowest we will draw at — below that the chart scrolls inside its own box
 * rather than becoming unreadable. */
const CHART_W = 700;
const CHART_FLOOR = 360;
const CHART_H = 236;
const PAD = { top: 14, bottom: 34, left: 54, right: 14 } as const;
const RIGHT_AXIS_W = 44;
/** One tick per ~90px: enough room for `00:00` plus air, at any width. */
const TICK_SPACING = 90;
/** Series colours come from the harmonized ANSI palette (DESIGN.md §2.7) —
 * the one categorical ramp the design system defines, already contrast-paired
 * in both themes, and the same one the terminal and Monaco read from. */
const SERIES_COUNT = 6;

interface Resolved {
  series: ChartSeries;
  xs: number[];
  index: number;
}

function axisOf(leaf: ChartLeaf, side: "left" | "right"): ValueAxis | null {
  return side === "left" ? leaf.y : leaf.y_right;
}

function extent(values: number[]): [number, number] {
  return [Math.min(...values), Math.max(...values)];
}

/**
 * Picking a *point* on a chart, without 2,400 tab stops.
 *
 * A chart may legally hold six series of four hundred points; making every one
 * a focusable target would be both a DOM the budget cannot afford and a
 * keyboard trap. So the picker is two steps: choose a series (six buttons at
 * most, in the legend's own order), then choose a point from a strip labelled
 * with the axis's real x — on a time grid that is the market's own clock, which
 * is the label the user is actually reading the chart by. Both steps are plain
 * buttons, so both work from the keyboard with nothing added.
 */
function PointStrip({
  leaf,
  at,
  labels,
}: { leaf: ChartLeaf; labels: string[] } & LeafAt) {
  const [open, setOpen] = useState<number | null>(null);
  const longest = Math.max(...leaf.series.map((s) => s.values.length));
  const grid = leaf.x.kind === "time" ? resolveGrid(leaf.x, longest) : null;
  const ticks = grid === null ? null : gridLabels(grid);
  const pointLabel = (index: number): string => ticks?.[index] ?? `#${String(index + 1)}`;
  const series = open === null ? null : leaf.series[open];
  return (
    <div className="wb-vis-points">
      <div className="wb-vis-points-pick">
        <span className="u-label">Points</span>
        {leaf.series.map((candidate, index) => (
          <button
            key={index}
            type="button"
            className={"wb-btn wb-btn-ghost wb-btn-sm" + (open === index ? " is-active" : "")}
            aria-expanded={open === index}
            onClick={() => {
              setOpen(open === index ? null : index);
            }}
          >
            {candidate.label}
          </button>
        ))}
      </div>
      {series !== null && open !== null && (
        <div className="wb-vis-points-list" role="group" aria-label={`Points of ${series.label}`}>
          {series.values.map((value, index) => (
            <Part
              key={index}
              path={["leaf", at, "series", nameOrIndex(labels, open), "point", index]}
              label={partLabel(
                leaf.title,
                "chart",
                series.label,
                `${pointLabel(index)}, ${String(value)}`,
              )}
            >
              <span className="u-tabular">{pointLabel(index)}</span>
            </Part>
          ))}
        </div>
      )}
    </div>
  );
}

function ChartView({ leaf, at }: { leaf: ChartLeaf } & LeafAt) {
  const clipId = useId();
  const pickable = usePickable();
  const seriesLabels = leaf.series.map((series) => series.label);
  const [host, width] = useDrawWidth(CHART_W, CHART_FLOOR);
  const isTime = leaf.x.kind === "time";
  const longest = Math.max(...leaf.series.map((s) => s.values.length));
  const grid = isTime ? resolveGrid(leaf.x as TimeAxis, longest) : null;

  // x for every point of every series, in domain units. On a time grid that is
  // milliseconds *from the start instant* — absolute, so a 23- or 25-hour day
  // is as wide as it really is (timeAxis.ts).
  const resolved: Resolved[] = leaf.series.map((series, index) => ({
    series,
    index,
    xs:
      grid !== null
        ? series.values.map((_, i) => i * grid.stepMs)
        : series.x.length > 0
          ? series.x
          : series.values.map((_, i) => i),
  }));
  const allX = resolved.flatMap((r) => r.xs);
  const stepWidth = grid !== null ? grid.stepMs : minimumGap(allX);
  // Bars and steps occupy an interval, so the domain runs to the end of the
  // last one; a line's marker sits at that interval's centre.
  const xDomain: [number, number] =
    grid !== null ? [0, longest * grid.stepMs] : [Math.min(...allX), Math.max(...allX) + stepWidth];

  const hasRight = leaf.y_right !== null && leaf.series.some((s) => s.axis === "right");
  const right = hasRight ? RIGHT_AXIS_W : PAD.right;
  const plot = {
    x0: PAD.left,
    x1: width - right,
    y0: PAD.top,
    y1: CHART_H - PAD.bottom,
  };
  const maxTicks = Math.max(2, Math.floor((plot.x1 - plot.x0) / TICK_SPACING));

  const axisTicks = (side: "left" | "right") => {
    const axis = axisOf(leaf, side);
    const values = resolved.filter((r) => r.series.axis === side).flatMap((r) => r.series.values);
    if (axis === null || values.length === 0) return null;
    const [lo, hi] = extent(values);
    // A bar reads as a distance from zero; hiding zero makes a 2% change look
    // like a doubling. Lines and steps keep their own range.
    const bars = resolved.some((r) => r.series.axis === side && r.series.style === "bar");
    const spec =
      axis.scale === "log"
        ? logTicks(lo, hi)
        : niceTicks(bars ? Math.min(0, lo) : lo, bars ? Math.max(0, hi) : hi);
    return {
      axis,
      ...spec,
      to: scale(spec.domain, [plot.y1, plot.y0], axis.scale),
    };
  };

  const left = axisTicks("left");
  const rightAxis = hasRight ? axisTicks("right") : null;
  const toX = scale(xDomain, [plot.x0, plot.x1]);
  const xTicks =
    grid !== null
      ? timeTicks(grid, maxTicks).map((tick) => ({
          value: tick.offsetMs,
          label: tick.label,
        }))
      : niceTicks(xDomain[0], xDomain[1], maxTicks).ticks;

  const bandFor = (r: Resolved, i: number): [number, number] => {
    const start = toX(r.xs[i]);
    return [start, toX(r.xs[i] + stepWidth)];
  };
  const pointX = (r: Resolved, i: number): number =>
    // On an interval grid a point describes the whole interval; its marker
    // belongs in the middle of it, while a step's transition is on the edge.
    grid !== null ? (bandFor(r, i)[0] + bandFor(r, i)[1]) / 2 : toX(r.xs[i]);

  const barSeries = resolved.filter((r) => r.series.style === "bar");

  const description = `${leaf.title || "Chart"}: ${leaf.series
    .map((s) => `${s.label} (${s.style})`)
    .join(", ")}`;

  return (
    <figure className="wb-vis-leaf">
      <LeafTitle text={leaf.title} />
      <div className="wb-vis-scroll" ref={host}>
        <svg
          className="wb-vis-chart"
          width={width}
          height={CHART_H}
          viewBox={`0 0 ${String(width)} ${String(CHART_H)}`}
          role="img"
          aria-label={description}
        >
          <defs>
            <clipPath id={clipId}>
              <rect x={plot.x0} y={plot.y0} width={plot.x1 - plot.x0} height={plot.y1 - plot.y0} />
            </clipPath>
          </defs>
          {left?.ticks.map((tick) => (
            <g key={`ly${String(tick.value)}`}>
              <line
                className="wb-vis-grid"
                x1={plot.x0}
                x2={plot.x1}
                y1={left.to(tick.value)}
                y2={left.to(tick.value)}
              />
              <text className="wb-vis-tick is-y" x={plot.x0 - 8} y={left.to(tick.value)}>
                {tick.label}
              </text>
            </g>
          ))}
          {rightAxis?.ticks.map((tick) => (
            <text
              key={`ry${String(tick.value)}`}
              className="wb-vis-tick is-y2"
              x={plot.x1 + 8}
              y={rightAxis.to(tick.value)}
            >
              {tick.label}
            </text>
          ))}
          {xTicks.map((tick) => (
            <text
              key={`x${String(tick.value)}`}
              className="wb-vis-tick is-x"
              x={toX(tick.value)}
              y={plot.y1 + 16}
            >
              {tick.label}
            </text>
          ))}
          <line className="wb-vis-axis" x1={plot.x0} x2={plot.x1} y1={plot.y1} y2={plot.y1} />
          <g clipPath={`url(#${clipId})`}>
            {resolved.map((r) => {
              const axis = r.series.axis === "right" ? rightAxis : left;
              if (axis === null) return null;
              const cls = `wb-vis-series is-s${String((r.index % SERIES_COUNT) + 1)}`;
              if (r.series.style === "bar") {
                const slot = barSeries.findIndex((b) => b.index === r.index);
                const width = (toX(stepWidth) - toX(0)) / Math.max(1, barSeries.length);
                const zero = axis.to(Math.max(axis.domain[0], Math.min(0, axis.domain[1])));
                return (
                  <g key={r.index} className={cls}>
                    {r.series.values.map((value, i) => {
                      const y = axis.to(value);
                      return (
                        <rect
                          key={i}
                          className="wb-vis-bar"
                          x={bandFor(r, i)[0] + slot * width + width * 0.1}
                          width={Math.max(1, width * 0.8)}
                          y={Math.min(y, zero)}
                          height={Math.max(1, Math.abs(zero - y))}
                        />
                      );
                    })}
                  </g>
                );
              }
              if (r.series.style === "scatter") {
                return (
                  <g key={r.index} className={cls}>
                    {r.series.values.map((value, i) => (
                      <circle
                        key={i}
                        className="wb-vis-dot"
                        cx={pointX(r, i)}
                        cy={axis.to(value)}
                        r={2.5}
                      />
                    ))}
                  </g>
                );
              }
              const points: string[] = [];
              r.series.values.forEach((value, i) => {
                const y = axis.to(value);
                if (r.series.style === "step") {
                  const [from, to] = bandFor(r, i);
                  points.push(
                    `${i === 0 ? "M" : "L"}${String(from)} ${String(y)}`,
                    `L${String(to)} ${String(y)}`,
                  );
                } else {
                  points.push(`${i === 0 ? "M" : "L"}${String(pointX(r, i))} ${String(y)}`);
                }
              });
              return <path key={r.index} className={`${cls} wb-vis-line`} d={points.join(" ")} />;
            })}
          </g>
        </svg>
      </div>
      <figcaption className="wb-vis-legend">
        {leaf.series.map((series, index) => (
          <span key={index} className={`wb-vis-key is-s${String((index % SERIES_COUNT) + 1)}`}>
            <span className={`wb-vis-swatch is-${series.style}`} aria-hidden="true" />
            {/* The legend key *is* the series anchor: it is where a reader
                already points when they mean "that line". */}
            <Part
              path={["leaf", at, "series", nameOrIndex(seriesLabels, index)]}
              label={partLabel(leaf.title, "chart", series.label)}
            >
              {series.label}
            </Part>
            {(series.axis === "right" ? leaf.y_right?.unit : leaf.y.unit) !== "" && (
              <span className="wb-vis-unit">
                {series.axis === "right" ? leaf.y_right?.unit : leaf.y.unit}
              </span>
            )}
          </span>
        ))}
        {grid !== null && <span className="wb-vis-grid-caption">{gridCaption(grid)}</span>}
        {grid === null && leaf.x.label !== "" && (
          <span className="wb-vis-grid-caption">{leaf.x.label}</span>
        )}
      </figcaption>
      {pickable && <PointStrip leaf={leaf} at={at} labels={seriesLabels} />}
    </figure>
  );
}

// ---- diagram -----------------------------------------------------------------

const DIAGRAM_W = 700;
const NODE_H = 34;
const LAYER_GAP = 46;
const NODE_GAP = 16;
const DIAGRAM_PAD = 8;
/** Below this a box cannot hold a label, so the diagram scrolls instead. */
const MIN_NODE_W = 108;

function DiagramView({ leaf, at }: { leaf: DiagramLeaf } & LeafAt) {
  const arrowId = useId();
  const pickable = usePickable();
  const placed = layerNodes(leaf.nodes, leaf.edges);
  const byId = new Map(placed.map((node) => [node.id, node]));
  const layers = Math.max(...placed.map((node) => node.layer)) + 1;
  const widest = Math.max(...placed.map((node) => node.layerSize));
  const floor = widest * MIN_NODE_W + (widest - 1) * NODE_GAP + 2 * DIAGRAM_PAD;
  const [host, width] = useDrawWidth(DIAGRAM_W, floor);
  const nodeW = Math.min(190, (width - 2 * DIAGRAM_PAD - (widest - 1) * NODE_GAP) / widest);
  const height = DIAGRAM_PAD * 2 + layers * NODE_H + (layers - 1) * LAYER_GAP;

  const boxX = (node: { index: number; layerSize: number }): number => {
    const total = node.layerSize * nodeW + (node.layerSize - 1) * NODE_GAP;
    return (width - total) / 2 + node.index * (nodeW + NODE_GAP);
  };
  const boxY = (node: { layer: number }): number => DIAGRAM_PAD + node.layer * (NODE_H + LAYER_GAP);

  return (
    <figure className="wb-vis-leaf">
      <LeafTitle text={leaf.title} />
      <div className="wb-vis-scroll" ref={host}>
        <svg
          className="wb-vis-diagram"
          width={width}
          height={height}
          viewBox={`0 0 ${String(width)} ${String(height)}`}
          // `role="img"` gives the drawing one accessible name and makes its
          // children presentational — right while the picture is a picture, and
          // wrong the moment its boxes are things you can pick. In annotate
          // mode it becomes a group so the parts inside are announced.
          role={pickable ? "group" : "img"}
          aria-label={`${leaf.title || "Diagram"}: ${leaf.nodes
            .map((n) => roleLabel(n.label, n.role))
            .join(", ")}`}
        >
          <defs>
            <marker
              id={arrowId}
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path className="wb-vis-arrow" d="M0 0 L8 4 L0 8 z" />
            </marker>
          </defs>
          {leaf.edges.map((edge, index) => {
            const from = byId.get(edge.source);
            const to = byId.get(edge.target);
            if (from === undefined || to === undefined) return null;
            const x1 = boxX(from) + nodeW / 2;
            const y1 = boxY(from) + NODE_H;
            const x2 = boxX(to) + nodeW / 2;
            const y2 = boxY(to);
            const bend = Math.max(16, (y2 - y1) / 2);
            return (
              <SvgPart
                key={index}
                className="wb-vis-edge-part"
                path={["leaf", at, "edge", index]}
                label={partLabel(leaf.title, "diagram", `arrow ${edge.source} to ${edge.target}`)}
                markerAt={{ x: (x1 + x2) / 2 + 10, y: (y1 + y2) / 2 - 8 }}
              >
                <path
                  className="wb-vis-edge"
                  markerEnd={`url(#${arrowId})`}
                  d={`M${String(x1)} ${String(y1)} C${String(x1)} ${String(y1 + bend)} ${String(x2)} ${String(y2 - bend)} ${String(x2)} ${String(y2)}`}
                />
                {edge.label !== "" && (
                  <text className="wb-vis-edge-label" x={(x1 + x2) / 2} y={(y1 + y2) / 2}>
                    {edge.label}
                  </text>
                )}
              </SvgPart>
            );
          })}
          {leaf.nodes.map((node) => {
            const box = byId.get(node.id);
            if (box === undefined) return null;
            return (
              <SvgPart
                key={node.id}
                className={`wb-vis-node${roleClass(node.role)}`}
                path={["leaf", at, "node", node.id]}
                label={partLabel(leaf.title, "diagram", roleLabel(node.label, node.role))}
                markerAt={{ x: boxX(box) + nodeW - 8, y: boxY(box) + 12 }}
              >
                <rect x={boxX(box)} y={boxY(box)} width={nodeW} height={NODE_H} rx={4} />
                <text x={boxX(box) + nodeW / 2} y={boxY(box) + NODE_H / 2}>
                  {node.label}
                </text>
              </SvgPart>
            );
          })}
        </svg>
      </div>
    </figure>
  );
}

// ---- code diff ---------------------------------------------------------------

const DIFF_SIGN: Record<string, string> = { same: " ", add: "+", del: "−" };
const DIFF_WORD: Record<string, string> = {
  same: "",
  add: "Added",
  del: "Removed",
};

/**
 * A diff line is anchored by the **side and line number of the payload**, not
 * by its position in the rendered diff.
 *
 * `matchLines` is ours: it decides which before-line pairs with which
 * after-line, and its output would change if that matcher ever improved. An
 * anchor pointing into it would silently start meaning a different line. Side
 * plus line number points into `before`/`after` — the strings the agent sent
 * and still holds — which is also the only form `services/plan_anchors.py` can
 * validate without re-implementing the matcher. A hunk is therefore addressed
 * by the line it starts at; the payload has no hunks, and an anchor the server
 * cannot check is exactly what anchors exist to avoid.
 */
function CodeDiffView({ leaf, at }: { leaf: CodeDiffLeaf } & LeafAt) {
  const lines = matchLines(leaf.before, leaf.after);
  return (
    <figure className="wb-vis-leaf">
      <LeafTitle text={leaf.title} />
      <pre className="wb-vis-diff" data-lang={leaf.language === "" ? undefined : leaf.language}>
        <code>
          {lines.map((line, index) => {
            // `matchLines` already carries which line of which side this is
            // (1-based, null where absent). A `same` line exists on both; it is
            // anchored to `after`, which is the state being proposed.
            const side = line.kind === "del" ? "before" : "after";
            const number = (line.kind === "del" ? line.beforeNo : line.afterNo) ?? 1;
            return (
              <span key={index} className={`wb-vis-diff-line is-${line.kind}`}>
                {/* The sign carries the meaning, so the diff survives without
                    colour (DESIGN.md §7). */}
                <span className="wb-vis-diff-sign" aria-hidden="true">
                  {DIFF_SIGN[line.kind]}
                </span>
                {line.kind !== "same" && <span className="u-sr-only">{DIFF_WORD[line.kind]}: </span>}
                <Part
                  path={["leaf", at, "side", side, "line", number - 1]}
                  label={partLabel(
                    leaf.title,
                    "diff",
                    `${side} line ${String(number)}`,
                    line.text.trim(),
                  )}
                >
                  {line.text}
                </Part>
                {"\n"}
              </span>
            );
          })}
        </code>
      </pre>
    </figure>
  );
}

// ---- metrics -----------------------------------------------------------------

function MetricsView({ leaf, at }: { leaf: MetricsLeaf } & LeafAt) {
  const labels = leaf.items.map((metric) => metric.label);
  return (
    <figure className="wb-vis-leaf">
      <LeafTitle text={leaf.title} />
      <dl className="wb-vis-metrics">
        {leaf.items.map((metric, index) => (
          <div key={index} className={`wb-vis-metric${roleClass(metric.role)}`}>
            <dt>
              {metric.role !== "neutral" && (
                <span className="u-sr-only">{ROLE_WORD[metric.role]}: </span>
              )}
              <Part
                path={["leaf", at, "metric", nameOrIndex(labels, index)]}
                label={partLabel(leaf.title, "metrics", roleLabel(metric.label, metric.role))}
              >
                {metric.label}
              </Part>
            </dt>
            <dd>
              <span className="wb-vis-figure u-tabular">{metric.value}</span>
              {metric.unit !== "" && <span className="wb-vis-unit">{metric.unit}</span>}
            </dd>
          </div>
        ))}
      </dl>
    </figure>
  );
}

// ---- blocks ------------------------------------------------------------------

function LeafView({ leaf, at }: { leaf: VisualLeaf } & LeafAt) {
  switch (leaf.kind) {
    case "table":
      return <TableView leaf={leaf} at={at} />;
    case "chart":
      return <ChartView leaf={leaf} at={at} />;
    case "diagram":
      return <DiagramView leaf={leaf} at={at} />;
    case "code_diff":
      return <CodeDiffView leaf={leaf} at={at} />;
    case "metrics":
      return <MetricsView leaf={leaf} at={at} />;
  }
}

function BlockView({ block, from }: { block: VisualBlock; from: number }) {
  return (
    <div className={`wb-vis-block is-${block.layout}`}>
      {block.items.map((leaf, index) => (
        <LeafView key={index} leaf={leaf} at={from + index} />
      ))}
    </div>
  );
}

/**
 * A `visual` plan node, rendered inside the plan card.
 *
 * `annotation` is what turns the drawing into a surface you can point at. It is
 * optional and defaults to nothing: a visual outside a plan card (a future
 * panel, PR 4) renders identically with no anchors and no cost.
 */
export function VisualView({
  node,
  annotation,
}: {
  node: VisualNode;
  annotation?: VisualAnnotation;
}) {
  // The flat leaf index every anchor starts from, accumulated once here so the
  // numbering has a single home (`../plan/anchors.ts` mirrors it for lookups).
  let from = 0;
  const blocks = node.blocks.map((block, index) => {
    const at = from;
    from += block.items.length;
    return <BlockView key={index} block={block} from={at} />;
  });
  return (
    <AnnotateProvider value={annotation ?? null}>
      <section className="wb-vis" aria-label={node.title === "" ? "Visual" : node.title}>
        {node.title !== "" && <h3 className="wb-vis-heading">{node.title}</h3>}
        {blocks}
      </section>
    </AnnotateProvider>
  );
}
