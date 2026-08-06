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
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from workbench_server.models.agents import SessionKind, UiState
from workbench_server.models.orchestrator import (
    MAX_TASK_CHARS,
    OrchestratorBudget,
    SpawnRefusal,
    WorkerInfo,
)
from workbench_server.models.plans import PlanArtifact, plan_input_schema
from workbench_server.services.agent_sessions import PlanAlreadyPendingError, SessionBridge

#: Characters of a worker's output one ``read_worker`` returns by default, and
#: the widest window it will serve. Named here rather than imported from
#: ``services/orchestrator.py`` because that module imports *this* one's sibling
#: and the tool's own schema quotes both numbers to the model.
READ_WINDOW_CHARS = 2_000
MAX_READ_WINDOW_CHARS = 8_000

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
    # Every character here is paid for on every request of every session, so
    # the anchors clause (PR 3) was bought rather than added: the wording above
    # it was tightened by 60 characters first. See the ceiling test.
    description=(
        "Show an interactive plan card and wait for the user's decision. "
        "Use it instead of chat prose for multi-step work or a choice between "
        "alternatives. Nodes render natively: "
        "option_group (user picks one), step_list (ordered; file_refs open "
        "editor tabs), question, markdown, visual (tables, charts, diagrams, diffs, "
        "metrics — we draw from your numbers; read the workbench:plan-visual "
        "skill first). Returns {plan_id, verdict, choices, annotations, comment}; "
        "annotations are {anchor,text}; anchor.path names the part it is about. "
        "verdict "
        "'approve' means proceed with the choices; 'revise' means rework it from "
        "their comments and present a new card; 'reject' means drop this "
        "approach; 'no_decision' means the user never answered (timeout or "
        "interrupt) — stop and ask in chat, never treat it as approval."
    ),
    output_format="compact-json",
    # The envelope was 129 bytes for a bare approval and 512 was the ceiling.
    # Anchors (PR 3) changed the *shape*: an annotation is now
    # `{"anchor":{"kind","node_id","path":[...]},"text":...}`, about 100 bytes
    # of structure before the user has typed a word, and the representative
    # approval below — two anchored notes, a choice and a comment — measures
    # 409. 768 covers roughly four such notes. What rides past that is the
    # user's own typing, not ours to budget: the ceiling is on the shape, and
    # the one path we *can* overrun (a validation error) is clamped to it in
    # `handle_present_plan`.
    max_result_bytes=768,
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


# ---- the orchestrator toolset (Mission Control) ------------------------------
#
# Five tools that only an ``orchestrator`` session ever sees. A *separate* tuple
# from ``AGENT_TOOLS`` rather than a `when` on each spec, and the reason is the
# budget this module exists to enforce: a description is paid once per session
# but **a schema is paid on every request**, so putting five toolsets in every
# chat session's context would cost every user of the app for a capability that
# session cannot use. ``build_context_bridge`` takes the kind and hands over
# exactly one of the two lists.

SPAWN_WORKER = AgentToolSpec(
    name="spawn_worker",
    description=(
        "Start a worker agent on one task, in its own git worktree. Returns "
        "{worker_id, slot} or a refusal naming the ceiling and the setting that "
        "raises it. Workers cannot run shell commands without the user "
        "approving each one on the Mission Control board — plan for that."
    ),
    output_format="text",
    # A spawn answers with an id, a slot and a sentence; a refusal answers with
    # the cap, the observation and a setting name. 512 covers the longer of the
    # two (a refusal measured 190 bytes) with room for a slot path.
    max_result_bytes=512,
    max_schema_bytes=420,
    input_schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The worker's first message. Self-contained: it has your context.",
                "maxLength": MAX_TASK_CHARS,
            },
            "base": {"type": "string", "description": "Commit or ref to check out. Default HEAD."},
        },
        "required": ["task"],
    },
)

