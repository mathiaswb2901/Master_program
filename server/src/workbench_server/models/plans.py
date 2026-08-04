"""Visual plan artifacts: the typed payload behind the ``present_plan`` MCP tool.

When an agent proposes multi-step work or alternatives it calls ``present_plan``
with a :class:`PlanArtifact` instead of streaming a wall of markdown; Workbench
renders it as a native, clickable card and hands the user's decision back as a
:class:`PlanResponse`. The schema is deliberately closed — a discriminated union
of four node kinds with hard size caps — so the model chooses *content* and our
own React components own the *rendering*. Free-form HTML from a model is not a
product primitive; this is.

Sizes are capped here rather than trusted: an oversized or malformed plan comes
back to the agent as a validation error it can fix, not as a broken card.
"""

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

# One card must stay readable in a chat column; these are product limits, not
# implementation limits. Exceeding them is an agent error worth reporting.
MAX_NODES = 15
MAX_OPTIONS = 6
MAX_STEPS = 20
MAX_FILE_REFS = 8
MAX_PROS_CONS = 6

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


PlanNode = Annotated[
    OptionGroupNode | StepListNode | QuestionNode | MarkdownNode,
    Field(discriminator="kind"),
]


class PlanArtifact(BaseModel):
    """A whole plan card.

    ``plan_id`` is minted here and never accepted from the agent: the tool body
    strips the key before validating (``handle_present_plan``) because a default
    factory alone would happily keep an agent-supplied value. That matters — the
    tool result the agent reads contains ``plan_id``, and an agent that echoes it
    back on the re-present after a ``revise`` verdict would produce a card the UI
    dedupes away, leaving the user nothing to answer.
    """

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


# ---- the user's answer -------------------------------------------------------

# "no_decision" is never an implied approval: it is what a timeout or an
# interrupt resolves to, and the agent is told to stop and ask in chat.
PlanVerdict = Literal["approve", "revise", "reject", "no_decision"]


class PlanAnnotation(BaseModel):
    """Free text the user attached to one node (also how questions are answered)."""

    node_id: NodeId
    text: str = Field(min_length=1, max_length=600)


class PlanResponse(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    verdict: PlanVerdict
    #: node_id -> option_id, one entry per option group the user resolved.
    choices: dict[str, str] = Field(default_factory=dict, max_length=MAX_NODES)
    annotations: list[PlanAnnotation] = Field(default_factory=list, max_length=MAX_NODES)
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


def plan_input_schema() -> dict[str, Any]:
    """JSON Schema the agent sees for ``present_plan``: PlanArtifact minus the
    server-minted ``plan_id``, with all definitions inlined."""
    schema = PlanArtifact.model_json_schema()
    defs = schema.pop("$defs", {})
    inlined = _flatten(schema, defs)
    if not isinstance(inlined, dict):  # pragma: no cover - model_json_schema returns a dict
        raise TypeError("plan schema is not an object")
    properties = inlined.get("properties")
    if isinstance(properties, dict):
        properties.pop("plan_id", None)
    return inlined
