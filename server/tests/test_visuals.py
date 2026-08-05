"""The visual scene graph: every leaf kind, every cap, and the DST contract.

Three things this file is for, in order of how badly they would hurt:

1. **Safety.** The reason Workbench draws its own pixels instead of adopting a
   third-party artifact bridge is that nothing in a model-authored payload may
   become executable, reach the network, or address the agent. That is a
   property of the *schema* — there is no URL, no image source, no markup field
   — so it is asserted over the schema itself, where a future field that opened
   one would fail rather than ship.
2. **The DST contract.** A generic time axis lies about a 23- or 25-hour day.
   The grid is absolute (start + i * step) and the labels are local, and this
   file pins that both ways round on real Nordic clock-change dates. The
   renderer implements the same rule in TypeScript (``ui/src/visual/timeAxis.ts``)
   and its vitest suite asserts the same days — two implementations agreeing on
   the same dates is the point.
3. **The caps.** Every one of them is reachable from an agent's arguments, so
   every one gets a rejection case: an oversized artifact must come back as a
   tool error it can fix, never as a renderer that wedges the chat column.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from workbench_server.models.plans import (
    MAX_VISUAL_NODES,
    PlanArtifact,
    VisualNode,
    plan_input_schema,
)
from workbench_server.models.visuals import (
    MAX_DIAGRAM_EDGES,
    MAX_DIAGRAM_NODES,
    MAX_DIFF_LINES,
    MAX_METRICS,
    MAX_POINTS,
    MAX_SERIES,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_VISUAL_LEAVES,
    MAX_VISUAL_MARKS,
    ChartLeaf,
    CodeDiffLeaf,
    DiagramLeaf,
    MetricsLeaf,
    TableLeaf,
    TimeAxis,
    VisualBlock,
    diff_lines,
    leaf_count,
    leaf_marks,
    mark_count,
)

CET = timezone(timedelta(hours=1))
CEST = timezone(timedelta(hours=2))
NORDIC = "Europe/Stockholm"


def leaf(kind: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, **fields}


def table(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "table",
        "columns": [
            {"label": "Hour"},
            {"label": "Price", "type": "numeric", "unit": "EUR/MWh"},
        ],
        "rows": [["00:00", "41.2"], ["01:00", "38.7"]],
    }
    payload.update(overrides)
    return payload


def chart(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "chart",
        "series": [{"label": "SE3", "values": [1.0, 2.0, 3.0]}],
    }
    payload.update(overrides)
    return payload


def full_chart(points: int = MAX_POINTS, series: int = MAX_SERIES) -> dict[str, Any]:
    """A chart at its own caps, drawn as ``scatter`` — the worst DOM case there
    is, one ``<circle>`` per point, where a line series would emit one path."""
    return {
        "kind": "chart",
        "series": [
            {"label": f"s{i}", "style": "scatter", "values": [float(n) for n in range(points)]}
            for i in range(series)
        ],
    }


def worst_case_blocks(marks: int) -> list[dict[str, Any]]:
    """``MAX_VISUAL_LEAVES`` leaves totalling exactly ``marks``, with every
    other cap also at its limit: two scatter charts at 6 x 400, two tables at
    50 x 8, a 20-node/30-edge diagram, a 120-line-a-side diff, six metrics, and
    one chart sized to land the total on the number asked for.

    A payload no agent would send, which is the point — it is the shape the
    per-leaf caps permit, and the one the aggregate cap has to answer for."""
    diagram = {
        "kind": "diagram",
        "nodes": [{"id": f"n{i}", "label": f"N{i}"} for i in range(MAX_DIAGRAM_NODES)],
        # Forward-only, so it stays the acyclic DAG the layout needs: a chain
        # plus every skip-one edge, up to the edge cap.
        "edges": [
            {"source": f"n{i}", "target": f"n{i + span}"}
            for span in (1, 2)
            for i in range(MAX_DIAGRAM_NODES - span)
        ][:MAX_DIAGRAM_EDGES],
    }
    # Two sides that share no line, so the renderer's matcher really does emit
    # one row per line of each — 240, the number counted here.
    before = "\n".join("x = 1" for _ in range(MAX_DIFF_LINES))
    after = "\n".join("y = 2" for _ in range(MAX_DIFF_LINES))
    fixed: list[dict[str, Any]] = [
        full_chart(),
        full_chart(),
        table(
            columns=[{"label": f"c{i}"} for i in range(MAX_TABLE_COLUMNS)],
            rows=[[f"{r}.{c}" for c in range(MAX_TABLE_COLUMNS)] for r in range(MAX_TABLE_ROWS)],
        ),
        table(
            columns=[{"label": f"c{i}"} for i in range(MAX_TABLE_COLUMNS)],
            rows=[[f"{r}.{c}" for c in range(MAX_TABLE_COLUMNS)] for r in range(MAX_TABLE_ROWS)],
        ),
        diagram,
        leaf("code_diff", before=before, after=after),
        leaf("metrics", items=[{"label": f"m{i}", "value": "1"} for i in range(MAX_METRICS)]),
    ]
    spent = sum(
        leaf_marks(VisualBlock.model_validate({"items": [item]}).items[0]) for item in fixed
    )
    items = [*fixed, full_chart(points=marks - spent, series=1)]
    return [{"layout": "row", "items": items[i : i + 4]} for i in (0, 4)]


def visual(*items: dict[str, Any], layout: str = "single") -> dict[str, Any]:
    return {
        "kind": "visual",
        "node_id": "scene",
        "blocks": [{"layout": layout, "items": list(items)}],
    }


def plan_with(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"title": "A drawn plan", "nodes": list(nodes)}


def local_labels(axis: TimeAxis, count: int) -> list[str]:
    """The renderer's rule, in Python: step in absolute time, label in the
    market's zone. Any other order is the bug this schema exists to prevent."""
    zone = ZoneInfo(axis.timezone)
    step = timedelta(minutes=axis.step_minutes)
    return [(axis.start + step * i).astimezone(zone).strftime("%H:%M") for i in range(count)]


