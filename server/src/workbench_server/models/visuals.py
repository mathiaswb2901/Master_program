"""The visual scene graph: what an agent may *draw* inside a plan card.

``models/plans.py`` says why the plan schema is closed — "free-form HTML from a
model is not a product primitive; this is". This module extends that stance to
pictures. The model sends **structure and numbers**; Workbench computes every
coordinate and emits every pixel from its own React/SVG components. No markup,
no HTML, no SVG strings, no CSS, no URLs and no image sources cross the wire, so
the closed-union safety posture survives intact: there is no field a payload can
put a script, a fetch, or an instruction to the agent into, because none of the
leaf kinds has one. ``test_visuals.py`` asserts that over the schema itself, so
the next field to arrive here cannot quietly open one.

**Depth 2, and non-recursive on purpose.** A ``VisualNode`` holds blocks; a
block holds *leaves* and nothing else. That is a security property (no nesting
bomb, no payload whose rendering cost is unbounded in its own depth), a budget
property (the inlined JSON Schema the model reads terminates and stays small —
``plan_input_schema`` inlines every ``$ref``, so one recursive edge would be a
non-terminating flatten), and a design property (a card is read in a 760px chat
column, not a dashboard). Keep it that way.

**Class docstrings are context the model pays for.** Every one of them lands in
the tool's input schema, on every request of every session, so they are kept to
a line; the reasoning lives in ``#`` comments beside them, which are free.

**One semantic vocabulary.** :data:`VisualRole` is the only colour language in
here — the same five words for a highlighted table cell, a diagram node and a
metric. Colour is never the sole signal (DESIGN.md §7): the renderer pairs every
role with a border weight and a screen-reader word.

Every cap below is a product limit enforced as validation, not a comment: an
oversized artifact comes back to the agent as a fixable tool error rather than a
renderer that wedges the chat column.
"""

from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

# ---- caps -------------------------------------------------------------------

MAX_VISUAL_BLOCKS = 6
MAX_BLOCK_ITEMS = 4
#: Leaves in one visual node, across all its blocks. Blocks times items would allow
#: 24; eight is what stays readable stacked in a chat column.
MAX_VISUAL_LEAVES = 8
MAX_TABLE_ROWS = 50
MAX_TABLE_COLUMNS = 8
MAX_TABLE_HIGHLIGHTS = 20
MAX_SERIES = 6
#: Points per series. A day of quarter-hours is 96; a month of hours is 744.
#: 400 covers a fortnight of hourly dispatch and bounds the SVG path we build.
MAX_POINTS = 400
MAX_DIAGRAM_NODES = 20
MAX_DIAGRAM_EDGES = 30
MAX_METRICS = 6
MAX_DIFF_CHARS = 2000
#: Lines per side of a diff. The renderer's line matcher is O(before * after),
#: so this is what keeps a pathological payload from pinning the UI thread.
MAX_DIFF_LINES = 120

#: Drawn marks in one visual node, summed over its leaves — a chart point, a
#: table cell, a diagram box or arrow, a diff line, a metric. One mark is
#: roughly one DOM element the renderer emits.
#:
#: Every cap above bounds *one* leaf; only this one bounds their product, and
#: the product is where the arithmetic stops being reassuring. Eight leaves at
#: the chart cap is 8 x 6 x 400 = 19,200 points, and three visual nodes is a
#: card of 57,600 SVG elements — every individual cap respected and a chat
#: column that stops responding. 6,000 leaves room for two full-width charts
#: (2 x 6 x 400 = 4,800) plus the tables and metrics that are read with them,
#: and holds a whole card to 3 x 6,000 = 18,000 marks, which
#: ``ui/src/visual/budget.test.tsx`` renders inside a measured budget.
MAX_VISUAL_MARKS = 6000

#: The whole colour language of a visual. "neutral" is the absence of a role.
VisualRole = Literal["neutral", "accent", "success", "warning", "error"]

Label = Annotated[str, Field(max_length=60)]
Title = Annotated[str, Field(max_length=80)]
Unit = Annotated[str, Field(max_length=16)]
Cell = Annotated[str, Field(max_length=120)]
Key = Annotated[str, Field(min_length=1, max_length=40)]


