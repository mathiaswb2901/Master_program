"""Anchors: the grammar, and resolving one against the artifact it points into.

Two layers, tested separately because they fail differently.

* :class:`AnnotationAnchor` validates the *shape* — a plan anchor names no node,
  a part path is (key, value) pairs starting at a leaf, a range is
  ``from``/``to`` with ``start < end``. A bad shape is a schema error at the
  wire boundary, and the client frame never reaches the session.
* ``services/plan_anchors`` validates the *target* — row 14 is a row this table
  has, ``"Price"`` is a column it declares. A bad target is a rejected decision.

The malformed cases matter more than the happy ones. An anchor exists so the
agent can act on "that number is wrong"; an anchor that resolves to nothing
would make that sentence about nothing, and one that resolves to the wrong thing
would make it a lie. Neither may be silent, so every case below asserts the
failure is *reported*, not swallowed.
"""

import pytest
from pydantic import ValidationError

from workbench_server.models.plans import (
    AnnotationAnchor,
    MarkdownNode,
    PlanAnnotation,
    PlanArtifact,
    PlanStep,
    QuestionNode,
    StepListNode,
    VisualNode,
    node_anchor,
)
from workbench_server.models.visuals import (
    ChartLeaf,
    ChartSeries,
    CodeDiffLeaf,
    DiagramEdge,
    DiagramLeaf,
    DiagramNode,
    Metric,
    MetricsLeaf,
    TableColumn,
    TableLeaf,
    ValueAxis,
    VisualBlock,
)
from workbench_server.services.plan_anchors import anchor_problem, annotation_problems

MARKDOWN_TEXT = "The 25-hour day needs a fold tag. Everything else is unchanged."


def artifact() -> PlanArtifact:
    """One card holding every anchorable shape: a visual whose five leaves cover
    every leaf kind, a markdown node to slice, a question, and a step list that
    has no parts at all (which is itself a case)."""
    return PlanArtifact(
        plan_id="p1",
        title="Åsen 2 — 25 October dispatch",
        nodes=[
            MarkdownNode(node_id="intro", text=MARKDOWN_TEXT),
            VisualNode(
                node_id="scene",
                blocks=[
                    VisualBlock(
                        items=[
                            MetricsLeaf(
                                title="Day totals",
                                items=[
                                    Metric(label="Revenue", value="18 420", unit="EUR"),
                                    Metric(label="Cycles", value="1.4"),
                                ],
                            )
                        ]
                    ),
                    VisualBlock(
                        layout="row",
                        items=[
                            ChartLeaf(
                                title="Price",
                                x=ValueAxis(),
                                series=[
                                    ChartSeries(label="SE3", values=[41.2, 38.7, 36.1]),
                                    ChartSeries(label="Åsen 2", values=[0.0, -5.0, 5.0]),
                                ],
                            ),
                            TableLeaf(
                                title="Hours",
                                columns=[
                                    TableColumn(label="Hour"),
                                    TableColumn(label="Price", type="numeric", unit="EUR/MWh"),
                                ],
                                rows=[["02:00 (1st)", "-3.8"], ["02:00 (2nd)", "-6.1"]],
                            ),
                            DiagramLeaf(
                                title="Pipeline",
                                nodes=[
                                    DiagramNode(id="feed", label="TGN feed"),
                                    DiagramNode(id="opt", label="Optimizer"),
                                ],
                                edges=[DiagramEdge(source="feed", target="opt")],
                            ),
                            CodeDiffLeaf(
                                title="calendar.py",
                                before="hours = range(24)\nfor h in hours:\n",
                                after="hours = delivery_hours(day, tz)\nfor h in hours:\n",
                            ),
                        ],
                    ),
                ],
            ),
            QuestionNode(node_id="q1", text="Backfill 2025 too?"),
            StepListNode(node_id="steps", steps=[PlanStep(text="Add a fold-aware index")]),
        ],
    )


def part(path: list[str | int], node_id: str = "scene") -> AnnotationAnchor:
    return AnnotationAnchor(kind="part", node_id=node_id, path=path)


# ---- grammar (no artifact needed) --------------------------------------------


