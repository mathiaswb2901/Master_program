"""The agent-facing tool registry, and the ergonomics budget it carries.

Every tool a Workbench session puts in an agent's context is declared here once:
the name, the description the *model* reads, the input schema, and — required,
so ``mypy --strict`` fails an omission rather than letting a tool ship
unmeasured — the format its result comes back in. :mod:`sdk_factory` builds the
MCP server from these specs and derives the session's allow-list from them, so a
tool is added in one place and bounded in one place.

This is the *only* place an agent-facing tool is declared. ``ui/src/registry.ts``
is the registry for what a capability contributes to the *window* — its panel,
commands and status items — and deliberately says nothing about the tools that
capability gives an agent: a second copy of the model-facing text, on the wire
or in a descriptor, would be a second authority to keep honest with nothing
reading it, and the first edit that touched one and not the other would go
unnoticed.

The budget is enforced by tests, never by a comment. Every description *and
every input schema* is loaded into every session's context, so both are a cost
paid on every request (CLAUDE.md); ``server/tests/test_agent_tools.py`` asserts
a ceiling on each description, on each input schema and on the serialized size
of a representative result, and the quality gate fails bloat. Latency is
deliberately not budgeted — these are in-process calls where the model and the
user dominate.

The schema ceiling is the one that bites hardest and is the least visible: a
result is paid for when a tool is called, a description once per session, but a
schema is paid for on every request whether or not the tool is ever used. It is
what made the scene graph's shape (``models/visuals.py``) a budget decision
before a design one.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from workbench_server.models.agents import UiState
from workbench_server.models.plans import PlanArtifact, plan_input_schema
from workbench_server.services.agent_sessions import PlanAlreadyPendingError, SessionBridge

#: How a tool's result reaches the model. ``compact-json`` means no indent and
#: no pretty separators. Measured on the representative payload the tests use
#: (``representative_ui_state`` — six open files, two dirty):
#: ``model_dump_json()`` is 312 bytes against 371 with ``indent=2``, ~16% fewer,
#: and the gap widens with nesting depth since every level adds its own
#: indentation. It is a small win taken on every call of every session.
OutputFormat = Literal["compact-json", "text", "markdown"]

#: Ceiling on the model-facing description, shared: the cost is the same
#: whatever the tool does. It binds — ``present_plan`` spends 708 of it.
MAX_DESCRIPTION_CHARS = 800

MCP_SERVER_NAME = "workbench"


@dataclass(frozen=True)
class AgentToolSpec:
    """One agent-facing tool, as data."""

    name: str
    description: str
    output_format: OutputFormat
    #: Ceiling on this tool's serialized result, in bytes. Per tool, not global:
    #: a shared number large enough for the chattiest tool cannot fail for any
    #: other, which is how a budget becomes decoration. Size it from the
    #: measured representative payload plus a margin you can say out loud (see
    #: the two below), so that growing the result is a diff someone justifies.
    max_result_bytes: int
    #: Ceiling on the compact JSON of ``input_schema``, in bytes. Also per tool,
    #: and required for the same reason: a tool that takes nothing must fail the
    #: gate the moment it starts taking something, and a tool with a rich schema
    #: must fail it the moment that schema grows without anyone noticing.
    max_schema_bytes: int
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def schema_bytes(self) -> int:
        """What this tool's schema actually costs, compactly serialized."""
        return len(json.dumps(self.input_schema, separators=(",", ":")).encode())

    @property
    def qualified_name(self) -> str:
        """What the SDK's permission rules and allow-lists call this tool."""
        return f"mcp__{MCP_SERVER_NAME}__{self.name}"


def text_result(text: str) -> dict[str, Any]:
    """An MCP tool result carrying one text block."""
    return {"content": [{"type": "text", "text": text}]}


def error_result(text: str) -> dict[str, Any]:
    """A tool error the agent reads and can fix, rather than an exception."""
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def clamp_result(text: str, limit: int) -> str:
    """Cut a result to its tool's byte budget rather than hoping it fits.

    For text an agent only reads (an error message), where the alternative to
    truncation is an unbounded wall of prose it is charged for. Never for a
    payload it parses — clamping valid JSON would hand it invalid JSON.
    """
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    return encoded[: max(limit - 3, 0)].decode(errors="ignore") + "…"


# ---- get_workspace_state ----------------------------------------------------

