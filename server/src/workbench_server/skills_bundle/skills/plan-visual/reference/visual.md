# The `visual` node, in full

Progressive disclosure: this file is **not** loaded with the skill. Read it once
before authoring your first `visual`, then work from `SKILL.md`.

You send structure and numbers. Workbench computes every coordinate and emits
every pixel from its own React/SVG components in the app's design tokens. There
is no field for markup, HTML, SVG, CSS, a URL, an image or a coordinate — an
artifact cannot fetch anything, run anything, or say anything to you. Anything
you cannot express below, say in a `markdown` node instead.

## Shape

```
visual        node_id, title (<= 80), blocks (1–6)
  block       layout: single | row | grid | split, items (1–4 leaves)
    leaf      table | chart | diagram | code_diff | metrics
```

Blocks hold leaves. Leaves hold data. **Nothing nests further**, and at most
**8 leaves** and **3 visual nodes** exist in one card.

- `single` — exactly 1 item. `split` — exactly 2, read as before/after.
- `row` — side by side, `grid` — wrapping. Both stack automatically in a narrow
  panel, so never split something that must be read together.

## Leaves

### table

```json
{"kind": "table", "title": "Fold hours",
 "columns": [{"label": "Hour"},
             {"label": "Price", "type": "numeric", "unit": "EUR/MWh"},
             {"label": "Source", "type": "code"}],
 "rows": [["02:00 (1st)", "-3.8", "fold=0"],
          ["02:00 (2nd)", "-6.1", "fold=1"]],
 "highlights": [{"row": 1, "column": 1, "role": "error"}]}
```

- `type`: `text` (default) | `numeric` | `code`. **`numeric` is what makes a
  column right-align in tabular figures** — set it on every column of figures,
  and set `unit` on it. Cells are strings: you choose the digits, we choose the
  typography.
- Caps: 8 columns, 50 rows, every row exactly as long as `columns`.
- `highlights` (<= 20): `{row, column?, role}`; omit `column` for a whole row.
  `role` is one of `accent | success | warning | error` (`neutral` = none) and
  is the *only* colour vocabulary — the renderer pairs each with a border and a
  screen-reader word, so it never means colour alone.

### chart

```json
{"kind": "chart", "title": "Price and dispatch",
 "x": {"kind": "time", "start": "2026-10-25T00:00:00+02:00",
       "step_minutes": 60, "timezone": "Europe/Stockholm"},
 "y": {"kind": "value", "label": "Price", "unit": "EUR/MWh"},
 "y_right": {"kind": "value", "label": "Dispatch", "unit": "MW"},
 "series": [
   {"label": "SE3 day-ahead", "values": [41.2, 38.7, 36.1]},
   {"label": "Åsen 2", "style": "step", "values": [0, -5, -5], "axis": "right"}]}
```

- `series`: <= 6, each <= 400 `values`. `style`: `line` (default) | `bar` |
  `step` | `scatter`.
- `x` is either `{"kind": "value", label, unit, scale}` or
  `{"kind": "time", ...}`. `y` and `y_right` are always value axes.
- `scale`: `linear` (default) | `log`. A log axis rejects non-positive values —
  negative prices are real, so keep price axes linear.
- Two units on one chart: put the second on `y_right` and set
  `"axis": "right"` on its series. One unit? Leave `y_right` out.
- On a **value** x axis, give each series an explicit `x` of the same length, or
  omit it and the points are indexed 0..n-1.

#### time axes and DST

`start` is an **instant** (ISO-8601 *with an offset*), and point `i` is
`start + i * step_minutes` in absolute time. Labels are computed in `timezone`
(an IANA name, validated — an unknown one is a tool error).

That is what makes clock-change days true:

| day | points | labels |
|---|---|---|
| normal | 24 | 00:00 … 23:00 |
| spring forward (last Sun in March) | **23** | 01:00 → 03:00, no 02:00 |
| autumn back (last Sun in October) | **25** | 02:00 twice |

Send as many `values` as the day really has. Never send `x` on a time axis —
the grid supplies it, and a second source of truth is how a fold gets drawn
twice.

### diagram

```json
{"kind": "diagram", "title": "Pipeline",
 "nodes": [{"id": "feed", "label": "TGN feed"},
           {"id": "opt", "label": "Optimizer", "role": "accent"}],
 "edges": [{"source": "feed", "target": "opt", "label": "hourly"}]}
```

<= 20 nodes, <= 30 edges. **Acyclic** — we lay it out as a layered DAG, so a
cycle or a self-loop is a validation error. No coordinates: layers, order and
spacing are ours.

### code_diff

```json
{"kind": "code_diff", "title": "calendar.py", "language": "python",
 "before": "hours = range(24)\n", "after": "hours = delivery_hours(day, tz)\n"}
```

<= 2000 chars and <= 120 lines a side; at least one side non-empty. We match the
lines and render the diff. `language` is a lowercase tag (`python`, `sql`) — it
labels the block and nothing else.

### metrics

```json
{"kind": "metrics", "items": [
  {"label": "Delivery hours", "value": "25", "role": "accent"},
  {"label": "Revenue", "value": "18 420", "unit": "EUR"}]}
```

<= 6 figures, `value` pre-formatted (<= 24 chars). For headline numbers only —
a row of metrics is not a table.

## When to draw

Draw when the shape *is* the answer: a profile over time, a before/after, a
pipeline, a comparison the reader would otherwise assemble from prose. Do not
draw three numbers, a restatement of your step list, or decoration. A card the
user has to scroll past is worse than the sentence it replaced.
