"""Visual plan artifacts: the typed payload behind the ``present_plan`` MCP tool.

When an agent proposes multi-step work or alternatives it calls ``present_plan``
with a :class:`PlanArtifact` instead of streaming a wall of markdown; Workbench
renders it as a native, clickable card and hands the user's decision back as a
:class:`PlanResponse`. The schema is deliberately closed — a discriminated union
of five node kinds with hard size caps — so the model chooses *content* and our
own React components own the *rendering*. Free-form HTML from a model is not a
product primitive; this is.

The fifth kind, :class:`VisualNode`, extends that stance from prose to pictures:
its payload is a typed scene graph (``models/visuals.py``) of tables, charts,
diagrams, diffs and metrics from which Workbench computes every coordinate. The
model sends structure and numbers; not one byte of markup, CSS or geometry
crosses the wire.

Sizes are capped here rather than trusted: an oversized or malformed plan comes
back to the agent as a validation error it can fix, not as a broken card.
"""

import uuid
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from workbench_server.models.visuals import (
    MAX_VISUAL_BLOCKS,
    MAX_VISUAL_LEAVES,
    MAX_VISUAL_MARKS,
    VisualBlock,
    leaf_count,
    mark_count,
)

# One card must stay readable in a chat column; these are product limits, not
# implementation limits. Exceeding them is an agent error worth reporting.
MAX_NODES = 15
MAX_OPTIONS = 6
MAX_STEPS = 20
MAX_FILE_REFS = 8
MAX_PROS_CONS = 6
#: Visual nodes per card. A plan that wants five pictures wants a dashboard,
#: and a dashboard is a panel (PR 4), not a card in a 760px chat column.
MAX_VISUAL_NODES = 3

NodeId = Annotated[str, Field(min_length=1, max_length=64)]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class FileRef(BaseModel):
    """A file a step touches. Workspace-relative, forward slashes; rendered as a
    chip that opens the file in a real editor tab."""

    path: str = Field(min_length=1, max_length=400)


class PlanOption(BaseModel):
    """One alternative inside an option group."""

    option_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    pros: list[ShortText] = Field(default_factory=list, max_length=MAX_PROS_CONS)
    cons: list[ShortText] = Field(default_factory=list, max_length=MAX_PROS_CONS)
    recommended: bool = False


class OptionGroupNode(BaseModel):
    """A choice the user makes: two or more options, at most one recommended."""

    kind: Literal["option_group"] = "option_group"
    node_id: NodeId
    prompt: str = Field(min_length=1, max_length=300)
    options: list[PlanOption] = Field(min_length=2, max_length=MAX_OPTIONS)

    @field_validator("options")
    @classmethod
    def _check_options(cls, options: list[PlanOption]) -> list[PlanOption]:
        ids = [option.option_id for option in options]
        if len(set(ids)) != len(ids):
            raise ValueError("option_id must be unique within an option group")
        if sum(option.recommended for option in options) > 1:
            raise ValueError("at most one option may be recommended")
        return options


class PlanStep(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    file_refs: list[FileRef] = Field(default_factory=list, max_length=MAX_FILE_REFS)


class StepListNode(BaseModel):
    """An ordered list of concrete steps, each optionally naming files."""

    kind: Literal["step_list"] = "step_list"
    node_id: NodeId
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)


class QuestionNode(BaseModel):
    """Something the agent needs answered; the answer returns as an annotation."""

    kind: Literal["question"] = "question"
    node_id: NodeId
    text: str = Field(min_length=1, max_length=400)


class MarkdownNode(BaseModel):
    """Prose between the interactive parts — context, caveats, a heading."""

    kind: Literal["markdown"] = "markdown"
    node_id: NodeId
    text: str = Field(min_length=1, max_length=2000)