# ---- leaves round-trip -------------------------------------------------------


class TestLeafKinds:
    def test_every_leaf_kind_round_trips_through_a_plan(self) -> None:
        node = {
            "kind": "visual",
            "node_id": "scene",
            "title": "Everything",
            "blocks": [
                {"items": [table()]},
                {"items": [chart()]},
                {
                    "layout": "row",
                    "items": [
                        leaf(
                            "diagram",
                            nodes=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                            edges=[{"source": "a", "target": "b"}],
                        ),
                        leaf("code_diff", before="a = 1\n", after="a = 2\n", language="python"),
                    ],
                },
                {"items": [leaf("metrics", items=[{"label": "Cycles", "value": "1.4"}])]},
            ],
        }
        artifact = PlanArtifact.model_validate(plan_with(node))
        again = PlanArtifact.model_validate_json(artifact.model_dump_json())
        drawn = again.nodes[0]
        assert isinstance(drawn, VisualNode)
        kinds = [item.kind for block in drawn.blocks for item in block.items]
        assert kinds == ["table", "chart", "diagram", "code_diff", "metrics"]

    def test_a_numeric_column_is_a_schema_fact_not_a_guess(self) -> None:
        """The renderer right-aligns and sets tabular figures off ``type``. If
        that ever became sniffing of the cell text, this is what would fail."""
        leaf_model = TableLeaf.model_validate(table())
        assert [column.type for column in leaf_model.columns] == ["text", "numeric"]
        assert leaf_model.columns[1].unit == "EUR/MWh"

    def test_markup_looking_text_is_accepted_as_text(self) -> None:
        """The schema does not sanitize — it does not have to. Strings stay
        strings, and the renderer makes them text nodes (asserted in vitest and
        in the E2E journey); rejecting them here would be theatre that also
        broke the legitimate case of a table of HTML snippets."""
        payload = table(rows=[["<script>alert(1)</script>", "1"], ["</td><td>", "2"]])
        assert TableLeaf.model_validate(payload).rows[0][0] == "<script>alert(1)</script>"


