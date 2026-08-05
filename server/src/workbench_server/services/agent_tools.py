"""The agent-facing tool registry, and the ergonomics budget it carries.

Every tool a Workbench session puts in an agent's context is declared here once:
the name, the description the *model* reads, the input schema, and — required,
so ``mypy --strict`` fails an omission rather than letting a tool ship
unmeasured — the format its result comes back in. :mod:`sdk_factory` builds the
MCP server from these specs and derives the session's allow-list from them, so a
tool is added in one place and bounded in one place.

This is the server half of the tool registry; ``ui/src/registry.ts`` is the
client half, where a capability declares the same tools alongside its panel.
The two are deliberately not wired together over the network: this list is what
the SDK actually reads, and duplicating it as a payload would be a second
authority to keep honest for no gain.

The budget is enforced by tests, never by a comment. Every description is loaded
into every session's context, so it is a cost paid on every request (CLAUDE.md);
``server/tests/test_agent_tools.py`` asserts a ceiling on each description and
on the serialized size of a representative result, and the quality gate fails
bloat. Latency is deliberately not budgeted — these are in-process calls where
the model and the user dominate.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from workbench_server.models.agents import UiState
from workbench_server.models.plans import PlanArtifact, plan_input_schema
from workbench_server.services.agent_sessions import PlanAlreadyPendingError, SessionBridge

#: How a tool's result reaches the model. ``compact-json`` means no indent and
#: no pretty separators — measured elsewhere at ~40% fewer tokens than the
#: pretty form for the same payload, which is why it is the default answer.
OutputFormat = Literal["compact-json", "text", "markdown"]

#: Ceilings the tests bind. Raising one is a deliberate act with a diff, which
#: is the whole point: the alternative was a number nobody ever looked at.
MAX_DESCRIPTION_CHARS = 800
MAX_RESULT_BYTES = 2048

MCP_SERVER_NAME = "workbench"


@dataclass(frozen=True)
class AgentToolSpec:
    """One agent-facing tool, as data."""

    name: str
    description: str
    output_format: OutputFormat
    input_schema: dict[str, Any] = field(default_factory=dict)

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


# ---- get_workspace_state ----------------------------------------------------

GET_WORKSPACE_STATE = AgentToolSpec(
    name="get_workspace_state",
    description=(
        "Current workbench UI state: the file the user is looking at, open tabs, "
        "and files with unsaved changes (do NOT edit those)."
    ),
    output_format="compact-json",
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
        "Show the user an interactive plan card in Workbench and wait for their "
        "decision. Use this instead of writing a plan as chat prose whenever you "
        "propose multi-step work or ask the user to choose between alternatives. "
        "Nodes render natively: option_group (the user picks one option), step_list "
        "(ordered steps, file_refs open real editor tabs), question, markdown. "
        "Returns JSON {plan_id, verdict, choices, annotations, comment}. verdict "
        "'approve' means proceed with the chosen options; 'revise' means rework the "
        "plan using their comments and present it again; 'reject' means drop this "
        "approach; 'no_decision' means the user never answered (timeout or "
        "interrupt) — stop and ask in chat, never treat it as approval."
    ),
    output_format="compact-json",
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
    reads them and fixes its own arguments on the next call.
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
        return error_result(f"Invalid plan — fix and retry: {problems}")
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
