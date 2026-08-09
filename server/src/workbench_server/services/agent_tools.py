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
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast, get_args

from pydantic import BaseModel, ValidationError

from workbench_server.models.agent_reconcile import ReconcileMismatch, ReconcileSummary
from workbench_server.models.agents import SessionKind, UiState
from workbench_server.models.commands import CommandInvokeResult, CommandManifest
from workbench_server.models.gates import (
    MAX_GATE_LOG_BYTES,
    MAX_GATES_PER_RUN,
    GateLog,
    GateSpec,
)
from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    SlideText,
    WordEdit,
    WordParagraphStyle,
    WordText,
    WordWriteOp,
)
from workbench_server.models.orchestrator import (
    MAX_TASK_CHARS,
    OrchestratorBudget,
    SpawnRefusal,
    WorkerInfo,
)
from workbench_server.models.plans import PlanArtifact, plan_input_schema
from workbench_server.models.reconciliation import ReconciliationReport, ReconciliationSpec
from workbench_server.models.review import (
    MAX_FINDINGS,
    ReportFindingsRequest,
)
from workbench_server.models.search import (
    MAX_HITS,
    SearchRequest,
    SearchResponse,
)
from workbench_server.models.validation import (
    EvidenceKind,
    ValidationResult,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.services.agent_sessions import PlanAlreadyPendingError, SessionBridge
from workbench_server.services.office_host.a1 import cell_ref, column_letter, parse_cell
from workbench_server.services.office_host.document_bridge import DocumentBridgeError

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


# ---- office_read ------------------------------------------------------------

#: Server-side window bounds, applied before the read reaches the bridge so a
#: single call can never return the whole of a large document. The model may ask
#: for fewer characters but not more; the cell budget is not the model's to set.
#: These bound the window's *shape* — characters of Word body, count of Excel
#: cells, and (threaded through as ``max_chars``) the aggregate text of an Excel
#: window so one long cell cannot fill it. They do **not** bound the serialized
#: *byte* size: non-ASCII body (Norwegian æ/ø/å, denser diacritics, emoji) runs
#: to several bytes per character, so 6000 chars can exceed ``max_result_bytes``.
#: ``handle_office_read`` clamps the final text to that byte ceiling as the
#: backstop these char/cell caps cannot be. A 2000-row sheet (16k cells) never
#: fits, which is the point of windowing rather than streaming.
OFFICE_READ_MAX_CHARS = 6_000
OFFICE_READ_MAX_CELLS = 600


class OfficeDocumentReader(Protocol):
    """The narrow read surface the ``office_read`` tool needs — implemented by
    ``OfficeHostService`` and threaded in so this module never imports it."""

    async def document_structure(self, path: str) -> DocStructure: ...

    async def read_document(
        self,
        path: str,
        *,
        max_chars: int,
        max_cells: int,
        sheet: str | None = None,
        a1_range: str | None = None,
        start_paragraph: int = 0,
        start_slide: int = 1,
    ) -> WordText | CellWindow | SlideText: ...


OFFICE_READ = AgentToolSpec(
    name="office_read",
    description=(
        "Read the LIVE Office document Workbench has docked in a panel — only "
        "documents Workbench opened, named by workspace-relative path. Word: the "
        "body windowed by start_paragraph and max_chars, with a 'paragraphs X-Y "
        "of N' footer. Excel: a TSV grid with an A1 corner header and a 'rows X-Y "
        "of N, cols A-H of Z; pass range=A1:H50 for more' footer; omit sheet to "
        "list the sheets, or pass range to pick a corner. PowerPoint: whole slides "
        "from start_slide, each headed 'Slide N: title', read-only. An empty "
        "document says so. Reads include unsaved on-screen edits. If the document "
        "is not docked, it says so and how to open it."
    ),
    # Text, not JSON: an Excel range is list-heavy, and a TSV grid the model can
    # read directly is cheaper than the same cells wrapped in JSON arrays — the
    # one place among these tools where the AXI list-payload argument bites.
    output_format="text",
    # A full window is bounded server-side (the two constants above), so this is
    # sized to the widest one plus the header and footer, not to a whole
    # document: ~6 kB of Word body or a few kB of TSV, never a 2000-row sheet.
    # Raising it means widening those bounds, which is the diff to justify.
    max_result_bytes=8_192,
    # Five small properties, one required. Measured near 640 bytes; 900 leaves
    # room to reword a description-in-schema without letting a sixth argument in
    # unmeasured — the schema is paid on every request whether the tool runs.
    max_schema_bytes=900,
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path of the docked document.",
            },
            "sheet": {
                "type": "string",
                "description": "Excel only: which worksheet. Omit to list the sheets.",
            },
            "range": {
                "type": "string",
                "description": "Excel only: A1 range or corner, e.g. A1:H50 or A51.",
            },
            "start_paragraph": {
                "type": "integer",
                "minimum": 0,
                "description": "Word only: zero-based first paragraph to read.",
            },
            "start_slide": {
                "type": "integer",
                "minimum": 1,
                "description": "PowerPoint only: one-based first slide to read.",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "description": "Word/PowerPoint: cap on characters returned (bounded server-side).",
            },
        },
        "required": ["path"],
    },
)


# ---- office_write -----------------------------------------------------------


class OfficeDocumentWriter(Protocol):
    """The narrow write surface the ``office_write`` tool needs — implemented by
    ``OfficeHostService`` and threaded in so this module never imports it. The
    mirror of :class:`OfficeDocumentReader`, one method wide."""

    async def write_document(
        self,
        path: str,
        *,
        content: str,
        op: WordWriteOp = "replace",
        paragraph: int | None = None,
        after_paragraph: int | None = None,
        style: WordParagraphStyle | None = None,
        sheet: str | None = None,
        cell: str | None = None,
    ) -> WordEdit | CellEdit: ...


class OfficeDocumentAccess(OfficeDocumentReader, OfficeDocumentWriter, Protocol):
    """The office-host service as the SDK wiring sees it: read and write, nothing
    more. One handle threaded into the context bridge so ``office_read`` and
    ``office_write`` share the service without this module importing it."""


#: The style intents an insert may ask for, taken straight from the model's own
#: Literal so the schema the agent reads, the validation this module does and the
#: type the bridge applies cannot drift into three different lists.
WORD_STYLE_INTENTS: tuple[str, ...] = get_args(WordParagraphStyle)