# ---- caps and rejections -----------------------------------------------------


class TestTableCaps:
    def test_a_ragged_row_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expected 2"):
            TableLeaf.model_validate(table(rows=[["00:00", "41.2"], ["01:00"]]))

    def test_too_many_rows(self) -> None:
        rows = [[str(i), "1"] for i in range(MAX_TABLE_ROWS + 1)]
        with pytest.raises(ValidationError, match="at most 50"):
            TableLeaf.model_validate(table(rows=rows))

    def test_too_many_columns(self) -> None:
        columns = [{"label": f"c{i}"} for i in range(9)]
        with pytest.raises(ValidationError, match="at most 8"):
            TableLeaf.model_validate(table(columns=columns, rows=[["x"] * 9]))

    def test_a_highlight_past_the_end_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="past the last row"):
            TableLeaf.model_validate(table(highlights=[{"row": 9, "role": "error"}]))
        with pytest.raises(ValidationError, match="past the last column"):
            TableLeaf.model_validate(table(highlights=[{"row": 0, "column": 5, "role": "error"}]))

    def test_an_unknown_role_is_rejected(self) -> None:
        """One colour vocabulary across every leaf — a sixth word would need a
        token and a contrast pair, not a string."""
        with pytest.raises(ValidationError):
            TableLeaf.model_validate(table(highlights=[{"row": 0, "role": "danger"}]))


class TestChartCaps:
    def test_too_many_series(self) -> None:
        series = [{"label": f"s{i}", "values": [1.0]} for i in range(MAX_SERIES + 1)]
        with pytest.raises(ValidationError, match="at most 6"):
            ChartLeaf.model_validate(chart(series=series))

    def test_too_many_points(self) -> None:
        values = [1.0] * (MAX_POINTS + 1)
        with pytest.raises(ValidationError, match="at most 400"):
            ChartLeaf.model_validate(chart(series=[{"label": "s", "values": values}]))

    def test_mismatched_x_and_values(self) -> None:
        with pytest.raises(ValidationError, match="x has 2 entries"):
            ChartLeaf.model_validate(
                chart(series=[{"label": "s", "values": [1.0, 2.0, 3.0], "x": [0.0, 1.0]}])
            )

    def test_a_time_axis_refuses_an_explicit_x(self) -> None:
        """Two sources of truth for x is how a DST day gets drawn twice, once
        per source, and the two disagree."""
        payload = chart(
            x={"kind": "time", "start": "2026-10-25T00:00:00+02:00", "timezone": NORDIC},
            series=[{"label": "s", "values": [1.0, 2.0], "x": [0.0, 1.0]}],
        )
        with pytest.raises(ValidationError, match="a time axis supplies x"):
            ChartLeaf.model_validate(payload)

    def test_the_right_axis_must_exist_to_be_used(self) -> None:
        payload = chart(series=[{"label": "MW", "values": [1.0], "axis": "right"}])
        with pytest.raises(ValidationError, match="right axis, which is not set"):
            ChartLeaf.model_validate(payload)

    def test_a_log_axis_refuses_non_positive_values(self) -> None:
        """Negative prices are real and a log axis cannot draw them — better a
        tool error than a chart with a hole where the interesting hour was."""
        payload = chart(
            y={"kind": "value", "scale": "log"},
            series=[{"label": "price", "values": [10.0, -6.1]}],
        )
        with pytest.raises(ValidationError, match="non-positive values on a log axis"):
            ChartLeaf.model_validate(payload)

    def test_a_log_axis_accepts_positive_values(self) -> None:
        payload = chart(
            y={"kind": "value", "scale": "log"},
            series=[{"label": "load", "values": [10.0, 4000.0]}],
        )
        assert ChartLeaf.model_validate(payload).y.scale == "log"