class VisualNode(BaseModel):
    """A drawn artifact: a short stack of blocks, each holding typed leaves.

    Depth stops here. Blocks hold leaves and leaves hold data, so the payload
    cannot nest — see ``models/visuals.py`` for why that is a safety property
    and a schema-budget property before it is a design one.
    """

    kind: Literal["visual"] = "visual"
    node_id: NodeId
    title: str = Field(default="", max_length=120)
    blocks: list[VisualBlock] = Field(min_length=1, max_length=MAX_VISUAL_BLOCKS)

    @field_validator("blocks")
    @classmethod
    def _check_leaf_budget(cls, blocks: list[VisualBlock]) -> list[VisualBlock]:
        count = leaf_count(blocks)
        if count > MAX_VISUAL_LEAVES:
            raise ValueError(f"{count} leaves in one visual, at most {MAX_VISUAL_LEAVES}")
        return blocks

    # Counting leaves is not counting work: eight leaves, each inside its own
    # cap, is 19,200 chart points, and this card may hold three of them. The
    # per-leaf caps bound one picture; this bounds the scene.
    @field_validator("blocks")
    @classmethod
    def _check_render_budget(cls, blocks: list[VisualBlock]) -> list[VisualBlock]:
        marks = mark_count(blocks)
        if marks > MAX_VISUAL_MARKS:
            raise ValueError(
                f"{marks} drawn marks in one visual, at most {MAX_VISUAL_MARKS} "
                "(a chart point, a table cell, a diagram node or edge, a diff "
                "line, a metric) — send fewer points, rows or leaves"
            )
        return blocks


PlanNode = Annotated[
    OptionGroupNode | StepListNode | QuestionNode | MarkdownNode | VisualNode,
    Field(discriminator="kind"),
]