OFFICE_WRITE = AgentToolSpec(
    name="office_write",
    description=(
        "Edit the LIVE Office document Workbench has docked in a panel — one "
        "targeted, undoable edit, only in documents Workbench opened, named by "
        "workspace-relative path. Word: paragraph (zero-based) and content "
        "replace that paragraph whole; op=insert adds a NEW paragraph after "
        "after_paragraph (omit = end, -1 = top), one per call, optional "
        "style=body|heading1|heading2|heading3. An insert shifts every later "
        "paragraph by one — re-address from the index and total it returns. "
        "Excel: sheet, cell (e.g. B2) and content set one cell; empty content "
        "clears it. PowerPoint is read-only. Edits apply to what is on screen, "
        "including unsaved changes, so the user can undo them. A bad target, an "
        "undocked document or a table-cell anchor is refused with the reason. "
        "Read it back with office_read."
    ),
    output_format="text",
    # The result is one confirmation sentence: an address, a char count, a short
    # echo of what was written and the read-back hint. 512 covers a long path and
    # an 80-char echo with margin; the content the model *sent* is its own cost,
    # never echoed in full here — which is why this is nowhere near office_read's.
    # An insert's confirmation is the longest (it also names the new total, the
    # shift and the anchor for the next one) and measured 232 bytes on a short
    # path, so the ceiling is unchanged.
    max_result_bytes=512,
    # Eight small properties, two required. Measured 490 bytes at five (the
    # replace/set surface); the three the insert adds — op, after_paragraph,
    # style — measure 902 in total. 1100 leaves the same kind of headroom the
    # 800 did: room to reword a description-in-schema, not room for a ninth
    # argument to arrive unmeasured. The schema is paid on every request whether
    # the tool runs, so this is the number that had to be justified rather than
    # raised quietly: the enum on `style` is the price of an agent that can say
    # "heading" without knowing the document's locale.
    max_schema_bytes=1_100,
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path of the docked document.",
            },
            "content": {
                "type": "string",
                "description": "The new text for the target paragraph or cell.",
            },
            "op": {
                "type": "string",
                "enum": ["replace", "insert"],
                "description": "Word: replace the target (default) or insert a new paragraph.",
            },
            "paragraph": {
                "type": "integer",
                "minimum": 0,
                "description": "Word only: zero-based paragraph to replace.",
            },
            "after_paragraph": {
                "type": "integer",
                "minimum": -1,
                "description": "Word insert: insert after this paragraph; omit for the end.",
            },
            "style": {
                "type": "string",
                "enum": list(WORD_STYLE_INTENTS),
                "description": "Word insert: the new paragraph's style. Omit to follow the anchor.",
            },
            "sheet": {
                "type": "string",
                "description": "Excel only: which worksheet.",
            },
            "cell": {
                "type": "string",
                "description": "Excel only: which cell, e.g. B2.",
            },
        },
        "required": ["path", "content"],
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


def _office_read_refusal(error: DocumentBridgeError, path: str) -> str:
    """Turn a read refusal into a sentence the agent can act on (AXI shape 3)."""
    if error.reason == "document_not_hosted":
        return (
            f"{path} is not docked in Workbench. Open it first (the Office panel or "
            "the office host), then read it."
        )
    if error.reason == "document_gone":
        return f"{path} was closed before it could be read; re-open it to read it again."
    if error.reason == "range_invalid":
        return str(error)
    return f"{path} cannot be read right now: {error}"


def _format_word(word: WordText, path: str) -> str:
    """Word body plus the 'paragraphs X-Y of N' footer, or an explicit 'empty'."""
    if word.total_paragraphs == 0:
        return f"{path} (Word) is empty — it has no paragraphs."
    returned = word.text.count("\n\n") + 1 if word.text else 0
    end = word.start_paragraph + returned  # exclusive, one-based at the boundary
    shown = f"paragraphs {word.start_paragraph + 1}-{end} of {word.total_paragraphs}"
    if end < word.total_paragraphs:
        footer = f"[{shown}; pass start_paragraph={end} for the next, or a larger max_chars.]"
    else:
        footer = f"[{shown} — end of document.]"
    return f"{word.text}\n\n{footer}"


def _format_slides(slides: SlideText, path: str) -> str:
    """Slide blocks plus a 'slides X-Y of N' footer, or an explicit 'empty'."""
    if slides.total_slides == 0:
        return f"{path} (PowerPoint) is empty — it has no slides."
    end = slides.start_slide + slides.returned_slides - 1
    shown = f"slides {slides.start_slide}-{end} of {slides.total_slides}"
    if end < slides.total_slides:
        footer = f"[{shown}; pass start_slide={end + 1} for the next, or a larger max_chars.]"
    else:
        footer = f"[{shown} — end of deck.]"
    return f"{slides.text}\n\n{footer}"


def _format_sheet_list(structure: DocStructure, path: str) -> str:
    """The 'omit sheet to list the sheets' answer (AXI shape 3: what to do next)."""
    sheets = structure.sheets or []
    if not sheets:
        return f"{path} (Excel) has no sheets."
    lines = [f"{path} (Excel), {len(sheets)} sheet(s):"]
    for sheet in sheets:
        size = "empty" if sheet.rows == 0 else f"{sheet.rows} rows x {sheet.cols} cols"
        lines.append(f"  {sheet.name}: {size}")
    lines.append("Pass sheet=<name> (and optionally range=A1:H50) to read one.")
    return "\n".join(lines)


def _format_excel(window: CellWindow, path: str) -> str:
    """A TSV grid with an A1 corner header and a windowing footer, or 'empty'."""
    if window.total_rows == 0:
        return f"{path} (Excel) sheet {window.sheet!r} is empty — it has no cells."
    corner = window.a1_range.split(":", 1)[0]
    first_row, first_col = parse_cell(corner)
    header = "\t".join([window.sheet, *(column_letter(first_col + j) for j in range(window.cols))])
    lines = [header]
    for offset, row in enumerate(window.cells):
        lines.append("\t".join([str(first_row + 1 + offset), *row]))
    last_col = first_col + window.cols - 1
    rows_shown = f"rows {first_row + 1}-{first_row + window.rows} of {window.total_rows}"
    cols_shown = (
        f"cols {column_letter(first_col)}-{column_letter(last_col)} "
        f"of {column_letter(window.total_cols - 1)}"
    )
    if first_row + window.rows < window.total_rows or last_col < window.total_cols - 1:
        nxt = cell_ref(first_row + window.rows, first_col)
        footer = f"[{rows_shown}, {cols_shown}; pass range={nxt}: for more.]"
    else:
        footer = f"[{rows_shown}, {cols_shown} — whole sheet.]"
    return f"{window.sheet}!{window.a1_range}\n" + "\n".join(lines) + f"\n{footer}"


async def handle_office_read(reader: OfficeDocumentReader, args: dict[str, Any]) -> dict[str, Any]:
    """The office_read tool body, free of SDK imports so it is directly testable.

    Structure first, then the read: the structure call is cheap and it is what
    lets an Excel request with no sheet answer with the *list* of sheets rather
    than a refusal. Word ignores sheet/range; Excel ignores start_paragraph. The
    read window is bounded here, before the bridge sees it, so no single call can
    return a whole large document.
    """
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return error_result("office_read needs a workspace-relative 'path' to a docked document.")
    sheet = args.get("sheet") if isinstance(args.get("sheet"), str) else None
    a1_range = args.get("range") if isinstance(args.get("range"), str) else None
    raw_start = args.get("start_paragraph", 0)
    start_paragraph = raw_start if isinstance(raw_start, int) and raw_start >= 0 else 0
    raw_slide = args.get("start_slide", 1)
    start_slide = raw_slide if isinstance(raw_slide, int) and raw_slide >= 1 else 1
    raw_max = args.get("max_chars")
    max_chars = (
        min(raw_max, OFFICE_READ_MAX_CHARS)
        if isinstance(raw_max, int) and raw_max > 0
        else OFFICE_READ_MAX_CHARS
    )
    try:
        structure = await reader.document_structure(path)
        if structure.kind == "excel" and sheet is None:
            return text_result(_format_sheet_list(structure, path))
        result = await reader.read_document(
            path,
            max_chars=max_chars,
            max_cells=OFFICE_READ_MAX_CELLS,
            sheet=sheet,
            a1_range=a1_range,
            start_paragraph=start_paragraph,
            start_slide=start_slide,
        )
    except DocumentBridgeError as error:
        return error_result(_office_read_refusal(error, path))
    if isinstance(result, WordText):
        text = _format_word(result, path)
    elif isinstance(result, SlideText):
        text = _format_slides(result, path)
    else:
        text = _format_excel(result, path)
    # The two server-side caps bound the window's *shape* (characters of Word body,
    # count of Excel cells), not its byte size: non-ASCII body (Norwegian æ/ø/å,
    # denser diacritics or emoji) runs to several bytes per character, so a full
    # window can serialize past ``max_result_bytes`` even though it is within the
    # char/cell caps. Clamp is the byte backstop the caps cannot be — office_read
    # is text the model reads, never JSON it parses, so truncating it is safe.
    return text_result(clamp_result(text, OFFICE_READ.max_result_bytes))