class TestDiagramGraph:
    def base(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "diagram",
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "edges": [{"source": "a", "target": "b"}],
        }
        payload.update(overrides)
        return payload

    def test_a_cycle_is_rejected(self) -> None:
        """We lay it out as a layered DAG; a cycle has no layering, and a
        renderer that silently broke one would draw a graph nobody sent."""
        payload = self.base(edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "a"}])
        with pytest.raises(ValidationError, match="must be acyclic"):
            PlanArtifact.model_validate(plan_with(visual(payload)))

    def test_a_self_loop_is_rejected(self) -> None:
        payload = self.base(edges=[{"source": "a", "target": "a"}])
        with pytest.raises(ValidationError, match="edge to itself"):
            PlanArtifact.model_validate(plan_with(visual(payload)))

    def test_an_edge_to_an_unknown_node_is_rejected(self) -> None:
        payload = self.base(edges=[{"source": "a", "target": "ghost"}])
        with pytest.raises(ValidationError, match="unknown node"):
            PlanArtifact.model_validate(plan_with(visual(payload)))

    def test_duplicate_node_ids_are_rejected(self) -> None:
        payload = self.base(nodes=[{"id": "a", "label": "A"}, {"id": "a", "label": "B"}])
        with pytest.raises(ValidationError, match="node id must be unique"):
            PlanArtifact.model_validate(plan_with(visual(payload)))

    def test_a_forest_with_no_edges_is_fine(self) -> None:
        artifact = PlanArtifact.model_validate(plan_with(visual(self.base(edges=[]))))
        assert artifact.nodes[0].kind == "visual"


class TestCodeDiffCaps:
    def test_both_sides_empty_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one of before/after"):
            PlanArtifact.model_validate(plan_with(visual(leaf("code_diff"))))

    def test_one_side_empty_is_a_pure_addition(self) -> None:
        node = visual(leaf("code_diff", after="new = True\n"))
        assert PlanArtifact.model_validate(plan_with(node)).nodes[0].kind == "visual"

    def test_too_many_lines(self) -> None:
        """The renderer matches lines in O(before * after); this is the cap that
        keeps a pathological payload off the UI thread."""
        body = "\n".join("x" * 2 for _ in range(MAX_DIFF_LINES + 1))
        with pytest.raises(ValidationError, match="at most 120"):
            PlanArtifact.model_validate(plan_with(visual(leaf("code_diff", before=body))))

    def test_a_language_tag_is_a_tag_not_a_path(self) -> None:
        for bad in ("../etc/passwd", "python;rm -rf", "<script>"):
            with pytest.raises(ValidationError):
                PlanArtifact.model_validate(
                    plan_with(visual(leaf("code_diff", after="x", language=bad)))
                )


class TestBlockShape:
    def test_a_split_holds_exactly_two(self) -> None:
        with pytest.raises(ValidationError, match="exactly two items"):
            VisualBlock.model_validate({"layout": "split", "items": [table()]})

    def test_a_single_holds_exactly_one(self) -> None:
        with pytest.raises(ValidationError, match="exactly one item"):
            VisualBlock.model_validate({"layout": "single", "items": [table(), table()]})

    def test_a_block_never_nests(self) -> None:
        """Depth 2 is the whole shape: a block's items are leaves, so a block
        inside a block is not a deeper scene, it is an unknown leaf kind."""
        nested = {"layout": "row", "items": [{"layout": "row", "items": [table()]}]}
        with pytest.raises(ValidationError):
            VisualBlock.model_validate(nested)

    def test_leaf_budget_across_blocks(self) -> None:
        blocks = [{"layout": "row", "items": [table()] * 3} for _ in range(3)]
        node = {"kind": "visual", "node_id": "scene", "blocks": blocks}
        with pytest.raises(ValidationError, match=f"at most {MAX_VISUAL_LEAVES}"):
            PlanArtifact.model_validate(plan_with(node))

    def test_visual_nodes_per_card(self) -> None:
        nodes = [dict(visual(table()), node_id=f"v{i}") for i in range(MAX_VISUAL_NODES + 1)]
        with pytest.raises(ValidationError, match=f"at most {MAX_VISUAL_NODES}"):
            PlanArtifact.model_validate(plan_with(*nodes))