# ---- table ------------------------------------------------------------------


# ``type`` is what makes a number render as a number: numeric right-aligns and
# sets tabular-nums because the *schema* says these are figures (DESIGN.md
# principle 7), not because a heuristic sniffed the strings. ``unit`` renders
# under the label — silent unit mismatches (MW vs MWh, EUR/MWh vs EUR/kWh) are
# this domain's most common lie, and a column that states its unit cannot tell it.
class TableColumn(BaseModel):
    """A column: numeric right-aligns in tabular figures, code renders mono."""

    label: Label
    type: Literal["text", "numeric", "code"] = "text"
    unit: Unit = ""


class CellHighlight(BaseModel):
    """Give one cell — or a whole row, omitting `column` — a semantic role."""

    row: int = Field(ge=0)
    column: int | None = Field(default=None, ge=0)
    role: VisualRole = "accent"


# Cells are strings: the model decides significant digits and separators (it
# knows what the number means), while alignment, figures and emphasis come from
# the column type and the highlights — from the schema, never from the text.
class TableLeaf(BaseModel):
    """Typed columns and rows of pre-formatted cells."""

    kind: Literal["table"] = "table"
    title: Title = ""
    columns: list[TableColumn] = Field(min_length=1, max_length=MAX_TABLE_COLUMNS)
    rows: list[list[Cell]] = Field(min_length=1, max_length=MAX_TABLE_ROWS)
    highlights: list[CellHighlight] = Field(default_factory=list, max_length=MAX_TABLE_HIGHLIGHTS)

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        width = len(self.columns)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(f"row {index} has {len(row)} cells, expected {width}")
        for highlight in self.highlights:
            if highlight.row >= len(self.rows):
                raise ValueError(f"highlight row {highlight.row} is past the last row")
            if highlight.column is not None and highlight.column >= width:
                raise ValueError(f"highlight column {highlight.column} is past the last column")
        return self


# ---- chart ------------------------------------------------------------------


# Inlined three times per chart (x, y, y_right), so its docstring is paid for
# three times too — keep it to a clause.
class ValueAxis(BaseModel):
    """A numeric axis; `log` needs positive values."""

    kind: Literal["value"] = "value"
    label: Label = ""
    unit: Unit = ""
    scale: Literal["linear", "log"] = "linear"


# ``start`` is an *instant* and every later point is ``start + i * step_minutes``
# in absolute time, so the axis is linear in real time and a DST day is drawn at
# its true length. ``timezone`` is the zone the tick labels are computed in —
# which is where a 23-hour day loses its 02:00 and a 25-hour day shows it twice.
#
# That split is the whole point. A grid expressed in local wall-clock hours
# silently invents an hour every spring and drops one every autumn; a grid
# expressed in UTC with UTC labels is honest but unreadable to anyone holding a
# market calendar. Absolute spacing plus local labels is the only pair that is
# both, and it is the reason `step` and this axis arrived together.
class TimeAxis(BaseModel):
    """A regular grid: `start` (an instant, with offset) plus `step_minutes`,
    labelled in `timezone` (IANA), so a 23- or 25-hour day is drawn correctly."""

    kind: Literal["time"] = "time"
    label: Label = ""
    start: AwareDatetime
    step_minutes: int = Field(default=60, ge=1, le=1440)
    timezone: str = Field(default="UTC", max_length=40)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, zone: str) -> str:
        """Reject a zone we cannot resolve, rather than letting the renderer
        fall back to UTC and label a Nordic day with German hours. The browser
        throws on an unknown zone too, so this turns a broken card into a tool
        error the agent can fix."""
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone {zone!r}") from exc
        return zone


ChartXAxis = Annotated[ValueAxis | TimeAxis, Field(discriminator="kind")]


# ``step`` is first-class and not a styling choice: a dispatch schedule holds
# its value across a settlement period, and drawing it as a line between hour
# centres claims a ramp that never happened.
class ChartSeries(BaseModel):
    """One series. `step` holds each value across its interval (dispatch);
    `x` is only for a value axis — a time axis supplies x from its grid."""

    label: Label
    style: Literal["line", "bar", "step", "scatter"] = "line"
    values: list[float] = Field(min_length=1, max_length=MAX_POINTS)
    x: list[float] = Field(default_factory=list, max_length=MAX_POINTS)
    axis: Literal["left", "right"] = "left"

    @model_validator(mode="after")
    def _check_lengths(self) -> Self:
        if self.x and len(self.x) != len(self.values):
            raise ValueError(f"x has {len(self.x)} entries, values has {len(self.values)}")
        return self


