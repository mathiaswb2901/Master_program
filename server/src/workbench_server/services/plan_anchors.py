"""Resolving an annotation anchor against the artifact it claims to point into.

``models/plans.py`` validates an anchor's *grammar* — pairs, known keys, a
non-negative index where an index belongs. That is everything that can be
checked without the plan. This module checks the other half: that the node
exists, that it is the kind of node the anchor implies, that row 14 is a row
this table has, that ``"Price"`` is a column this table declares, that a text
range is inside the string it slices.

**Why this is validation and not a lookup that shrugs.** An anchor is data the
agent will act on: "the price in row 14 is wrong" is only meaningful if row 14
is a row. A resolver that skipped what it could not find would turn a malformed
response into a note about nothing — dropped silently in the best case, and in
the worst case read by the agent as pointing at whatever *is* at that position.
So an unresolvable anchor makes the whole decision malformed, and
``AgentSession.resolve_plan`` refuses it rather than passing part of it on.

Pure functions over the models — no I/O, no session state — so every branch is a
unit test (``server/tests/test_plan_anchors.py``).
"""

from collections.abc import Sequence

from workbench_server.models.plans import (
    AnchorSegment,
    AnnotationAnchor,
    MarkdownNode,
    PlanAnnotation,
    PlanArtifact,
    PlanNode,
    QuestionNode,
    VisualNode,
)
from workbench_server.models.visuals import (
    ChartLeaf,
    CodeDiffLeaf,
    DiagramLeaf,
    MetricsLeaf,
    TableLeaf,
    VisualLeaf,
    diff_lines,
)

#: The two node kinds carrying a plain string a range can slice. A range into an
#: ``option_group`` would have to name *which* string; a card that needs that
#: wants a part anchor on a visual, not a subtler range.
_TEXT_NODES = (MarkdownNode, QuestionNode)

#: What one path resolves to: the segments, keyed. The grammar validator has
#: already guaranteed string keys, pairs, and no key twice.
Segments = dict[str, AnchorSegment]

#: The segment keys each leaf kind is addressed by — every key its own validator
#: below actually reads. Which of them are *required* is that validator's
#: business; this map answers the other question, and the grammar cannot: which
#: are meaningless here.
#:
#: A validator reads the keys it knows and ignores the rest, so ``series "SE3"
#: row 999`` on a chart used to resolve clean with the ``row`` dropped on the
#: floor. That is the exact failure anchors exist to prevent: the note then
#: attaches to what the *understood* half names, which is not what anybody
#: pointed at. Mixing two leaves' keys is a client (or an agent SDK) that has
#: confused two leaves, and the half we understand is not the half to trust —
#: so the anchor is malformed, and the decision carrying it is refused.
_LEAF_KEYS: dict[str, frozenset[str]] = {
    "table": frozenset({"row", "col"}),
    "chart": frozenset({"series", "point"}),
    "diagram": frozenset({"node", "edge"}),
    "code_diff": frozenset({"side", "line"}),
    "metrics": frozenset({"metric"}),
}


def _pairs(path: Sequence[AnchorSegment]) -> Segments:
    return {str(key): value for key, value in zip(path[::2], path[1::2], strict=True)}


def _index(at: Segments, key: str) -> int | None:
    """An index segment, or ``None`` when the key is absent. The grammar has
    already rejected a non-index value under an index key."""
    value = at.get(key)
    return value if isinstance(value, int) else None


def _named(labels: Sequence[str], value: AnchorSegment, what: str) -> str | None:
    """Why this label-or-index segment does not name one of ``labels``.

    A label must match exactly one entry: two columns called "Price" make
    "Price" an ambiguous address, and guessing which one the user meant is how a
    note lands on the wrong number.
    """
    if isinstance(value, int):
        if not 0 <= value < len(labels):
            return f"{what} {value} is past the last one ({len(labels)})"
        return None
    matches = sum(label == value for label in labels)
    if matches == 0:
        return f"no {what} called {value!r}"
    if matches > 1:
        return f"{matches} entries are called {value!r} — an ambiguous {what}"
    return None


def _resolved(labels: Sequence[str], value: AnchorSegment) -> int:
    """Where a label-or-index segment points. Only called once :func:`_named`
    has said it resolves, so the label is present exactly once."""
    if isinstance(value, int):
        return value
    return [*labels].index(value)


def _leaves(node: VisualNode) -> list[VisualLeaf]:
    """A visual node's leaves, flat over its blocks — the order a ``leaf``
    segment counts in. Blocks are how the scene is *laid out*; the leaves are
    what it contains, and a note points at content."""
    return [leaf for block in node.blocks for leaf in block.items]