# ---- the whole scene's cost --------------------------------------------------
#
# Every cap above bounds one leaf. None of them bounds the *product*, and the
# product is the number that decides whether a card renders or wedges a chat
# column: eight leaves each inside the chart cap is 19,200 points, and a card
# holds three such nodes. This section is the aggregate, which is the only cap
# that can be argued from arithmetic rather than from a single field's size.


class TestRenderBudget:
    def test_marks_are_counted_per_leaf_kind(self) -> None:
        """One mark is roughly one element the renderer emits, so the count is
        per kind: cells, points, boxes-plus-arrows, diff lines, figures."""
        counted = {
            "table": leaf_marks(TableLeaf.model_validate(table())),
            "chart": leaf_marks(ChartLeaf.model_validate(chart())),
            "diagram": leaf_marks(
                DiagramLeaf.model_validate(
                    leaf(
                        "diagram",
                        nodes=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                        edges=[{"source": "a", "target": "b"}],
                    )
                )
            ),
            "code_diff": leaf_marks(
                CodeDiffLeaf.model_validate(leaf("code_diff", before="a\nb", after="a"))
            ),
            "metrics": leaf_marks(
                MetricsLeaf.model_validate(leaf("metrics", items=[{"label": "C", "value": "1"}]))
            ),
        }
        assert counted == {
            "table": 4,  # 2 rows x 2 columns
            "chart": 3,  # one series of three points
            "diagram": 3,  # two boxes and an arrow
            "code_diff": 3,  # two lines before, one after
            "metrics": 1,
        }
        # A trailing newline counts one line more than the renderer draws. The
        # budget over-counts on purpose (``diff_lines``): rejecting a shade
        # early is safe, admitting a payload we then have to draw is not.
        assert diff_lines("a\n") == 2

    def test_every_per_leaf_cap_at_once_is_still_rejected(self) -> None:
        """The scenario the per-leaf caps do not cover: eight leaves, each one a
        chart at *its* cap. Legal field by field, 19,200 SVG marks in one node,
        and three of these fit in a card. This is the arithmetic the aggregate
        cap exists to stop, and it must stop it as a fixable tool error."""
        biggest = full_chart()
        assert leaf_marks(ChartLeaf.model_validate(biggest)) == MAX_SERIES * MAX_POINTS
        blocks = [{"layout": "row", "items": [biggest] * 4} for _ in range(2)]
        node = {"kind": "visual", "node_id": "scene", "blocks": blocks}
        with pytest.raises(ValidationError, match="19200 drawn marks"):
            PlanArtifact.model_validate(plan_with(node))

    def test_the_error_tells_the_agent_what_to_send_instead(self) -> None:
        over = worst_case_blocks(MAX_VISUAL_MARKS + 1)
        node = {"kind": "visual", "node_id": "scene", "blocks": over}
        with pytest.raises(ValidationError, match="send fewer points, rows or leaves"):
            PlanArtifact.model_validate(plan_with(node))

    def test_the_worst_case_card_validates_and_is_bounded(self) -> None:
        """A node at the cap is *legal* — the budget is a ceiling, not a ban —
        and the whole card's worst case is that ceiling times the node cap.
        ``ui/src/visual/budget.test.tsx`` renders exactly this total and holds
        it to a measured time; the number below is the contract between them."""
        blocks = worst_case_blocks(MAX_VISUAL_MARKS)
        nodes = [
            {"kind": "visual", "node_id": f"v{i}", "blocks": blocks}
            for i in range(MAX_VISUAL_NODES)
        ]
        artifact = PlanArtifact.model_validate(plan_with(*nodes))
        drawn = [node for node in artifact.nodes if isinstance(node, VisualNode)]
        assert len(drawn) == MAX_VISUAL_NODES
        assert all(leaf_count(node.blocks) == MAX_VISUAL_LEAVES for node in drawn)
        total = sum(mark_count(node.blocks) for node in drawn)
        assert total == MAX_VISUAL_NODES * MAX_VISUAL_MARKS == 18_000

    def test_the_cap_still_admits_the_pictures_it_is_for(self) -> None:
        """A budget nobody can spend is a ban. Two full-width charts — a
        fortnight of hourly price and dispatch, six series each — plus the
        tables read beside them must fit, or the cap is set wrong."""
        two_charts = [{"layout": "row", "items": [full_chart(), full_chart()]}]
        assert (
            mark_count([VisualBlock.model_validate(block) for block in two_charts])
            == 2 * MAX_SERIES * MAX_POINTS
        )
        node = {"kind": "visual", "node_id": "scene", "blocks": two_charts}
        assert PlanArtifact.model_validate(plan_with(node)).nodes[0].kind == "visual"


