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

from workbench_server.models.agents import UiState
from workbench_server.models.office_bridge import CellWindow, DocStructure, WordText
from workbench_server.models.plans import PlanArtifact, plan_input_schema
from workbench_server.services.agent_sessions import PlanAlreadyPendingError, SessionBridge
from workbench_server.services.office_host.a1 import cell_ref, column_letter, parse_cell
from workbench_server.services.office_host.document_bridge import DocumentBridgeError

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


# ---- office_read ------------------------------------------------------------

#: Server-side window bounds, applied before the read reaches the bridge so a
#: single call can never return the whole of a large document. The model may ask
#: for fewer characters but not more; the cell budget is not the model's to set.
#: Sized so a full window stays well inside ``max_result_bytes`` below: Word text
#: is one byte per char here, and Excel at 600 short cells over ~8 columns is a
#: few thousand bytes of TSV — a 2000-row sheet (16k cells) never fits, which is
#: the point of windowing rather than streaming.
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
    ) -> WordText | CellWindow: ...


OFFICE_READ = AgentToolSpec(
    name="office_read",
    description=(
        "Read the LIVE Office document Workbench has docked in a panel — only "
        "documents Workbench opened, named by workspace-relative path. Word: the "
        "body windowed by start_paragraph and max_chars, with a 'paragraphs X-Y "
        "of N' footer. Excel: a TSV grid with an A1 corner header and a 'rows X-Y "
        "of N, cols A-H of Z; pass range=A1:H50 for more' footer; omit sheet to "
        "list the sheets, or pass range to pick a corner. An empty document says "
        "so. Reads include unsaved on-screen edits. If the document is not docked, "
        "it says so and how to open it."
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
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "description": "Word only: cap on characters returned (bounded server-side).",
            },
        },
        "required": ["path"],
    },
)


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
        )
    except DocumentBridgeError as error:
        return error_result(_office_read_refusal(error, path))
    if isinstance(result, WordText):
        return text_result(_format_word(result, path))
    return text_result(_format_excel(result, path))


# ---- the registry -----------------------------------------------------------

AGENT_TOOLS: tuple[AgentToolSpec, ...] = (GET_WORKSPACE_STATE, PRESENT_PLAN, OFFICE_READ)


def allowed_tool_names() -> list[str]:
    """Allow-list entries for every registered tool — the session's own tools
    are not what ``can_use_tool`` exists to gate."""
    return [spec.qualified_name for spec in AGENT_TOOLS]