# ``plan_id`` is minted here and never accepted from the agent: the tool body
# strips the key before validating (``handle_present_plan``) because a default
# factory alone would happily keep an agent-supplied value. That matters — the
# tool result the agent reads contains ``plan_id``, and an agent that echoes it
# back on the re-present after a ``revise`` verdict would produce a card the UI
# dedupes away, leaving the user nothing to answer.
#
# A comment rather than a docstring on purpose: a class docstring becomes the
# schema's ``description`` and travels to the model on every request, and this
# paragraph is about our own internals, which the model can do nothing with.
class PlanArtifact(BaseModel):
    """A whole plan card."""

    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], max_length=64)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=600)
    nodes: list[PlanNode] = Field(min_length=1, max_length=MAX_NODES)

    @field_validator("nodes")
    @classmethod
    def _unique_node_ids(cls, nodes: list[PlanNode]) -> list[PlanNode]:
        ids = [node.node_id for node in nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("node_id must be unique within a plan")
        return nodes

    @field_validator("nodes")
    @classmethod
    def _visual_budget(cls, nodes: list[PlanNode]) -> list[PlanNode]:
        visuals = sum(isinstance(node, VisualNode) for node in nodes)
        if visuals > MAX_VISUAL_NODES:
            raise ValueError(f"{visuals} visual nodes in one plan, at most {MAX_VISUAL_NODES}")
        return nodes


# ---- the user's answer -------------------------------------------------------

# "no_decision" is never an implied approval: it is what a timeout or an
# interrupt resolves to, and the agent is told to stop and ask in chat.
PlanVerdict = Literal["approve", "revise", "reject", "no_decision"]


# ---- anchors -----------------------------------------------------------------

# **An anchor is a semantic path the renderer emits, never a CSS selector.**
#
# That is the whole design decision, and it is worth stating as a rejection: the
# obvious way to let a user point at part of a picture is to record what they
# clicked — an element id, a selector, a bounding box. All three are wrong here.
# A selector is *our stylesheet* leaking into the agent's input: it breaks the
# next time a class is renamed, it means nothing to a model reading it, and it
# would let anything that can influence markup address a part of the artifact it
# was never given. A path like ``["leaf", 2, "row", 14, "col", "Price"]`` is the
# opposite on all three counts: it survives a re-render and a restyle because it
# names *data*, the agent can act on it directly (it is the agent's own payload,
# addressed in the agent's own vocabulary), and every segment is validated
# against the artifact before it travels — an anchor cannot name a row that does
# not exist, so it cannot be used to say something the artifact does not contain.
#
# Positional where the payload is ordered (rows, points, edges, leaves), named
# where the payload carries a name (a column label, a series label, a diagram
# node id) — see ``services/plan_anchors.py``, which resolves both.

#: One segment: a key we chose, or an index into the payload's own ordering / a
#: name the payload itself carries.
AnchorSegment = int | Annotated[str, Field(min_length=1, max_length=64)]

#: Longest legal path. ``leaf n series "x" point i`` is six segments and nothing
#: in the scene graph nests deeper, so eight is headroom, not a shape.
MAX_ANCHOR_PATH = 8

#: Which leaf of a ``visual`` node, counted flat over its blocks. Every ``part``
#: path starts here, because a part is always part of a drawn leaf.
_LEAF_KEY = "leaf"
#: Keys whose value indexes the payload's own ordering.
_INDEX_KEYS = frozenset({_LEAF_KEY, "row", "point", "edge", "line"})
#: Keys whose value is a name the payload carries.
_NAME_KEYS = frozenset({"node", "side"})
#: Keys taking either — a label when the payload gives a usable one, else the
#: index. The renderer decides which (``ui/src/plan/anchors.ts``).
_EITHER_KEYS = frozenset({"col", "series", "metric"})
_PART_KEYS = _INDEX_KEYS | _NAME_KEYS | _EITHER_KEYS


def _is_index(value: AnchorSegment) -> bool:
    """A non-negative int. ``bool`` is an ``int`` subclass and is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class AnnotationAnchor(BaseModel):
    """What a note points at: the plan, a node, a part of a drawn leaf, or a
    text range. `path` is a semantic path into the artifact, never a selector."""

    kind: Literal["plan", "node", "part", "range"] = "node"
    #: Empty only for ``kind="plan"``.
    node_id: str = Field(default="", max_length=64)
    #: ``(key, value)`` pairs: ``["leaf", 2, "row", 14, "col", "Price"]``.
    path: list[AnchorSegment] = Field(default_factory=list, max_length=MAX_ANCHOR_PATH)

    # Grammar only. Whether row 14 *exists* needs the artifact, and lives in
    # ``services/plan_anchors.py`` — so a malformed shape is a schema error and
    # a wrong target is a rejected decision, which are different failures.
    @model_validator(mode="after")
    def _check_grammar(self) -> Self:
        if self.kind == "plan":
            if self.node_id or self.path:
                raise ValueError("a plan anchor names no node and carries no path")
            return self
        if not self.node_id:
            raise ValueError(f"a {self.kind} anchor needs a node_id")
        if self.kind == "node":
            if self.path:
                raise ValueError("a node anchor carries no path")
            return self
        if self.kind == "range":
            match self.path:
                case ["from", int() as start, "to", int() as end] if (
                    _is_index(start) and _is_index(end) and start < end
                ):
                    return self
            raise ValueError('a range anchor is ["from", start, "to", end] with 0 <= start < end')
        return self._check_part_path()

    def _check_part_path(self) -> Self:
        if not self.path:
            raise ValueError("a part anchor needs a path")
        if len(self.path) % 2:
            raise ValueError("a part path is (key, value) pairs")
        if self.path[0] != _LEAF_KEY:
            raise ValueError(f"a part path starts with {_LEAF_KEY!r} — a part is part of a leaf")
        seen: set[str] = set()
        for key, value in zip(self.path[::2], self.path[1::2], strict=True):
            if not isinstance(key, str) or key not in _PART_KEYS:
                raise ValueError(f"unknown path key {key!r}")
            if key in seen:
                raise ValueError(f"path key {key!r} appears twice")
            seen.add(key)
            if key in _INDEX_KEYS and not _is_index(value):
                raise ValueError(f"{key} takes a non-negative index, got {value!r}")
            if key in _NAME_KEYS and not isinstance(value, str):
                raise ValueError(f"{key} takes a name, got {value!r}")
            if key in _EITHER_KEYS and not (isinstance(value, str) or _is_index(value)):
                raise ValueError(f"{key} takes a label or an index, got {value!r}")
        return self


def node_anchor(node_id: str) -> AnnotationAnchor:
    """The whole-node anchor — what every annotation was before parts existed,
    and still the fallback when a user annotates a node rather than a part."""
    return AnnotationAnchor(kind="node", node_id=node_id)


class PlanAnnotation(BaseModel):
    """A note the user attached to one part of the plan (also how questions are
    answered). `anchor` says which part; whole-node is the fallback."""

    anchor: AnnotationAnchor
    text: str = Field(min_length=1, max_length=600)


#: Notes on one card. A plan holds at most 15 nodes, but a *part* anchor makes
#: one node worth marking up several times over — a table with four wrong cells
#: is one node and four notes. 40 is what a user can plausibly write on one card
#: before deciding, and it keeps the response the agent reads bounded.
MAX_ANNOTATIONS = 40


class PlanResponse(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    verdict: PlanVerdict
    #: node_id -> option_id, one entry per option group the user resolved.
    choices: dict[str, str] = Field(default_factory=dict, max_length=MAX_NODES)
    annotations: list[PlanAnnotation] = Field(default_factory=list, max_length=MAX_ANNOTATIONS)
    comment: str = Field(default="", max_length=2000)


# ---- /ws/agent/{id} frames ---------------------------------------------------


class PlanPresented(BaseModel):
    """server -> client: render this plan card (also replayed to late clients)."""

    type: Literal["plan_presented"] = "plan_presented"
    plan: PlanArtifact


class PlanResolved(BaseModel):
    """server -> client: the pending plan is settled and the card is now history.

    Emitted for every settlement path — a decision, the timeout, an interrupt —
    and broadcast to *all* subscribers, so no client can keep showing a live,
    answerable card (or, worse, flip it to "Approved") for a plan the agent has
    already stopped waiting on. This frame, not the optimistic click, is what
    makes a card read-only.
    """

    type: Literal["plan_resolved"] = "plan_resolved"
    plan_id: str = Field(min_length=1, max_length=64)
    verdict: PlanVerdict


class PlanDecision(BaseModel):
    """client -> server: the user answered the pending plan."""

    type: Literal["plan_decision"] = "plan_decision"
    response: PlanResponse


# ---- MCP tool input schema ---------------------------------------------------


def _flatten(node: Any, defs: dict[str, Any]) -> Any:
    """Replace every ``$ref`` with the definition it points at, and drop the
    ``discriminator`` keyword whose mapping would then dangle.

    Tool schemas travel verbatim to the model; ``$defs``/``$ref`` round-trips
    through the CLI are not guaranteed, and this schema has no recursion, so
    inlining is both safe and cheaper than debugging a silently-empty tool. The
    ``oneOf`` branches keep their ``kind`` const, so nothing is lost.

    Inlining is what makes the *shape* of the scene graph a budget question and
    not only a design one: every extra place a leaf union appears is another
    full copy of five leaf schemas in every session's context (``visuals.py``,
    ``VisualBlock``). Non-recursive is also what makes this terminate.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = _flatten(defs[ref.rsplit("/", 1)[1]], defs)
            return {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return {key: _flatten(value, defs) for key, value in node.items() if key != "discriminator"}
    if isinstance(node, list):
        return [_flatten(item, defs) for item in node]
    return node


#: Defaults that restate what ``required`` already says. A field absent from
#: ``required`` is optional; that it falls back to the empty string, the empty
#: list or null adds nothing a model can act on. A *meaningful* default
#: (``"linear"``, ``60``, ``"single"``) is kept — that one it does act on.
_EMPTY_DEFAULTS: tuple[Any, ...] = ("", [], {}, None)


def _is_noise(key: str, value: Any, node: dict[str, Any]) -> bool:
    """Whether a schema keyword is pure cost.

    ``title`` restates the property name it hangs off ("Node Id" for
    ``node_id``); ``type`` and ``default`` beside a ``const`` restate the const;
    an empty ``default`` restates optionality. Every one of them is paid for on
    every request of every session (CLAUDE.md's tool budget), and none of them
    changes what the model can send.
    """
    if key == "title":
        return True
    if key in ("type", "default") and "const" in node:
        return True
    if key == "default":
        return any(value is empty or value == empty for empty in _EMPTY_DEFAULTS) or isinstance(
            value, dict
        )
    return False


def _denoise(node: Any, in_names: bool = False) -> Any:
    """Strip those keywords — but only where they are *keywords*.

    ``in_names`` marks the one place a dict's keys are field names rather than
    schema keywords (the body of ``properties``), because the scene graph has
    leaves with a literal ``title`` field and dropping that would delete a
    caption from the contract instead of a label from the schema.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if in_names:
                out[key] = _denoise(value)
                continue
            if _is_noise(key, value, node):
                continue
            out[key] = _denoise(value, in_names=key == "properties")
        return out
    if isinstance(node, list):
        return [_denoise(item) for item in node]
    return node


def plan_input_schema() -> dict[str, Any]:
    """JSON Schema the agent sees for ``present_plan``: PlanArtifact minus the
    server-minted ``plan_id``, with all definitions inlined and the keywords
    that only restate the property name stripped."""
    schema = PlanArtifact.model_json_schema()
    defs = schema.pop("$defs", {})
    inlined = _denoise(_flatten(schema, defs))
    if not isinstance(inlined, dict):  # pragma: no cover - model_json_schema returns a dict
        raise TypeError("plan schema is not an object")
    properties = inlined.get("properties")
    if isinstance(properties, dict):
        properties.pop("plan_id", None)
    return inlined