# The second y axis exists for one shape the owner actually draws: price
# (EUR/MWh) against dispatch (MW) on one time grid. Two units on one linear axis
# is a category error, and two charts hides the correlation that is the entire
# reason to look.
class ChartLeaf(BaseModel):
    """Typed series on typed axes. `y_right` is the second unit (price vs MW)."""

    kind: Literal["chart"] = "chart"
    title: Title = ""
    x: ChartXAxis = ValueAxis()
    y: ValueAxis = ValueAxis()
    y_right: ValueAxis | None = None
    series: list[ChartSeries] = Field(min_length=1, max_length=MAX_SERIES)

    @model_validator(mode="after")
    def _check_axes(self) -> Self:
        on_time_grid = isinstance(self.x, TimeAxis)
        for series in self.series:
            if on_time_grid and series.x:
                raise ValueError(f"series {series.label!r}: a time axis supplies x, do not send it")
            if series.axis == "right" and self.y_right is None:
                raise ValueError(f"series {series.label!r} uses the right axis, which is not set")
            axis = self.y_right if series.axis == "right" else self.y
            if axis is not None and axis.scale == "log" and min(series.values) <= 0:
                raise ValueError(f"series {series.label!r} has non-positive values on a log axis")
        return self


# ---- diagram ----------------------------------------------------------------


class DiagramNode(BaseModel):
    """A box. `role` tints it; ids are referenced by edges."""

    id: Key
    label: Label
    role: VisualRole = "neutral"


class DiagramEdge(BaseModel):
    """A directed arrow between two node ids."""

    source: Key
    target: Key
    label: str = Field(default="", max_length=40)