#: Chars of the written content echoed back in the confirmation. Enough for the
#: model to recognise what it wrote without paying to have its own input quoted
#: back in full — the echo is a courtesy, the address and count are the answer.
OFFICE_WRITE_ECHO_CHARS = 80


def _office_write_refusal(error: DocumentBridgeError, path: str) -> str:
    """Turn a write refusal into a sentence the agent can act on (AXI shape 3)."""
    if error.reason == "document_not_hosted":
        return (
            f"{path} is not docked in Workbench. Open it first (the Office panel or "
            "the office host), then edit it."
        )
    if error.reason == "document_gone":
        return f"{path} was closed before the edit could apply; re-open it to edit it again."
    if error.reason == "range_invalid":
        # The bridge's own message names the empty/invalid target the write hit.
        return str(error)
    return f"{path} cannot be edited right now: {error}"


def _echo(content: str) -> str:
    """A short, quoted preview of what was written, truncated with a stated tail."""
    if len(content) <= OFFICE_WRITE_ECHO_CHARS:
        return f'"{content}"'
    return f'"{content[:OFFICE_WRITE_ECHO_CHARS]}…" ({len(content)} chars in all)'


def _format_word_edit(edit: WordEdit, path: str, content: str) -> str:
    """Confirm a Word paragraph write and name the read-back (AXI shapes 2 & 3)."""
    if edit.op == "insert":
        return _format_word_insert(edit, path, content)
    where = f"paragraph {edit.paragraph + 1} of {path}"
    confirm = (
        f"emptied {where}"
        if edit.written_chars == 0
        else f"wrote {edit.written_chars} chars to {where}: {_echo(content)}"
    )
    return (
        f"{confirm}. The document still has {edit.total_paragraphs} paragraphs; "
        f"read it back with office_read (start_paragraph={edit.paragraph}) to confirm."
    )


def _format_word_insert(edit: WordEdit, path: str, content: str) -> str:
    """Confirm an inserted paragraph — and say what it did to every address after it.

    The addressing clause is the point (AXI shape 3, and the reason ``op`` exists
    on :class:`WordEdit`). An insert invalidates the paragraph numbers the model
    is holding from its last read, so the confirmation states the new index, the
    new total, the shift, and the exact argument for the *next* paragraph of the
    same section — the alternative is a re-read before every second sentence.
    Indices here are zero-based throughout, matching the arguments they are
    handed straight back to.
    """
    styled = f" ({edit.style})" if edit.style else ""
    what = (
        "inserted an empty paragraph"
        if edit.written_chars == 0
        else f"inserted {edit.written_chars} chars as a new paragraph"
    )
    echo = "" if edit.written_chars == 0 else f": {_echo(content)}"
    # An append has nothing below it, so saying "everything after has shifted"
    # would be a claim about paragraphs that do not exist — true but useless, and
    # the kind of boilerplate a model learns to stop reading.
    shift = (
        "nothing else moved"
        if edit.paragraph >= edit.total_paragraphs - 1
        else f"every index from {edit.paragraph} on has shifted by 1"
    )
    return (
        f"{what} at index {edit.paragraph}{styled} of {path}{echo}. The document now has "
        f"{edit.total_paragraphs} paragraphs and {shift}; office_read "
        f"(start_paragraph={edit.paragraph}) confirms it, and after_paragraph={edit.paragraph} "
        f"continues the section."
    )


def _format_cell_edit(edit: CellEdit, path: str, content: str) -> str:
    """Confirm an Excel cell write and name the read-back (AXI shapes 2 & 3)."""
    where = f"{edit.sheet}!{edit.a1_cell} of {path}"
    confirm = (
        f"cleared {where}"
        if edit.written_chars == 0
        else f"wrote {edit.written_chars} chars to {where}: {_echo(content)}"
    )
    return (
        f"{confirm}. Read it back with office_read "
        f"(sheet={edit.sheet}, range={edit.a1_cell}) to confirm."
    )


def _write_op(args: dict[str, Any]) -> WordWriteOp | str:
    """The requested operation, or the raw value when it is not one we have.

    An unrecognised ``op`` is **never** quietly treated as the default: the
    default rewrites an existing paragraph, so a model that meant "add" and
    mistyped it would destroy the user's text and be told it succeeded.
    """
    raw = args.get("op", "replace")
    return "insert" if raw == "insert" else ("replace" if raw == "replace" else str(raw))


def _anchor(args: dict[str, Any]) -> int | str | None:
    """The insert anchor: an int, ``None`` for "append at the end", or the raw
    value when it is neither — refused above rather than rounded into a position
    the caller did not ask for."""
    raw = args.get("after_paragraph")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < -1:
        return str(raw)
    return raw


async def handle_office_write(writer: OfficeDocumentWriter, args: dict[str, Any]) -> dict[str, Any]:
    """The office_write tool body, free of SDK imports so it is directly testable.

    The service decides which target the document's kind requires (a paragraph
    for Word, a sheet+cell for Excel) and refuses a missing one rather than
    guessing — the tool only marshals the arguments and renders the confirmation.
    A refusal comes back as a tool error the agent reads and fixes, never an
    exception, and it names the empty/invalid target or how to dock the document.

    The insert arguments are validated *here*, before the service sees them, for
    one reason: every one of them names a position in the user's document, and
    the failure mode of a lenient parse is not an error message — it is text
    landing somewhere nobody asked for.
    """
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return error_result("office_write needs a workspace-relative 'path' to a docked document.")
    content = args.get("content")
    if not isinstance(content, str):
        return error_result("office_write needs 'content' — the new text for the target.")
    op = _write_op(args)
    if op not in ("replace", "insert"):
        return error_result(f"office_write op must be 'replace' or 'insert', not {op!r}.")
    raw_paragraph = args.get("paragraph")
    paragraph = (
        raw_paragraph
        if isinstance(raw_paragraph, int)
        and not isinstance(raw_paragraph, bool)
        and raw_paragraph >= 0
        else None
    )
    after_paragraph = _anchor(args)
    style = args.get("style")
    if op == "insert":
        if paragraph is not None:
            return error_result(
                "office_write insert addresses with 'after_paragraph', not 'paragraph' "
                f"(you passed paragraph={paragraph}). Omit after_paragraph to append at the end."
            )
        if isinstance(after_paragraph, str):
            return error_result(
                f"office_write after_paragraph must be a paragraph index or -1, not "
                f"{after_paragraph!r}. Omit it to append at the end of the document."
            )
        if style is not None and style not in WORD_STYLE_INTENTS:
            return error_result(
                f"office_write style must be one of {', '.join(WORD_STYLE_INTENTS)}, "
                f"not {style!r}. Omit it to follow the paragraph you are inserting after."
            )
    elif style is not None:
        # A replace changes text, never formatting. Saying so beats letting the
        # model believe it restyled a paragraph it did not.
        return error_result(
            "office_write style applies to op=insert only; a replace changes the "
            "paragraph's text and leaves its style alone."
        )
    elif after_paragraph is not None and paragraph is None:
        # It named a position and forgot the op. Answering "name a paragraph"
        # here — which is what the service would say — reads as a contradiction
        # to a model that just named one.
        return error_result(
            "office_write got after_paragraph without op='insert'. Pass op='insert' to add a "
            "new paragraph there, or paragraph=<index> to replace an existing one."
        )
    sheet = args.get("sheet") if isinstance(args.get("sheet"), str) else None
    cell = args.get("cell") if isinstance(args.get("cell"), str) else None
    try:
        edit = await writer.write_document(
            path,
            content=content,
            op=cast(WordWriteOp, op),
            paragraph=paragraph,
            after_paragraph=after_paragraph if isinstance(after_paragraph, int) else None,
            style=cast(WordParagraphStyle, style) if isinstance(style, str) else None,
            sheet=sheet,
            cell=cell,
        )
    except DocumentBridgeError as error:
        return error_result(_office_write_refusal(error, path))
    text = (
        _format_word_edit(edit, path, content)
        if isinstance(edit, WordEdit)
        else _format_cell_edit(edit, path, content)
    )
    return text_result(clamp_result(text, OFFICE_WRITE.max_result_bytes))