LIST_WORKERS = AgentToolSpec(
    name="list_workers",
    description="Your workers: id, state, slot, turns, cost, and the task each is on.",
    output_format="text",
    # One line per worker; the ceiling is the worker cap (8) times a ~90-byte
    # line plus the budget footer. 1,024 is that with margin, and it fails the
    # moment a per-worker line grows a field nobody costed.
    max_result_bytes=1_024,
    max_schema_bytes=8,
)

READ_WORKER = AgentToolSpec(
    name="read_worker",
    description=(
        "The tail of one worker's output. Says how much it withheld and how to "
        "ask for more; says so explicitly when there is nothing yet."
    ),
    output_format="text",
    # The window is the budget: MAX_READ_WINDOW_CHARS plus the header and the
    # withheld-count sentence.
    max_result_bytes=MAX_READ_WINDOW_CHARS + 256,
    max_schema_bytes=340,
    input_schema={
        "type": "object",
        "properties": {
            "worker_id": {"type": "string"},
            "chars": {
                "type": "integer",
                "description": (
                    f"Tail size, default {READ_WINDOW_CHARS}, max {MAX_READ_WINDOW_CHARS}."
                ),
            },
        },
        "required": ["worker_id"],
    },
)

SEND_TO_WORKER = AgentToolSpec(
    name="send_to_worker",
    description="Send a follow-up message to a running worker. Refused once its budget is spent.",
    output_format="text",
    max_result_bytes=384,
    max_schema_bytes=200,
    input_schema={
        "type": "object",
        "properties": {"worker_id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["worker_id", "text"],
    },
)

STOP_WORKER = AgentToolSpec(
    name="stop_worker",
    description="Stop one worker and return its worktree slot to the pool.",
    output_format="text",
    max_result_bytes=256,
    max_schema_bytes=120,
    input_schema={
        "type": "object",
        "properties": {"worker_id": {"type": "string"}},
        "required": ["worker_id"],
    },
)


class OrchestratorHandle(Protocol):
    """The slice of ``services/orchestrator.py`` the tool bodies below use.

    A Protocol for the same reason :class:`SessionBridge` is one: it keeps this
    module free of the service, so every tool body is testable against a fake
    and the real service is not importable from the SDK wiring by accident.
    """

    async def spawn(
        self, orchestrator_id: str, task: str, base: str | None = None
    ) -> "WorkerInfo | SpawnRefusal": ...
    def workers_of(self, orchestrator_id: str) -> "list[WorkerInfo]": ...
    def read(self, orchestrator_id: str, worker_id: str, window: int) -> str: ...
    def send(self, orchestrator_id: str, worker_id: str, text: str) -> "str | SpawnRefusal": ...
    async def stop_worker(self, orchestrator_id: str, worker_id: str) -> str: ...
    @property
    def budget(self) -> OrchestratorBudget: ...


def _arg_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


async def handle_spawn_worker(
    orchestrator: OrchestratorHandle, orchestrator_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    task = _arg_str(args, "task")
    if not task:
        return error_result("spawn_worker needs a `task` — the worker's first message.")
    base = _arg_str(args, "base") or None
    outcome = await orchestrator.spawn(orchestrator_id, task[:MAX_TASK_CHARS], base)
    if isinstance(outcome, SpawnRefusal):
        # A refusal is a tool *error*, so the model does not read it as a
        # worker it can talk to — and it carries the way out, never a bare no.
        return error_result(clamp_result(outcome.detail, SPAWN_WORKER.max_result_bytes))
    where = outcome.slot or "the workspace"
    return text_result(f"worker {outcome.worker_id} started in {where} on: {outcome.task[:120]}")


def handle_list_workers(
    orchestrator: OrchestratorHandle, orchestrator_id: str, session_cost: Any
) -> dict[str, Any]:
    """One line per worker plus the ceiling — compact text, never JSON.

    ``session_cost`` is a callable answering "what has this worker spent", kept
    as an argument rather than read here so this module stays free of the usage
    service. It is the same figure the board renders.
    """
    workers = orchestrator.workers_of(orchestrator_id)
    budget = orchestrator.budget
    if not workers:
        # "None" said out loud, with the next step (CLAUDE.md's three shapes).
        return text_result(
            f"no workers — spawn_worker starts one (up to {budget.max_workers} at a time)"
        )
    lines = []
    for worker in workers:
        state = worker.outcome or "running"
        cost = session_cost(worker.worker_id)
        lines.append(
            f"{worker.worker_id} {state} {worker.slot or '-'} "
            f"turns={worker.turns} cost=${cost:.2f} :: {worker.task[:60]}"
        )
    running = sum(1 for worker in workers if worker.outcome is None)
    lines.append(
        f"-- {running}/{budget.max_workers} running, fleet ceiling "
        f"{budget.max_fleet_turns} turns / ${budget.max_fleet_cost_usd:.2f}"
    )
    return text_result(clamp_result("\n".join(lines), LIST_WORKERS.max_result_bytes))


def handle_read_worker(
    orchestrator: OrchestratorHandle, orchestrator_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    worker_id = _arg_str(args, "worker_id")
    if not worker_id:
        return error_result("read_worker needs a `worker_id` — list_workers has them.")
    raw = args.get("chars")
    window = raw if isinstance(raw, int) and not isinstance(raw, bool) else READ_WINDOW_CHARS
    return text_result(
        clamp_result(
            orchestrator.read(orchestrator_id, worker_id, window), READ_WORKER.max_result_bytes
        )
    )


def handle_send_to_worker(
    orchestrator: OrchestratorHandle, orchestrator_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    worker_id = _arg_str(args, "worker_id")
    text = _arg_str(args, "text")
    if not worker_id or not text:
        return error_result("send_to_worker needs `worker_id` and `text`.")
    outcome = orchestrator.send(orchestrator_id, worker_id, text)
    if isinstance(outcome, SpawnRefusal):
        return error_result(clamp_result(outcome.detail, SEND_TO_WORKER.max_result_bytes))
    return text_result(clamp_result(outcome, SEND_TO_WORKER.max_result_bytes))


async def handle_stop_worker(
    orchestrator: OrchestratorHandle, orchestrator_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    worker_id = _arg_str(args, "worker_id")
    if not worker_id:
        return error_result("stop_worker needs a `worker_id` — list_workers has them.")
    answer = await orchestrator.stop_worker(orchestrator_id, worker_id)
    return text_result(clamp_result(answer, STOP_WORKER.max_result_bytes))


# ---- the registry -----------------------------------------------------------

#: Every session's toolset.
AGENT_TOOLS: tuple[AgentToolSpec, ...] = (GET_WORKSPACE_STATE, PRESENT_PLAN)

#: The extra toolset an ``orchestrator`` session carries. Never in the context
#: of a chat or worker session — see the note above the specs.
ORCHESTRATOR_TOOLS: tuple[AgentToolSpec, ...] = (
    SPAWN_WORKER,
    LIST_WORKERS,
    READ_WORKER,
    SEND_TO_WORKER,
    STOP_WORKER,
)

#: Everything the budget test must measure. One list, so a tool that ships in
#: neither tuple is a tool that does not exist rather than one that dodged the
#: ceiling.
ALL_AGENT_TOOLS: tuple[AgentToolSpec, ...] = AGENT_TOOLS + ORCHESTRATOR_TOOLS


def tools_for(kind: SessionKind) -> tuple[AgentToolSpec, ...]:
    """The toolset this kind of session gets. A worker gets the base set: a
    worker that could spawn workers is a fork bomb with a budget attached."""
    return AGENT_TOOLS + ORCHESTRATOR_TOOLS if kind == "orchestrator" else AGENT_TOOLS


def allowed_tool_names(kind: SessionKind = "chat") -> list[str]:
    """Allow-list entries for this session's own tools — they are not what
    ``can_use_tool`` exists to gate."""
    return [spec.qualified_name for spec in tools_for(kind)]