# ---- the DST contract --------------------------------------------------------


class TestTimeAxis:
    def test_an_unknown_zone_is_rejected(self) -> None:
        """A zone the renderer cannot resolve would fall back to UTC and label a
        Nordic day with the wrong hours — silently, which is the whole problem."""
        with pytest.raises(ValidationError, match="unknown IANA time zone"):
            TimeAxis.model_validate({"start": "2026-10-25T00:00:00+02:00", "timezone": "Nordpool"})

    def test_a_naive_start_is_rejected(self) -> None:
        """Without an offset there is no instant, only a wall clock — and a wall
        clock is exactly what is ambiguous on the day this axis exists for."""
        with pytest.raises(ValidationError):
            TimeAxis.model_validate({"start": "2026-10-25T00:00:00", "timezone": NORDIC})

    def test_a_normal_day_is_twenty_four_hours(self) -> None:
        axis = TimeAxis(start=datetime(2026, 6, 15, tzinfo=CEST), timezone=NORDIC)
        labels = local_labels(axis, 24)
        assert labels[:4] == ["00:00", "01:00", "02:00", "03:00"]
        assert labels[-1] == "23:00"
        assert len(set(labels)) == 24

    def test_spring_forward_is_a_twenty_three_hour_day(self) -> None:
        """2026-03-29, Europe/Stockholm: 02:00 never happens. A grid that
        counted local hours would invent it; an absolute grid cannot."""
        axis = TimeAxis(start=datetime(2026, 3, 29, tzinfo=CET), timezone=NORDIC)
        labels = local_labels(axis, 23)
        assert labels[:4] == ["00:00", "01:00", "03:00", "04:00"]
        assert "02:00" not in labels
        assert labels[-1] == "23:00"
        # 23 hourly points really do cover the whole calendar day.
        end = axis.start + timedelta(minutes=axis.step_minutes) * 23
        assert end.astimezone(ZoneInfo(NORDIC)).date() == datetime(2026, 3, 30).date()

    def test_autumn_back_is_a_twenty_five_hour_day(self) -> None:
        """2026-10-25, Europe/Stockholm: 02:00 happens twice, at two different
        instants and two different prices. Both must be drawn."""
        axis = TimeAxis(start=datetime(2026, 10, 25, tzinfo=CEST), timezone=NORDIC)
        labels = local_labels(axis, 25)
        assert labels[:5] == ["00:00", "01:00", "02:00", "02:00", "03:00"]
        assert labels.count("02:00") == 2
        assert labels[-1] == "23:00"
        end = axis.start + timedelta(minutes=axis.step_minutes) * 25
        assert end.astimezone(ZoneInfo(NORDIC)).date() == datetime(2026, 10, 26).date()

    def test_quarter_hours_survive_the_fold(self) -> None:
        """15-minute settlement is the direction the market is going; the same
        rule has to hold at a finer step."""
        axis = TimeAxis(start=datetime(2026, 10, 25, tzinfo=CEST), step_minutes=15, timezone=NORDIC)
        labels = local_labels(axis, 100)
        assert labels.count("02:00") == 2
        assert labels.count("02:15") == 2
        assert labels[-1] == "23:45"

    def test_a_full_day_of_prices_validates_on_the_long_day(self) -> None:
        payload = chart(
            x={"kind": "time", "start": "2026-10-25T00:00:00+02:00", "timezone": NORDIC},
            y={"kind": "value", "label": "Price", "unit": "EUR/MWh"},
            series=[{"label": "SE3", "style": "step", "values": [float(i) for i in range(25)]}],
        )
        model = ChartLeaf.model_validate(payload)
        assert isinstance(model.x, TimeAxis)
        assert len(model.series[0].values) == 25
        assert model.series[0].style == "step"