# ---- run_command ------------------------------------------------------------
#
# The window owns the command registry; this tool lets an agent reach one, so it
# can *arrange the window it works in* (open a file beside the terminal, put a
# plan card in a pane of its own) rather than only describing what to click. It
# never runs arbitrary code: only ids the window published are invocable, and the
# relay refuses the rest (M5 item 14).


class CommandInvoker(Protocol):
    """The narrow slice of ``services/commands.CommandRelay`` this tool needs —
    threaded into the context bridge so this module never imports the service."""

    def manifest(self) -> CommandManifest: ...
    def is_registered(self, command_id: str) -> bool: ...
    async def invoke(self, command_id: str, params: dict[str, Any]) -> CommandInvokeResult: ...


#: Commands listed in one discovery call before the list is capped and the rest
#: named as a count (AXI shape 1). The window has a few dozen commands, so this
#: shows the lot in the ordinary case and bounds a future that grows unbounded.
RUN_COMMAND_LIST_MAX = 50

RUN_COMMAND = AgentToolSpec(
    name="run_command",
    description=(
        "Arrange the Workbench window by invoking one of its registered commands — "
        "focus or open a panel, split a pane, switch a layout — instead of only "
        "telling the user what to click. Call with no command_id to list the "
        "available commands (id :: title); call with a command_id to run one. "
        "params is reserved for commands that take arguments and is otherwise "
        "ignored. Only commands the window published are invocable; an unknown id "
        "is refused. Returns a one-line confirmation of what ran, or why it did "
        "not (no window connected, or the window did not confirm)."
    ),
    output_format="text",
    # A confirmation is one sentence; the list is the larger case — up to
    # RUN_COMMAND_LIST_MAX lines of `id :: title` (~45 bytes each) plus the
    # capped-count footer. 2,560 covers that with margin; a longer list is cut to
    # it and says so.
    max_result_bytes=2_560,
    # Two small optional properties. Measured near 250 bytes; 420 leaves room to
    # reword a description-in-schema without a third argument slipping in
    # unmeasured — the schema is paid on every request whether the tool runs.
    max_schema_bytes=420,
    input_schema={
        "type": "object",
        "properties": {
            "command_id": {
                "type": "string",
                "description": "Registered command id; omit to list the available commands.",
            },
            "params": {
                "type": "object",
                "description": "Reserved: arguments for a command that takes them.",
            },
        },
    },
)


def _format_command_list(manifest: CommandManifest) -> dict[str, Any]:
    """The discovery answer: one `id :: title` per line, capped with a count.

    An empty manifest says so and how to fix it (AXI shapes 2 and 3): no window
    has connected, so there is nothing to run and nothing to list yet.
    """
    items = manifest.commands
    if not items:
        return text_result(
            "No commands available — no Workbench window is connected. "
            "Open the window, then call run_command again."
        )
    shown = items[:RUN_COMMAND_LIST_MAX]
    lines = [f"{item.id} :: {item.title}" for item in shown]
    if len(items) > RUN_COMMAND_LIST_MAX:
        withheld = len(items) - RUN_COMMAND_LIST_MAX
        lines.append(f"-- {withheld} more; these are the first {RUN_COMMAND_LIST_MAX}.")
    return text_result(clamp_result("\n".join(lines), RUN_COMMAND.max_result_bytes))


async def handle_run_command(invoker: CommandInvoker, args: dict[str, Any]) -> dict[str, Any]:
    """The run_command tool body, free of SDK imports so it is directly testable.

    No command_id lists the manifest; a command_id runs it. An unknown id is a
    tool error the agent fixes by discovering the real ones — never a silent
    pass. A run that does not succeed (no window, no confirmation) also comes
    back as a tool error, with the relay's own sentence for why.
    """
    command_id = _arg_str(args, "command_id")
    if not command_id:
        return _format_command_list(invoker.manifest())
    if not invoker.is_registered(command_id):
        return error_result(
            f"{command_id!r} is not a registered command. "
            "Call run_command with no command_id to list them."
        )
    raw = args.get("params")
    params = raw if isinstance(raw, dict) else {}
    result = await invoker.invoke(command_id, params)
    if result.ok:
        confirm = f"ran {command_id}" + (f": {result.detail}" if result.detail else "")
        return text_result(clamp_result(confirm, RUN_COMMAND.max_result_bytes))
    return error_result(
        clamp_result(f"did not run {command_id}: {result.detail}", RUN_COMMAND.max_result_bytes)
    )


# ---- office_reconcile -------------------------------------------------------
#
# The moat, made agent-usable. An agent that just wrote numbers into a workbook
# asks: do the cells actually hold what I computed? This runs M6's unit- and
# DST-aware reconciliation gate (services/reconciliation.py, registered on the
# ValidationService) and hands back an honest pass/warn/fail with the worst few
# mismatches — never the whole table, which lives behind the result's payload_ref.


class ReconciliationRunner(Protocol):
    """The narrow slice of ``ValidationService`` this tool needs — threaded into
    the context bridge so this module never imports the service (the CommandInvoker
    pattern). ``run`` dispatches to the registered ``reconciliation`` check; ``payload``
    fetches the stored :class:`ReconciliationReport` the worst-mismatch list is read
    from."""

    async def run(self, spec: ValidationSpec) -> ValidationResult: ...
    def payload(self, kind: EvidenceKind, ref: str) -> BaseModel | None: ...


#: Worst flagged cells carried in one confirmation. Worst-first; beyond this the
#: count is stated as ``withheld`` and the full table named (AXI shape 1). A handful
#: is what an agent acts on in one turn; the rest is a read of the named result.
RECONCILE_WORST_N = 5

#: Field clips that keep the compact-JSON result provably inside its byte budget:
#: the agent controls a cell address and the check writes a reason, so both are
#: bounded before serialization rather than clamped after (clamping JSON corrupts it).
_RECONCILE_CELL_CLIP = 64
_RECONCILE_REASON_CLIP = 140
_RECONCILE_LINE_CLIP = 200