class TestGrammar:
    def test_the_default_anchor_is_a_whole_node(self) -> None:
        """Today's behaviour, unchanged: an annotation with nothing else said
        about it is about the node, which is still the fallback."""
        assert node_anchor("steps") == AnnotationAnchor(kind="node", node_id="steps")
        assert node_anchor("steps").path == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "plan", "node_id": "scene"},
            {"kind": "plan", "path": ["leaf", 0]},
            {"kind": "node", "node_id": "scene", "path": ["leaf", 0]},
            {"kind": "node", "node_id": ""},
            {"kind": "part", "node_id": ""},
            {"kind": "part", "node_id": "scene", "path": []},
            {"kind": "part", "node_id": "scene", "path": ["leaf"]},
            {"kind": "part", "node_id": "scene", "path": ["row", 0]},
            {"kind": "part", "node_id": "scene", "path": ["leaf", 0, "row"]},
            {"kind": "part", "node_id": "scene", "path": ["leaf", 0, "selector", ".wb-vis td"]},
            {"kind": "part", "node_id": "scene", "path": ["leaf", 0, "row", "fourteen"]},
            {"kind": "part", "node_id": "scene", "path": ["leaf", 0, "node", 3]},
            {"kind": "part", "node_id": "scene", "path": ["leaf", -1]},
            {"kind": "part", "node_id": "scene", "path": ["leaf", 0, "row", 1, "row", 2]},
            {"kind": "range", "node_id": "intro", "path": ["from", 5]},
            {"kind": "range", "node_id": "intro", "path": ["from", 5, "to", 5]},
            {"kind": "range", "node_id": "intro", "path": ["from", 9, "to", 4]},
            {"kind": "range", "node_id": "intro", "path": ["from", -1, "to", 4]},
        ],
        ids=[
            "plan-names-a-node",
            "plan-carries-a-path",
            "node-carries-a-path",
            "node-without-an-id",
            "part-without-an-id",
            "part-without-a-path",
            "odd-number-of-segments",
            "path-not-starting-at-a-leaf",
            "key-without-a-value",
            "a-selector-is-not-a-key",
            "index-key-given-a-word",
            "name-key-given-an-index",
            "negative-index",
            "same-key-twice",
            "range-missing-its-end",
            "empty-range",
            "backwards-range",
            "negative-range",
        ],
    )
    def test_a_malformed_shape_is_a_schema_error(self, payload: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            AnnotationAnchor.model_validate(payload)

    def test_a_selector_cannot_be_smuggled_in_as_a_value(self) -> None:
        """The rejection that matters most. A CSS selector is a string, and a
        string is a legal *value* — so what keeps one out is that the vocabulary
        of keys is closed, and a value is only ever read as a label to look up
        in the payload. This asserts the closed vocabulary directly."""
        with pytest.raises(ValidationError):
            AnnotationAnchor.model_validate(
                {"kind": "part", "node_id": "scene", "path": ["css", "td:nth-child(2)"]}
            )
        # …and a label-shaped value is still only resolved against the payload:
        against = artifact()
        smuggled = part(["leaf", 2, "row", 0, "col", "td:nth-child(2)"])
        assert anchor_problem(against, smuggled) == "no column called 'td:nth-child(2)'"

    def test_the_path_is_capped(self) -> None:
        with pytest.raises(ValidationError):
            AnnotationAnchor.model_validate(
                {
                    "kind": "part",
                    "node_id": "scene",
                    "path": ["leaf", 0, "row", 1, "col", 1, "point", 1, "line", 1],
                }
            )


# ---- resolution against the artifact -----------------------------------------


class TestEveryKindResolves:
    def test_the_plan_as_a_whole(self) -> None:
        assert anchor_problem(artifact(), AnnotationAnchor(kind="plan")) is None

    def test_a_whole_node(self) -> None:
        for node_id in ("intro", "scene", "q1", "steps"):
            assert anchor_problem(artifact(), node_anchor(node_id)) is None

    @pytest.mark.parametrize(
        "path",
        [
            ["leaf", 0, "metric", "Revenue"],
            ["leaf", 0, "metric", 1],
            ["leaf", 1, "series", "SE3"],
            ["leaf", 1, "series", "SE3", "point", 2],
            ["leaf", 1, "series", 1, "point", 0],
            ["leaf", 2, "row", 1],
            ["leaf", 2, "row", 1, "col", "Price"],
            ["leaf", 2, "row", 0, "col", 0],
            ["leaf", 3, "node", "opt"],
            ["leaf", 3, "edge", 0],
            ["leaf", 4, "side", "before"],
            ["leaf", 4, "side", "after", "line", 1],
        ],
        ids=[
            "metric-by-label",
            "metric-by-index",
            "chart-series",
            "chart-point",
            "chart-point-by-series-index",
            "table-row",
            "table-cell-by-column-label",
            "table-cell-by-column-index",
            "diagram-node",
            "diagram-edge",
            "diff-side",
            "diff-line",
        ],
    )
    def test_a_part_of_a_leaf(self, path: list[str | int]) -> None:
        assert anchor_problem(artifact(), part(path)) is None

    def test_a_text_range_inside_a_markdown_node(self) -> None:
        anchor = AnnotationAnchor(kind="range", node_id="intro", path=["from", 4, "to", 16])
        assert anchor_problem(artifact(), anchor) is None
        # The slice those offsets name, so the numbers are the ones a reader
        # would get rather than merely in range.
        assert MARKDOWN_TEXT[4:16] == "25-hour day "

    def test_a_range_over_a_question(self) -> None:
        anchor = AnnotationAnchor(kind="range", node_id="q1", path=["from", 0, "to", 8])
        assert anchor_problem(artifact(), anchor) is None


class TestUnresolvableAnchors:
    """One test per way an anchor can name something that is not there. Every
    one returns a *reason* — the caller refuses the decision and logs it."""

    def test_a_node_that_is_not_in_the_artifact(self) -> None:
        assert anchor_problem(artifact(), node_anchor("ghost")) == "no node 'ghost' in this plan"

    def test_a_row_index_past_the_last_row(self) -> None:
        problem = anchor_problem(artifact(), part(["leaf", 2, "row", 14]))
        assert problem == "row 14 is past the last row (2)"

    def test_a_column_name_not_in_the_schema(self) -> None:
        problem = anchor_problem(artifact(), part(["leaf", 2, "row", 0, "col", "price_eur_mwh"]))
        assert problem == "no column called 'price_eur_mwh'"

    def test_a_column_index_past_the_last_column(self) -> None:
        assert anchor_problem(artifact(), part(["leaf", 2, "row", 0, "col", 9])) is not None

    def test_a_leaf_index_past_the_last_leaf(self) -> None:
        problem = anchor_problem(artifact(), part(["leaf", 9, "row", 0]))
        assert problem == "leaf 9 is not one of this node's 5 leaves"

    def test_a_series_that_is_not_in_the_chart(self) -> None:
        problem = anchor_problem(artifact(), part(["leaf", 1, "series", "SE4"]))
        assert problem == "no series called 'SE4'"

    def test_a_point_past_the_end_of_its_series(self) -> None:
        problem = anchor_problem(artifact(), part(["leaf", 1, "series", "SE3", "point", 99]))
        assert problem == "point 99 is past the last one (3)"

    def test_a_diagram_node_id_that_does_not_exist(self) -> None:
        assert anchor_problem(artifact(), part(["leaf", 3, "node", "bid"])) is not None

    def test_an_edge_index_past_the_last_edge(self) -> None:
        assert anchor_problem(artifact(), part(["leaf", 3, "edge", 7])) is not None

    def test_a_diff_line_past_the_end_of_its_side(self) -> None:
        assert (
            anchor_problem(artifact(), part(["leaf", 4, "side", "after", "line", 40])) is not None
        )

    def test_a_diff_side_that_is_not_a_side(self) -> None:
        assert anchor_problem(artifact(), part(["leaf", 4, "side", "middle"])) is not None

    def test_a_text_range_outside_the_string(self) -> None:
        anchor = AnnotationAnchor(
            kind="range", node_id="intro", path=["from", 0, "to", len(MARKDOWN_TEXT) + 1]
        )
        problem = anchor_problem(artifact(), anchor)
        assert problem == (
            f"the range ends at {len(MARKDOWN_TEXT) + 1}, "
            f"past the node's {len(MARKDOWN_TEXT)} characters"
        )

    def test_a_range_over_a_node_with_no_text(self) -> None:
        anchor = AnnotationAnchor(kind="range", node_id="scene", path=["from", 0, "to", 4])
        assert anchor_problem(artifact(), anchor) is not None

    def test_a_part_of_a_node_that_draws_nothing(self) -> None:
        """A step list has no leaves, so it has no parts — pointing into one is
        a category error, not an out-of-range index."""
        problem = anchor_problem(artifact(), part(["leaf", 0, "row", 0], node_id="steps"))
        assert problem == "node 'steps' is a step_list, and only a visual has parts"

    def test_the_wrong_kind_of_part_for_the_leaf(self) -> None:
        """A row on a chart. Every segment is grammatical and the leaf exists;
        it is the *pairing* that is wrong, which only the artifact can see."""
        assert anchor_problem(artifact(), part(["leaf", 1, "row", 0])) is not None

    def test_an_ambiguous_column_label_is_refused_rather_than_guessed(self) -> None:
        """Two columns with one name make that name an address for two things.
        Resolving it to the first would put the note on the wrong number."""
        plan = artifact()
        table = plan.nodes[1]
        assert isinstance(table, VisualNode)
        leaf = table.blocks[1].items[1]
        assert isinstance(leaf, TableLeaf)
        leaf.columns[0].label = "Price"
        problem = anchor_problem(plan, part(["leaf", 2, "row", 0, "col", "Price"]))
        assert problem == "2 entries are called 'Price' — an ambiguous column"


class TestResponseLevel:
    def test_a_clean_response_has_no_problems(self) -> None:
        notes = [
            PlanAnnotation(anchor=node_anchor("steps"), text="add a rollback"),
            PlanAnnotation(anchor=part(["leaf", 2, "row", 1, "col", "Price"]), text="wrong fold"),
        ]
        assert annotation_problems(artifact(), notes) == []

    def test_every_bad_anchor_is_named_with_its_position(self) -> None:
        """All of them, not the first: a client that got two wrong should learn
        both in one round trip rather than one per fix."""
        notes = [
            PlanAnnotation(anchor=node_anchor("steps"), text="fine"),
            PlanAnnotation(anchor=part(["leaf", 2, "row", 14]), text="bad row"),
            PlanAnnotation(anchor=node_anchor("ghost"), text="bad node"),
        ]
        problems = annotation_problems(artifact(), notes)
        assert len(problems) == 2
        assert problems[0].startswith("annotation 1: row 14")
        assert problems[1].startswith("annotation 2: no node 'ghost'")