GET_WORKSPACE_STATE = AgentToolSpec(
    name="get_workspace_state",
    description=(
        "Current workbench UI state: the file the user is looking at, open tabs, "
        "and files with unsaved changes (do NOT edit those)."
    ),
    output_format="compact-json",
    # 312 bytes for the six-tab session in the tests, so 512 buys roughly four
    # more paths — a busy workspace, not a change of shape. Echoing anything
    # per-file (hashes, dirty flags, line counts) blows it, which is the point:
    # that is a redesign of what this tool costs, not an incremental edit.
    max_result_bytes=512,
    # A tool that takes nothing advertises nothing: `{}` is two bytes, and this
    # ceiling is what fails the gate if arguments ever appear here by accident.
    max_schema_bytes=8,
)


def workspace_state_result(state: UiState) -> dict[str, Any]:
    """The get_workspace_state body.

    Compact JSON, not indented: three short lists do not become more readable
    to a model for being pretty-printed, and every newline and run of spaces is
    tokens spent on every call.
    """
    return text_result(state.model_dump_json())


# ---- present_plan -----------------------------------------------------------

PRESENT_PLAN = AgentToolSpec(
    name="present_plan",
    description=(
        "Show the user an interactive plan card and wait for their decision. "
        "Use it instead of chat prose whenever you propose multi-step work "
        "or ask the user to choose between alternatives. Nodes render natively: "
        "option_group (user picks one), step_list (ordered steps; file_refs open "
        "editor tabs), question, markdown, visual (tables, charts, diagrams, diffs, "
        "metrics — we draw them from your numbers; read the workbench:plan-visual "
        "skill first). Returns JSON {plan_id, verdict, choices, annotations, comment}. "
        "verdict "
        "'approve' means proceed with the chosen options; 'revise' means rework the "
        "plan using their comments and present it again; 'reject' means drop this "
        "approach; 'no_decision' means the user never answered (timeout or "
        "interrupt) — stop and ask in chat, never treat it as approval."
    ),
    output_format="compact-json",
    # The envelope is 129 bytes for the representative approval; 512 covers a
    # verdict, the chosen options and a couple of sentences of the user's own
    # comment. What rides along past that is the user's typing, not ours to
    # budget — the ceiling is on the shape, and the one path we *can* overrun
    # (a validation error) is clamped to it in `handle_present_plan`.
    max_result_bytes=512,
    # 8,370 bytes measured for the five node kinds: 2,388 for the four original
    # ones and 5,981 for the scene graph (models/visuals.py). 9,500 leaves room
    # for a leaf kind's fields to grow but not for a sixth leaf kind to arrive
    # unmeasured — which is the point, because this is the one budget paid on
    # every request whether the tool is called or not. Raising it is a number
    # someone has to defend.
    max_schema_bytes=9_500,
    input_schema=plan_input_schema(),
)


async def handle_present_plan(bridge: SessionBridge, args: dict[str, Any]) -> dict[str, Any]:
    """The present_plan tool body, free of SDK imports so it is directly testable.

    ``plan_id`` is dropped before validation, not merely omitted from the tool's
    input schema: the schema is advisory, and ``PlanArtifact``'s default factory
    would keep any id the model sent. Since the tool result the agent reads
    *contains* the id, an agent that echoes it back when re-presenting a revised
    plan would collide with the settled card — the UI dedupes by ``plan_id`` and
    the user would be left with nothing to answer while the tool blocked for the
    full timeout. Minting here makes every presentation a fresh card.

    Validation errors come back as tool errors rather than exceptions: the agent
    reads them and fixes its own arguments on the next call — clamped to this
    tool's result budget, since pydantic's own message length is not ours and
    ten of them can outweigh the plan they are about.
    """
    try:
        artifact = PlanArtifact.model_validate(
            {key: value for key, value in args.items() if key != "plan_id"}
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:10]
        )
        return error_result(
            clamp_result(f"Invalid plan — fix and retry: {problems}", PRESENT_PLAN.max_result_bytes)
        )
    try:
        response = await bridge.present_plan(artifact)
    except PlanAlreadyPendingError as exc:
        return error_result(str(exc))
    return text_result(response.model_dump_json())


# ---- the registry -----------------------------------------------------------

AGENT_TOOLS: tuple[AgentToolSpec, ...] = (GET_WORKSPACE_STATE, PRESENT_PLAN)


def allowed_tool_names() -> list[str]:
    """Allow-list entries for every registered tool — the session's own tools
    are not what ``can_use_tool`` exists to gate."""
    return [spec.qualified_name for spec in AGENT_TOOLS]