OFFICE_RECONCILE = AgentToolSpec(
    name="office_reconcile",
    description=(
        "Check numbers you wrote into an .xlsx against your own computed "
        "expectations — the unit- and DST-aware reconciliation gate. Give the "
        "workbook path, a default_tolerance (abs and/or rel), and expectations: "
        "direct cells (cell like Sheet1!D14, expected, unit such as MWh/MW/EUR/MWh, "
        "optional cell_unit so a kWh-vs-MWh 1000x cannot pass silently), and/or a "
        "time_index that aligns rows to local wall-clock (needs timezone). Returns "
        "the derived risk (pass/low/medium/high/blocked), a one-line summary, "
        "pass/warn/fail counts, and the worst few mismatches (cell, expected, actual, "
        "delta, reason). An unreadable workbook or empty cells is an honest fail, "
        "never a silent pass; the full per-cell table is the named validation result."
    ),
    output_format="compact-json",
    # The success body is bounded by construction, not clamped (clamping valid JSON
    # hands back invalid JSON): RECONCILE_WORST_N mismatches, each with a clipped
    # cell and reason, plus a summary and next-step line. Five worst mismatches with
    # 64-char cells and 140-char reasons measure well under 2 kB even with the widest
    # float reprs; 2,560 is that with a margin the budget test pins.
    max_result_bytes=2_560,
    # The request reuses ReconciliationSpec, so the schema is the widest of these
    # tools: workbook, two expectation shapes, a tolerance and a time-index block.
    # Measured near 2.3 kB; 2,800 leaves room to reword a field description without a
    # new property slipping in unmeasured — the schema is paid on every request.
    max_schema_bytes=2_800,
    input_schema={
        "type": "object",
        "properties": {
            "workbook": {
                "type": "string",
                "description": "Workspace-relative path to the .xlsx to check.",
            },
            "default_tolerance": {
                "type": "object",
                "description": "Match band for any cell without an override; need at least one.",
                "properties": {
                    "abs": {"type": "number", "description": "Absolute band, in the value's unit."},
                    "rel": {
                        "type": "number",
                        "description": "Relative band, a fraction (0.001=0.1%).",
                    },
                },
            },
            "expectations": {
                "type": "array",
                "description": "Direct-cell checks: your number vs a workbook cell.",
                "items": {
                    "type": "object",
                    "properties": {
                        "cell": {
                            "type": "string",
                            "description": "A1 address, e.g. Sheet1!D14 or D14 (active sheet).",
                        },
                        "expected": {"type": "number", "description": "Your value, in unit."},
                        "unit": {
                            "type": "string",
                            "description": "Unit of expected: MWh/MW/EUR/MWh/EUR/% or '' default.",
                        },
                        "cell_unit": {
                            "type": "string",
                            "description": "Cell's unit if it differs; incompatible dims fail.",
                        },
                        "label": {"type": "string", "description": "Human label for the evidence."},
                    },
                    "required": ["cell", "expected"],
                },
            },
            "per_cell_tolerance": {
                "type": "object",
                "description": "Optional cell -> {abs,rel} overrides, keyed by the cell string.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA zone (e.g. Europe/Oslo). Required when time_index is set.",
            },
            "time_index": {
                "type": "object",
                "description": "Time-indexed check: rows aligned by local wall-clock time.",
                "properties": {
                    "timestamp_column": {
                        "type": "string",
                        "description": "Column letter of the local timestamps, e.g. A.",
                    },
                    "value_column": {
                        "type": "string",
                        "description": "Column letter of the values, e.g. B.",
                    },
                    "value_unit": {
                        "type": "string",
                        "description": "Unit of the value column (default: expectation's unit).",
                    },
                    "start_row": {
                        "type": "integer",
                        "description": "1-based first data row (default 2).",
                    },
                    "sheet": {"type": "string", "description": "Sheet the columns live on."},
                    "expectations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "timestamp": {
                                    "type": "string",
                                    "description": "Naive local ISO, e.g. 2024-10-27T02:00:00.",
                                },
                                "expected": {"type": "number"},
                                "unit": {"type": "string"},
                                "fold": {
                                    "type": "integer",
                                    "description": "Repeated fall-back hour: 0 first, 1 second.",
                                },
                                "label": {"type": "string"},
                            },
                            "required": ["timestamp", "expected"],
                        },
                    },
                },
                "required": ["timestamp_column", "value_column"],
            },
        },
        "required": ["workbook", "default_tolerance"],
    },
)


def _clip(text: str, limit: int) -> str:
    """A hard character clip with a stated tail, so a field cannot blow the budget."""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def _finite(value: float | None) -> float | None:
    """Drop a non-finite float to ``None`` so the summary stays valid JSON.

    ``ExpectedValue.expected`` is data the agent supplies: a JSON literal like
    ``1e400`` overflows to ``inf`` in Python's parser with no guard, and a ``delta``
    of two finite-but-huge floats can overflow the same way. ``model_dump_json``
    would serialize ``inf``/``nan`` as the bare tokens ``Infinity``/``NaN`` — not
    valid JSON per RFC 8259 — so a strict parser on the agent's side would choke on
    the very result that promises to be machine-readable. Mapping to ``None`` mirrors
    how ``actual``/``delta`` already go ``None`` for an unreadable cell."""
    return value if value is None or math.isfinite(value) else None


def _reconcile_summary(result: ValidationResult, runner: ReconciliationRunner) -> ReconcileSummary:
    """Fold one run into the compact confirmation, worst-first and budget-bounded.

    The reconciliation check returns exactly one grouped ``numeric`` evidence line;
    its ``payload_ref`` names the stored :class:`ReconciliationReport`. When there is
    no report — a spec error, an unreadable workbook, every cell empty — the risk is
    still an honest ``high`` fail and the evidence detail carries the reason, so the
    summary says *why* rather than a blank the model reads as either "clean" or "broke".
    """
    evidence = next((item for item in result.evidence if item.kind == "numeric"), None)
    report: ReconciliationReport | None = None
    if evidence is not None and evidence.payload_ref is not None:
        payload = runner.payload("numeric", evidence.payload_ref)
        if isinstance(payload, ReconciliationReport):
            report = payload

    if report is None:
        # No table to draw from: surface the honest reason, not a green blank.
        reason = evidence.detail if evidence is not None else result.summary
        return ReconcileSummary(
            risk=result.risk,
            summary=_clip(reason, _RECONCILE_LINE_CLIP),
            passed=0,
            warned=0,
            failed=0,
            total=0,
            worst=[],
            withheld=0,
            validation_id=result.validation_id,
            next_step=_clip(
                f"Nothing reconciled — {reason} Fix and re-run; full result "
                f"{result.validation_id}.",
                _RECONCILE_LINE_CLIP,
            ),
        )

    passed = report.matched
    failed = report.mismatched
    total = report.total
    warned = max(total - passed - failed, 0)
    flagged = [c for c in report.comparisons if c.outcome in ("fail", "warn")]
    worst = [
        ReconcileMismatch(
            cell=_clip(c.cell, _RECONCILE_CELL_CLIP),
            expected=_finite(c.expected),
            actual=_finite(c.actual),
            delta=_finite(c.delta),
            reason=_clip(c.reason, _RECONCILE_REASON_CLIP) if c.reason else None,
        )
        for c in flagged[:RECONCILE_WORST_N]
    ]
    # Withheld is measured against the *true* count of bad cells, not the stored
    # window (which the report already capped at 200): the failed/warned totals are
    # exact, so an agent is never told it saw everything when it did not.
    withheld = max((failed + warned) - len(worst), 0)

    if failed == 0 and warned == 0:
        summary = f"All {total} cells reconciled within tolerance."
        next_step = _clip(
            f"Clean: risk {result.risk}. Full table is validation result {result.validation_id}.",
            _RECONCILE_LINE_CLIP,
        )
    else:
        first = worst[0].cell if worst else "?"
        summary = _clip(
            f"{failed} fail / {warned} warn of {total} cells; worst {first}.",
            _RECONCILE_LINE_CLIP,
        )
        more = f" ({withheld} more not shown)" if withheld else ""
        next_step = _clip(
            f"Fix the {failed} flagged cell(s){more} and re-run; the full per-cell "
            f"table is validation result {result.validation_id} (kind 'numeric').",
            _RECONCILE_LINE_CLIP,
        )

    return ReconcileSummary(
        risk=result.risk,
        summary=summary,
        passed=passed,
        warned=warned,
        failed=failed,
        total=total,
        worst=worst,
        withheld=withheld,
        validation_id=result.validation_id,
        next_step=next_step,
    )