# Workbench lays this out as a layered DAG (longest-path layering) and emits the
# SVG, which is why acyclicity is validated here rather than defended against in
# the renderer: a cycle has no layering, and a layout that silently broke one
# would draw a picture the payload does not describe.
class DiagramLeaf(BaseModel):
    """Nodes and edges, never coordinates: we lay it out as an acyclic DAG."""

    kind: Literal["diagram"] = "diagram"
    title: Title = ""
    nodes: list[DiagramNode] = Field(min_length=1, max_length=MAX_DIAGRAM_NODES)
    edges: list[DiagramEdge] = Field(default_factory=list, max_length=MAX_DIAGRAM_EDGES)

    @model_validator(mode="after")
    def _check_graph(self) -> Self:
        ids = [node.id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("diagram node id must be unique")
        known = set(ids)
        for edge in self.edges:
            for end in (edge.source, edge.target):
                if end not in known:
                    raise ValueError(f"edge names unknown node {end!r}")
            if edge.source == edge.target:
                raise ValueError(f"node {edge.source!r} has an edge to itself")
        if _has_cycle(ids, self.edges):
            raise ValueError("diagram must be acyclic — it is laid out as a layered DAG")
        return self


def _has_cycle(ids: list[str], edges: list[DiagramEdge]) -> bool:
    """Kahn's algorithm: a graph with a cycle never drains."""
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
    incoming = dict.fromkeys(ids, 0)
    for edge in edges:
        outgoing[edge.source].append(edge.target)
        incoming[edge.target] += 1
    queue = [node_id for node_id, count in incoming.items() if count == 0]
    seen = 0
    while queue:
        node_id = queue.pop()
        seen += 1
        for target in outgoing[node_id]:
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    return seen != len(ids)


# ---- code diff --------------------------------------------------------------


def diff_lines(body: str) -> int:
    """Lines in one side of a diff — an empty side is zero, not one.

    A trailing newline counts as one line more than the renderer will draw
    (``matchLines`` reads it as ending a line, not starting an empty one).
    Deliberate: this number is both a cap and a cost estimate, and a cap that
    over-counts by one rejects slightly early, while one that under-counts lets
    a payload through that the renderer then has to draw."""
    return body.count("\n") + 1 if body else 0


# ``language`` is a tag we put in a data attribute — never a loader, never a
# path; the pattern is what keeps it that way.
class CodeDiffLeaf(BaseModel):
    """Before/after source; we match the lines and render the diff."""

    kind: Literal["code_diff"] = "code_diff"
    title: Title = ""
    language: str = Field(default="", max_length=20, pattern=r"^[a-z0-9+#._-]*$")
    before: str = Field(default="", max_length=MAX_DIFF_CHARS)
    after: str = Field(default="", max_length=MAX_DIFF_CHARS)

    @model_validator(mode="after")
    def _check_sides(self) -> Self:
        if not self.before and not self.after:
            raise ValueError("a code_diff needs at least one of before/after")
        for name, body in (("before", self.before), ("after", self.after)):
            lines = diff_lines(body)
            if lines > MAX_DIFF_LINES:
                raise ValueError(f"{name} has {lines} lines, at most {MAX_DIFF_LINES}")
        return self


# ---- metrics ----------------------------------------------------------------


# ``value`` is pre-formatted text for the same reason table cells are.
class Metric(BaseModel):
    """One labelled figure, rendered in tabular figures."""

    label: Label
    value: str = Field(min_length=1, max_length=24)
    unit: Unit = ""
    role: VisualRole = "neutral"


class MetricsLeaf(BaseModel):
    """A short row of headline figures."""

    kind: Literal["metrics"] = "metrics"
    title: Title = ""
    items: list[Metric] = Field(min_length=1, max_length=MAX_METRICS)


# ---- blocks -----------------------------------------------------------------

VisualLeaf = Annotated[
    TableLeaf | ChartLeaf | DiagramLeaf | CodeDiffLeaf | MetricsLeaf,
    Field(discriminator="kind"),
]


# Deliberately *one* model with a ``layout`` word rather than three container
# kinds in the block union. Both express the same thing, but a union would make
# ``plan_input_schema`` inline the five leaf schemas once per container — four
# copies of the most expensive part of the schema, in every session's context,
# on every request. One container, one copy.
class VisualBlock(BaseModel):
    """One band of the scene: `single` (1 item), `row`/`grid` (1-4),
    `split` (exactly 2 — before, after). Items are leaves; blocks never nest."""

    layout: Literal["single", "row", "grid", "split"] = "single"
    items: list[VisualLeaf] = Field(min_length=1, max_length=MAX_BLOCK_ITEMS)

    @model_validator(mode="after")
    def _check_layout(self) -> Self:
        if self.layout == "single" and len(self.items) != 1:
            raise ValueError("a single block holds exactly one item")
        if self.layout == "split" and len(self.items) != 2:
            raise ValueError("a split block holds exactly two items (before, after)")
        return self


def leaf_count(blocks: list[VisualBlock]) -> int:
    return sum(len(block.items) for block in blocks)


def leaf_marks(leaf: TableLeaf | ChartLeaf | DiagramLeaf | CodeDiffLeaf | MetricsLeaf) -> int:
    """What one leaf costs the renderer, counted in marks it must emit.

    Not an estimate of *time* — an estimate of DOM elements, which is what a
    browser's cost is linear in here (a scatter point is a ``<circle>``, a table
    cell a ``<td>``, a diff line a ``<span>``). A line or step series draws one
    path rather than one element per point, so counting its points over-states
    it; over-stating is the safe direction for a budget, and it keeps the number
    independent of a styling word the payload can change."""
    match leaf:
        case TableLeaf():
            return len(leaf.rows) * len(leaf.columns)
        case ChartLeaf():
            return sum(len(series.values) for series in leaf.series)
        case DiagramLeaf():
            return len(leaf.nodes) + len(leaf.edges)
        case CodeDiffLeaf():
            return diff_lines(leaf.before) + diff_lines(leaf.after)
        case MetricsLeaf():
            return len(leaf.items)


def mark_count(blocks: list[VisualBlock]) -> int:
    """The whole scene's cost — the number :data:`MAX_VISUAL_MARKS` bounds."""
    return sum(leaf_marks(leaf) for block in blocks for leaf in block.items)