# ---- the safety posture, asserted over the schema ----------------------------


class TestNothingExecutableCrossesTheWire:
    """The reason we draw our own pixels rather than adopting an artifact
    bridge. A payload may carry no URL we fetch, no image source, no markup and
    no handler, because no *field* of those shapes exists — asserted here so the
    next field to arrive cannot quietly open one."""

    #: Substrings that would name a field we would have to fetch, embed or
    #: execute. Deliberately broad: a false positive costs one rename.
    FORBIDDEN = (
        "url",
        "uri",
        "href",
        "src",
        "html",
        "svg",
        "script",
        "style",
        "css",
        "image",
        "img",
        "icon",
        "iframe",
        "embed",
        "link",
        "on_click",
        "onclick",
        "handler",
        "action",
        "command",
        "prompt_agent",
        "callback",
    )

    def properties(self, node: Any, in_names: bool = False) -> list[tuple[str, Any]]:
        """Every ``(name, subschema)`` pair in the whole inlined schema."""
        found: list[tuple[str, Any]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if in_names:
                    found.append((key, value))
                found += self.properties(value, in_names=key == "properties")
        elif isinstance(node, list):
            for item in node:
                found += self.properties(item)
        return found

    @staticmethod
    def free_text(schema: Any) -> bool:
        """Can this field carry a string we did not choose?

        A closed ``enum``/``const`` cannot: whatever it is called, the only
        values that validate are the handful we wrote. That is why the check
        below is about free text and not about names alone — ``ChartSeries.style``
        is one of four words and could never be CSS, while a plain string field
        called anything at all could be a URL.
        """
        if not isinstance(schema, dict):
            return False
        if "enum" in schema or "const" in schema:
            return False
        if any(
            TestNothingExecutableCrossesTheWire.free_text(part) for part in schema.get("anyOf", [])
        ):
            return True
        if schema.get("type") == "string":
            return True
        items = schema.get("items")
        return TestNothingExecutableCrossesTheWire.free_text(items) if items is not None else False

    def test_no_free_text_field_can_be_fetched_embedded_or_executed(self) -> None:
        # `prompt` is the option group's question to the *user*, pre-dating this
        # work and rendered as text; it is not an instruction channel.
        offenders = {
            name
            for name, schema in self.properties(plan_input_schema())
            if self.free_text(schema)
            and name != "prompt"
            and any(word in name.lower() for word in self.FORBIDDEN)
        }
        assert offenders == set(), f"a fetchable/executable-looking field appeared: {offenders}"

    def test_the_scene_graph_carries_no_geometry(self) -> None:
        """Coordinates are ours. A model that could place a node could place it
        off-canvas, over another, or in a shape the payload does not describe."""
        names = {name for name, _ in self.properties(plan_input_schema())}
        assert not ({"x_px", "y_px", "width", "height", "cx", "cy", "transform"} & names)

    def test_the_schema_is_json_and_finite(self) -> None:
        """``plan_input_schema`` inlines every ``$ref``; a recursive scene graph
        would not terminate, and a dangling ref would ship an empty tool."""
        text = json.dumps(plan_input_schema())
        assert "$ref" not in text
        assert "$defs" not in text