async def handle_office_reconcile(
    runner: ReconciliationRunner, args: dict[str, Any]
) -> dict[str, Any]:
    """The office_reconcile tool body, free of SDK imports so it is directly testable.

    The request *is* a ReconciliationSpec — validated here so a malformed call is a
    tool error the agent fixes, not an exception — mapped into a ValidationSpec that
    names the ``reconciliation`` check and carries the spec as its per-check params.
    The compact confirmation the agent reads is built from the run; the whole table
    stays behind the result's ``payload_ref`` and is only *named* here.
    """
    try:
        spec = ReconciliationSpec.model_validate(args)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:6]
        )
        return error_result(
            clamp_result(
                f"Invalid reconciliation request — fix and retry: {problems}",
                OFFICE_RECONCILE.max_result_bytes,
            )
        )
    validation_spec = ValidationSpec(
        subject=ValidationSubject(kind="file", ref=spec.workbook, label=spec.workbook),
        checks=["reconciliation"],
        params=spec.model_dump(),
    )
    result = await runner.run(validation_spec)
    summary = _reconcile_summary(result, runner)
    # Compact JSON, never clamped: the body is bounded by construction (a fixed
    # worst-N, clipped fields), so it is valid JSON the agent parses — not a wall
    # of prose truncated mid-token.
    return text_result(summary.model_dump_json())


# ---- workspace_search -------------------------------------------------------
#
# Grep the folder the session is working in, without shelling out. The window
# owns the same search (services/search.py, the Ctrl+Shift+F panel); this is the
# agent's door to it, so an agent can find where a symbol lives before it edits
# rather than re-reading files it already read. It respects the file tree's
# ignore rules (IGNORED_DIRS + CACHEDIR.TAG), so it never returns a match inside
# a build cache the user cannot see either.


class WorkspaceSearcher(Protocol):
    """The narrow slice of ``SearchService`` this tool needs — threaded into the
    context bridge so this module never imports the service (the CommandInvoker
    pattern)."""

    def search(self, request: SearchRequest) -> SearchResponse: ...


#: Hits carried in one tool result before the list is capped and the rest named
#: as a count (AXI shape 1). Smaller than the panel's default: an agent acts on a
#: handful and reads the file for the rest, and the cap is what keeps the result
#: provably inside its byte budget.
WORKSPACE_SEARCH_MAX_HITS = 40

#: Field clips that keep the compact text result inside its byte budget by
#: construction: a matching line and a path are both bounded before the result is
#: assembled, so the body cannot blow the ceiling on a minified file or a deep path.
_SEARCH_LINE_CLIP = 160
_SEARCH_PATH_CLIP = 120

WORKSPACE_SEARCH = AgentToolSpec(
    name="workspace_search",
    description=(
        "Find literal text across the files in your workspace folder — the same "
        "content search the Ctrl+Shift+F panel runs. Give a query; optionally "
        "max_results (default 40) and case_sensitive (default false). Returns hits "
        "grouped by file as 'path:line: matching text', respecting the file tree's "
        "ignored dirs and build caches (.git, node_modules, CACHEDIR.TAG). Says so "
        "explicitly when nothing matches, and when it stopped at max_results it "
        "names how to see more. Substring match, not a regex. Open a named file to "
        "read a hit in context."
    ),
    output_format="text",
    # WORKSPACE_SEARCH_MAX_HITS lines of `  line: text` (each clipped to
    # _SEARCH_LINE_CLIP) plus a path header per file and a header/footer sentence.
    # 40 hits * ~180 bytes + headers measures well under 8 kB; 8,192 is that with a
    # margin the budget test pins, and a longer body is clamped to it (text the
    # model reads, never JSON it parses, so clamping is safe).
    max_result_bytes=8_192,
    # Three small properties, one required. Measured near 480 bytes; 700 leaves
    # room to reword a field description without a fourth argument slipping in
    # unmeasured — the schema is paid on every request whether the tool runs.
    max_schema_bytes=700,
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The literal text to find (substring, not a regex).",
                "minLength": 1,
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_HITS,
                "description": f"Cap on hits (default {WORKSPACE_SEARCH_MAX_HITS}).",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Match case exactly. Default false.",
            },
        },
        "required": ["query"],
    },
)


def _format_search(response: SearchResponse) -> str:
    """Group the hits under their files, and end with what to do next.

    All three AXI shapes: a truncated result names ``max_results`` as the way to
    widen it (shape 1), an empty result says so and suggests a change (shape 2),
    and a non-empty one ends by naming a file to open (shape 3).
    """
    if not response.files:
        return (
            f"No matches for {response.query!r} in the workspace. "
            "Try a shorter or different query, or case_sensitive=false."
        )
    lines = [
        f"{response.total_hits} match(es) in {response.files_with_matches} file(s) "
        f"for {response.query!r}:"
    ]
    for file in response.files:
        lines.append(_clip(file.path, _SEARCH_PATH_CLIP))
        for hit in file.hits:
            lines.append(f"  {hit.line}: {_clip(hit.text.strip(), _SEARCH_LINE_CLIP)}")
    first = response.files[0].path
    if response.truncated:
        lines.append(
            f"[stopped at {response.total_hits} hits; more exist — narrow the query "
            "or raise max_results.]"
        )
    else:
        lines.append(
            f"Open a file (e.g. {_clip(first, _SEARCH_PATH_CLIP)}) to read a hit in context."
        )
    return "\n".join(lines)


def handle_workspace_search(searcher: WorkspaceSearcher, args: dict[str, Any]) -> dict[str, Any]:
    """The workspace_search tool body, free of SDK imports so it is directly testable.

    A missing or malformed query is a tool error the agent fixes, not an
    exception. The result is clamped to the tool's byte budget as a backstop — it
    is text the model reads, never JSON it parses, so truncation is safe.
    """
    query = _arg_str(args, "query")
    if not query:
        return error_result("workspace_search needs a `query` — the text to find.")
    raw_max = args.get("max_results")
    max_results = (
        min(raw_max, MAX_HITS)
        if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0
        else WORKSPACE_SEARCH_MAX_HITS
    )
    case_sensitive = args.get("case_sensitive") is True
    try:
        request = SearchRequest(query=query, max_results=max_results, case_sensitive=case_sensitive)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:6]
        )
        return error_result(
            clamp_result(
                f"Invalid search — fix and retry: {problems}", WORKSPACE_SEARCH.max_result_bytes
            )
        )
    response = searcher.search(request)
    return text_result(clamp_result(_format_search(response), WORKSPACE_SEARCH.max_result_bytes))


# ---- run_gates --------------------------------------------------------------
#
# The office_reconcile precedent, aimed at the other half of "is this right": a
# session that can prove its own work does not need a human to run the gate for
# it. M6 staged review PR1 — the check is services/gates.py, registered on the
# ValidationService as "gates".
#
# **The schema carries no argv, no cwd and no path**, and that is what makes this
# tool auto-allowed like every other workbench tool without being the shell
# escape ``_AUTO_ALLOWED``'s omission of ``Bash`` and the PreToolUse broker exist
# to prevent: it cannot express an arbitrary command. The gates are a
# server-owned catalog selected by id, and the *checkout* is resolved from the
# calling session's own id — which the bridge closes over — so the tool cannot be
# pointed at another session's work either. ``test_agent_tools.py`` asserts both.


class ToolchainRunner(Protocol):
    """The narrow slice of ``ValidationService`` this tool needs — the same two
    methods :class:`ReconciliationRunner` names, kept as its own protocol so the
    gate tool reads as what it is rather than borrowing reconciliation's name.
    ``run`` dispatches to the registered ``gates`` check; ``payload`` fetches the
    stored :class:`GateLog` the excerpt is read from."""

    async def run(self, spec: ValidationSpec) -> ValidationResult: ...
    def payload(self, kind: EvidenceKind, ref: str) -> BaseModel | None: ...