def _table_problem(leaf: TableLeaf, at: Segments) -> str | None:
    row = _index(at, "row")
    if row is None:
        return "a table part is a row (optionally a column)"
    if row >= len(leaf.rows):
        return f"row {row} is past the last row ({len(leaf.rows)})"
    if "col" not in at:
        return None
    return _named([column.label for column in leaf.columns], at["col"], "column")


def _chart_problem(leaf: ChartLeaf, at: Segments) -> str | None:
    if "series" not in at:
        return "a chart part is a series (optionally a point)"
    labels = [series.label for series in leaf.series]
    problem = _named(labels, at["series"], "series")
    if problem is not None:
        return problem
    point = _index(at, "point")
    if point is None:
        return None
    values = leaf.series[_resolved(labels, at["series"])].values
    if point >= len(values):
        return f"point {point} is past the last one ({len(values)})"
    return None


def _diagram_problem(leaf: DiagramLeaf, at: Segments) -> str | None:
    # Both keys address a diagram, so the key-set check above lets them through
    # together; only here is it visible that they name two different things.
    if "node" in at and "edge" in at:
        return "a diagram part is a node or an edge, not both"
    if "node" in at:
        if at["node"] not in {node.id for node in leaf.nodes}:
            return f"no diagram node {at['node']!r}"
        return None
    edge = _index(at, "edge")
    if edge is None:
        return "a diagram part is a node or an edge"
    if edge >= len(leaf.edges):
        return f"edge {edge} is past the last one ({len(leaf.edges)})"
    return None


def _diff_problem(leaf: CodeDiffLeaf, at: Segments) -> str | None:
    side = at.get("side")
    if side not in ("before", "after"):
        return "a code_diff part is side 'before' or 'after' (optionally a line)"
    body = leaf.before if side == "before" else leaf.after
    line = _index(at, "line")
    if line is None:
        return None
    count = diff_lines(body)
    if line >= count:
        return f"{side} line {line} is past the last one ({count})"
    return None


def _metrics_problem(leaf: MetricsLeaf, at: Segments) -> str | None:
    if "metric" not in at:
        return "a metrics part is a metric"
    return _named([item.label for item in leaf.items], at["metric"], "metric")


def _part_problem(node: PlanNode, path: Sequence[AnchorSegment]) -> str | None:
    if not isinstance(node, VisualNode):
        return f"node {node.node_id!r} is a {node.kind}, and only a visual has parts"
    at = _pairs(path)
    index = _index(at, "leaf")
    leaves = _leaves(node)
    if index is None or index >= len(leaves):
        return f"leaf {at.get('leaf')!r} is not one of this node's {len(leaves)} leaves"
    leaf = leaves[index]
    # Before asking whether row 14 exists, ask whether "row" means anything on
    # this leaf at all — a segment nobody reads is a segment nobody validates.
    unexpected = sorted(set(at) - _LEAF_KEYS[leaf.kind] - {"leaf"})
    if unexpected:
        named = ", ".join(repr(key) for key in unexpected)
        return f"{named} does not address a {leaf.kind} part"
    match leaf:
        case TableLeaf():
            return _table_problem(leaf, at)
        case ChartLeaf():
            return _chart_problem(leaf, at)
        case DiagramLeaf():
            return _diagram_problem(leaf, at)
        case CodeDiffLeaf():
            return _diff_problem(leaf, at)
        case MetricsLeaf():
            return _metrics_problem(leaf, at)


def _range_problem(node: PlanNode, path: Sequence[AnchorSegment]) -> str | None:
    if not isinstance(node, _TEXT_NODES):
        return f"node {node.node_id!r} is a {node.kind}, and only text has ranges"
    end = _index(_pairs(path), "to")
    if end is None or end > len(node.text):
        return f"the range ends at {end}, past the node's {len(node.text)} characters"
    return None


def anchor_problem(artifact: PlanArtifact, anchor: AnnotationAnchor) -> str | None:
    """Why this anchor does not point into this artifact, or ``None``."""
    if anchor.kind == "plan":
        return None
    node = next((n for n in artifact.nodes if n.node_id == anchor.node_id), None)
    if node is None:
        return f"no node {anchor.node_id!r} in this plan"
    if anchor.kind == "node":
        return None
    if anchor.kind == "range":
        return _range_problem(node, anchor.path)
    return _part_problem(node, anchor.path)


def annotation_problems(artifact: PlanArtifact, annotations: Sequence[PlanAnnotation]) -> list[str]:
    """Every anchor in a response that does not point into the artifact.

    A list rather than a first failure, so a buggy client sees all of what it
    got wrong in one log line instead of one round trip per mistake.
    """
    return [
        f"annotation {index}: {problem}"
        for index, annotation in enumerate(annotations)
        if (problem := anchor_problem(artifact, annotation.anchor)) is not None
    ]