#: Bytes of the first failing gate's log the tool shows by default. Small on
#: purpose: the common case is a model that needs the summary and the first
#: failure, and the whole log is one ``log_bytes`` away (AXI shape 1). The stored
#: payload is **never** narrowed by this — the human's expander always gets the
#: full captured window.
RUN_GATES_LOG_EXCERPT = 400

#: Clip on one evidence line echoed into the summary, so N gates cannot blow the
#: budget on wording alone.
_GATES_LINE_CLIP = 200

RUN_GATES = AgentToolSpec(
    name="run_gates",
    description=(
        "Prove the change you just wrote: run this project's configured gates "
        "(ruff, mypy, pytest, npm-test) in your own worktree and get each one's "
        "verdict. Optional gates (ids; omit for all) and log_bytes (how much of "
        "the first failing gate's output to show, default 400, max 8192). You "
        "cannot pass a command, a path or a cwd — the gates are server-owned and "
        "the checkout is your session's. Returns one line per gate with its exit "
        "code and duration, then the failing gate's captured output and where to "
        "read next. Says so explicitly when everything passes, and refuses (never "
        "silently passes) when your session holds no worktree."
    ),
    output_format="text",
    # One excerpt of at most MAX_GATE_LOG_BYTES (8,192) plus a header, up to eight
    # per-gate lines clipped to _GATES_LINE_CLIP, and a next-step sentence:
    # 8,192 + 8*200 + ~400 measures under 10,200. 10,240 is that with a margin the
    # budget test pins, and the body is clamped to it as a backstop (it is text
    # the model reads, never JSON it parses, so clamping is safe). Large because
    # the captured output *is* the value of this tool — a failing gate whose log
    # the model cannot read costs a second call to fetch it.
    max_result_bytes=10_240,
    # Two small properties, neither required. Measured near 400 bytes; 560 leaves
    # room to reword a description without a third argument slipping in
    # unmeasured — the schema is paid on every request whether the tool runs.
    max_schema_bytes=560,
    input_schema={
        "type": "object",
        "properties": {
            "gates": {
                "type": "array",
                "items": {"type": "string"},
                # Declared, not merely enforced: a model that can see the cap
                # asks inside it, where one that cannot spends a turn finding out.
                "maxItems": MAX_GATES_PER_RUN,
                "description": "Gate ids to run, e.g. ruff. Omit to run all configured.",
            },
            "log_bytes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_GATE_LOG_BYTES,
                "description": (
                    f"Bytes of the failing gate's output to show (default {RUN_GATES_LOG_EXCERPT})."
                ),
            },
        },
    },
)


def _gate_excerpt(text: str, budget: int) -> tuple[str, int]:
    """The tail of a captured log, bounded, plus how many bytes it holds.

    The **tail**, because that is where ``pytest`` and ``mypy`` put the summary a
    model acts on; the head is already reflected in the evidence line when a gate
    could not start.
    """
    encoded = text.encode()
    if len(encoded) <= budget:
        return text, len(encoded)
    kept = encoded[len(encoded) - budget :].decode(errors="ignore")
    return kept, budget


def _first_location(text: str) -> str | None:
    """A ``path:line`` a model should read next, if the log offers one.

    Deliberately a shallow scan rather than a per-tool parser: ruff, mypy and
    pytest all print ``path:line`` somewhere, and a wrong guess here costs a
    model one glance while a per-tool parser costs every future gate a branch.
    """
    for raw in text.splitlines():
        cleaned = raw.replace("(", " ").replace(")", " ").replace(",", " ")
        for token in cleaned.split():
            # Split on every colon rather than the last: real output ends in
            # ``path:line:`` (ruff) or ``path:line:col:`` (mypy), so a rpartition
            # would keep finding the trailing separator and nothing else.
            parts = token.split(":")
            for index in range(len(parts) - 1):
                head, number = parts[index], parts[index + 1]
                if not number.isdigit() or "." not in head:
                    continue
                if head.rsplit(".", 1)[-1].isalpha():
                    return f"{head}:{number}"
    return None


async def handle_run_gates(
    runner: ToolchainRunner, session_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    """The run_gates tool body, free of SDK imports so it is directly testable.

    All three AXI shapes. A truncated excerpt states its size and names
    ``log_bytes`` as the widener (shape 1); an all-clean run says so in words
    rather than returning a blank a model reads as either clean or broken, and so
    does a refusal (shape 2); a failing run ends by naming the first failing
    location, which is where the model should read next (shape 3).
    """
    raw_gates = args.get("gates")
    # Folded and capped here as well as in ``GateSpec``, because past the cap the
    # model *raises* — and a tool that raises costs the session a whole turn to
    # discover it asked for too much. Repeats fold silently (the same gate twice
    # is the same gate once, over the same unchanged tree); the surplus past the
    # cap is stated in the result rather than dropped, which is AXI shape 1 on an
    # argument instead of on a log.
    wanted = raw_gates if isinstance(raw_gates, list) else []
    named = list(dict.fromkeys(item for item in wanted if isinstance(item, str)))
    gates, surplus = named[:MAX_GATES_PER_RUN], named[MAX_GATES_PER_RUN:]
    raw_bytes = args.get("log_bytes")
    excerpt_budget = (
        min(raw_bytes, MAX_GATE_LOG_BYTES)
        if isinstance(raw_bytes, int) and not isinstance(raw_bytes, bool) and raw_bytes >= 0
        else RUN_GATES_LOG_EXCERPT
    )
    # `log_bytes` sizes what *this result* shows, never what the check captures:
    # narrowing the stored payload would shrink the human's expander to fit a
    # model's budget, which is the wrong trade in the one place a person has to
    # read the evidence themselves.
    spec = ValidationSpec(
        subject=ValidationSubject(kind="session_output", ref=session_id, label=session_id),
        checks=["gates"],
        params=GateSpec(gates=gates).model_dump(),
    )
    result = await runner.run(spec)
    body = _format_gates(result, runner, excerpt_budget)
    if surplus:
        body = (
            f"Only the first {MAX_GATES_PER_RUN} gate ids ran; "
            f"{len(surplus)} more were not ({_clip(', '.join(surplus), 120)}) — "
            "ask for those in a second call.\n"
        ) + body
    return text_result(clamp_result(body, RUN_GATES.max_result_bytes))


def _format_gates(result: ValidationResult, runner: ToolchainRunner, budget: int) -> str:
    """One compact text body: the verdicts, then the first failure in detail."""
    gate_lines = [item for item in result.evidence if item.kind == "gate"]
    if not gate_lines:
        return (
            "No gate evidence was produced — nothing ran and nothing was judged. "
            f"Validation {result.validation_id}."
        )
    skipped = [item for item in gate_lines if item.outcome == "skipped"]
    failed = [item for item in gate_lines if item.outcome == "fail"]
    passed = [item for item in gate_lines if item.outcome == "pass"]

    if not failed and not passed:
        # Every line is a refusal — the no-slot case, and the one place an empty
        # answer would be read as "clean". Say the refusal instead.
        return "\n".join(
            [f"No gates ran ({result.risk})."]
            + [_clip(item.detail, 600) for item in skipped]
            + [f"Validation {result.validation_id}."]
        )

    header = (
        f"All {len(passed)} gates pass ({', '.join(item.label for item in passed)})."
        if not failed
        else f"{len(failed)} of {len(failed) + len(passed)} gates FAIL."
    )
    lines = [header]
    lines += [
        f"{item.outcome}: {_clip(item.detail, _GATES_LINE_CLIP)}"
        for item in gate_lines
        if item.outcome != "pass" or failed
    ]
    if not failed:
        lines.append(f"Validation {result.validation_id} — nothing to fix.")
        return "\n".join(lines)

    first = failed[0]
    payload = runner.payload("gate", first.payload_ref) if first.payload_ref else None
    if isinstance(payload, GateLog):
        excerpt, shown = _gate_excerpt(payload.text, budget)
        total = (
            payload.truncated.total if payload.truncated is not None else len(payload.text.encode())
        )
        lines.append(f"--- {payload.gate} output ---")
        lines.append(excerpt)
        if shown < total:
            lines.append(
                f"[showing {shown} of {total} bytes; pass log_bytes up to "
                f"{MAX_GATE_LOG_BYTES} for more]"
            )
        location = _first_location(excerpt)
        lines.append(
            f"Next: read {location}." if location else f"Next: fix {payload.gate} and re-run."
        )
    else:
        lines.append(f"Next: fix {first.label} and re-run (its log has been evicted).")
    return "\n".join(lines)


# ---- report_findings (the reviewer's only tool) ------------------------------
#
# The *whole* toolset of a ``reviewer`` session, and a separate tuple from
# ``AGENT_TOOLS`` for the same reason the orchestrator's five are: a schema is
# paid on every request of every session, so a tool one kind can never call must
# not sit in another kind's context. Here the separation is also a **safety**
# boundary rather than only a budget one — a reviewer that could see
# ``office_write`` or ``run_command`` is not read-only, whatever the permission
# layer answers afterwards.
#
# The ``present_plan`` shape, deliberately: an agent delivers a *typed artifact*
# by calling a tool, and the server validates it. Parsing findings out of prose
# would make the refutation requirement unenforceable — the one property that
# separates a review from an opinion.


class FindingsReceiver(Protocol):
    """Where a reviewer's ``report_findings`` call lands.

    Implemented by ``services/review.py``'s check, which is holding an awaitable
    for exactly this while the reviewer session runs, and injected — so this
    module never imports the review service, the ``CommandInvoker`` pattern once
    more. ``session_id`` is the *reviewer's* own id, closed over by the bridge,
    so a report can only ever settle the review that commissioned it.

    Returns the sentence the reviewer reads back, or ``None`` when no review is
    waiting on this session — which is a mistake worth naming rather than an
    acknowledgement worth faking.
    """

    def receive_findings(self, session_id: str, report: ReportFindingsRequest) -> str | None: ...


REPORT_FINDINGS = AgentToolSpec(
    name="report_findings",
    description=(
        "Deliver your review as findings. Each needs severity (must_fix, "
        "should_fix, nit), claim (what is wrong, one line) and refutation (the "
        "input or state that breaks it) — a claim with no concrete failure path "
        "is a nit, not a must_fix. Optional file, line, confidence (certain, "
        "likely, possible) and a note about what you could not check. Call it "
        "once, with an empty findings list if the change holds up; that is a "
        "real answer and is recorded as one. You cannot approve or reject "
        "anything — a human reads these."
    ),
    output_format="text",
    # An acknowledgement: the count taken and one sentence. 256 covers it with
    # room for the refusal wording, which is the longer of the two paths.
    max_result_bytes=256,
    # Measured at 1,096 bytes for the finding object's six properties and their
    # enums. 1,400 leaves room to reword a field description without a seventh
    # property arriving unmeasured — and this schema is paid on every request of
    # a reviewer session, which is the only kind that carries it.
    max_schema_bytes=1_400,
    input_schema={
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                # Declared, not merely enforced: a model that can see the cap
                # files inside it, where one that cannot spends a turn finding out.
                "maxItems": MAX_FINDINGS,
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": ["must_fix", "should_fix", "nit"],
                        },
                        "claim": {"type": "string", "description": "What is wrong, one line."},
                        "refutation": {
                            "type": "string",
                            "description": "The input or state that breaks it. Required.",
                        },
                        "file": {"type": "string"},
                        "line": {"type": "integer", "minimum": 1},
                        "confidence": {
                            "type": "string",
                            "enum": ["certain", "likely", "possible"],
                        },
                    },
                    "required": ["severity", "claim", "refutation"],
                },
            },
            "note": {"type": "string", "description": "What you could not check, if anything."},
        },
        "required": ["findings"],
    },
)


def handle_report_findings(
    receiver: FindingsReceiver | None, session_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    """The report_findings tool body, free of SDK imports so it is testable.

    Validation errors come back as tool *errors* rather than exceptions, so the
    reviewer reads them and fixes its own arguments on the next call — which is
    the path a missing ``refutation`` takes, and the reason that requirement is
    enforceable at all.
    """
    if receiver is None:
        # Not reachable from a wired server; said out loud anyway, because a
        # reviewer that believes it filed a report it did not file is the one
        # failure mode of this tool that could look like a clean review.
        return error_result("no review is collecting findings in this server")
    try:
        report = ReportFindingsRequest.model_validate(args)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:6]
        )
        return error_result(
            clamp_result(
                f"Invalid findings — fix and retry: {problems}", REPORT_FINDINGS.max_result_bytes
            )
        )
    acknowledged = receiver.receive_findings(session_id, report)
    if acknowledged is None:
        return error_result("no review is waiting on this session's findings")
    return text_result(clamp_result(acknowledged, REPORT_FINDINGS.max_result_bytes))


# ---- the registry -----------------------------------------------------------

#: Every session's toolset — office_read and office_write included, so every
#: kind can read and edit a docked document; the orchestrator-only tools live in
#: ``ORCHESTRATOR_TOOLS``.
AGENT_TOOLS: tuple[AgentToolSpec, ...] = (
    GET_WORKSPACE_STATE,
    PRESENT_PLAN,
    OFFICE_READ,
    OFFICE_WRITE,
    OFFICE_RECONCILE,
    RUN_COMMAND,
    WORKSPACE_SEARCH,
    RUN_GATES,
)

#: The extra toolset an ``orchestrator`` session carries. Never in the context
#: of a chat or worker session — see the note above the specs.
ORCHESTRATOR_TOOLS: tuple[AgentToolSpec, ...] = (
    SPAWN_WORKER,
    LIST_WORKERS,
    READ_WORKER,
    SEND_TO_WORKER,
    STOP_WORKER,
)

#: The whole toolset a ``reviewer`` session carries: one tool, and it is an
#: *output*. A reviewer reads with the builtin file tools and answers with this;
#: it has no ``office_write``, no ``run_command`` and no ``workspace_search``,
#: which is the difference between a session that is read-only and one that is
#: merely asked to behave. ``services/sdk_factory.py`` is where that becomes
#: true, and ``test_sdk_factory.py`` asserts the exposed names per kind.
REVIEWER_TOOLS: tuple[AgentToolSpec, ...] = (REPORT_FINDINGS,)

#: Everything the budget test must measure. One list, so a tool that ships in
#: none of the tuples is a tool that does not exist rather than one that dodged
#: the ceiling.
ALL_AGENT_TOOLS: tuple[AgentToolSpec, ...] = AGENT_TOOLS + ORCHESTRATOR_TOOLS + REVIEWER_TOOLS


def tools_for(kind: SessionKind) -> tuple[AgentToolSpec, ...]:
    """The toolset this kind of session gets.

    A worker gets the base set: a worker that could spawn workers is a fork bomb
    with a budget attached. A **reviewer replaces** the base set rather than
    adding to it — the one arm of this function that subtracts, and the reason
    ``build_context_bridge`` had to become a selection over this function instead
    of a hardcoded list it only ever appended to.
    """
    if kind == "orchestrator":
        return AGENT_TOOLS + ORCHESTRATOR_TOOLS
    if kind == "reviewer":
        return REVIEWER_TOOLS
    return AGENT_TOOLS


def allowed_tool_names(kind: SessionKind = "chat") -> list[str]:
    """Allow-list entries for this session's own tools — they are not what
    ``can_use_tool`` exists to gate."""
    return [spec.qualified_name for spec in tools_for(kind)]
