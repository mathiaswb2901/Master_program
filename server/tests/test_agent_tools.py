"""The agent-facing tool registry, and the ergonomics budget it carries.

The budget is a quality-gate concern, not a review comment: a tool description
is loaded into *every* session's context, so it is paid for on every request,
and a result format is paid for on every call. These tests are where that stops
being advice — raising a ceiling takes a diff someone has to justify.

What is deliberately not tested here: latency. These are in-process calls where
the model and the user dominate, so budgeting it would promise a measurement
nobody takes.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.models.agents import UiState
from workbench_server.models.commands import CommandInvokeResult, CommandManifest
from workbench_server.models.gates import MAX_GATE_LOG_BYTES, MAX_GATES_PER_RUN, GateLog
from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    SheetDim,
    WordEdit,
    WordText,
)
from workbench_server.models.plans import (
    AnnotationAnchor,
    PlanAnnotation,
    PlanArtifact,
    PlanResponse,
)
from workbench_server.models.review import MAX_FINDINGS
from workbench_server.models.validation import (
    EvidenceItem,
    EvidenceKind,
    RiskLevel,
    ValidationResult,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.services.agent_tools import (
    AGENT_TOOLS,
    ALL_AGENT_TOOLS,
    GET_WORKSPACE_STATE,
    MAX_DESCRIPTION_CHARS,
    OFFICE_READ,
    OFFICE_READ_MAX_CELLS,
    OFFICE_READ_MAX_CHARS,
    OFFICE_WRITE,
    ORCHESTRATOR_TOOLS,
    PRESENT_PLAN,
    REPORT_FINDINGS,
    REVIEWER_TOOLS,
    RUN_GATES,
    allowed_tool_names,
    clamp_result,
    handle_office_read,
    handle_office_write,
    handle_present_plan,
    handle_report_findings,
    handle_run_gates,
    workspace_state_result,
)
from workbench_server.services.sdk_factory import UiStateStore, build_agent_options


def result_text(result: dict[str, Any]) -> str:
    text: str = result["content"][0]["text"]
    return text


class _Bridge:
    """SessionBridge stub: the user approves, as proposed — and marks two parts.

    Two anchored notes rather than none, because anchors changed the *shape* of
    this result (`{"anchor":{"kind","node_id","path":[…]},"text":…}` is about
    100 bytes of structure before a word is typed) and a budget measured on the
    shape we no longer send is a budget that cannot fail.
    """

    #: The orchestrator toolset acts *as* a session, so the bridge names one.
    session_id = "stub-session"

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        return True

    async def present_plan(self, artifact: PlanArtifact) -> PlanResponse:
        return PlanResponse(
            plan_id=artifact.plan_id,
            verdict="approve",
            choices={"approach": "local"},
            annotations=[
                PlanAnnotation(
                    anchor=AnnotationAnchor(
                        kind="part",
                        node_id="scene",
                        path=["leaf", 2, "row", 1, "col", "Price"],
                    ),
                    text="This price is the second 02:00, not the first.",
                ),
                PlanAnnotation(
                    anchor=AnnotationAnchor(
                        kind="part",
                        node_id="scene",
                        path=["leaf", 1, "series", "SE3 day-ahead", "point", 12],
                    ),
                    text="Negative here is real, keep it.",
                ),
            ],
            comment="Go, but keep the .bak.",
        )


class _Reader:
    """OfficeDocumentAccess stub: canned structure + one read/write result.

    The office bridge's own branches (empty, unknown sheet, gone, windowing) are
    exercised end-to-end against the real service and fake in
    ``test_office_document_bridge.py`` and its write sibling; here it only has to
    stand in so the SDK wiring builds and the result budget is measured against a
    worst-case window.
    """

    def __init__(
        self,
        structure: DocStructure,
        result: WordText | CellWindow | None = None,
        edit: WordEdit | CellEdit | None = None,
    ) -> None:
        self._structure = structure
        self._result = result
        self._edit = edit

    async def document_structure(self, path: str) -> DocStructure:
        return self._structure

    async def read_document(
        self,
        path: str,
        *,
        max_chars: int,
        max_cells: int,
        sheet: str | None = None,
        a1_range: str | None = None,
        start_paragraph: int = 0,
    ) -> WordText | CellWindow:
        assert self._result is not None
        return self._result

    async def write_document(
        self,
        path: str,
        *,
        content: str,
        paragraph: int | None = None,
        sheet: str | None = None,
        cell: str | None = None,
    ) -> WordEdit | CellEdit:
        assert self._edit is not None
        return self._edit


class _Commands:
    """CommandInvoker stub: an empty manifest, so nothing is registered and an
    invoke would report no window. Enough to build a session's options."""

    def manifest(self) -> CommandManifest:
        return CommandManifest()

    def is_registered(self, command_id: str) -> bool:
        return False

    async def invoke(self, command_id: str, params: dict[str, Any]) -> CommandInvokeResult:
        return CommandInvokeResult(
            invocation_id="x", dispatched=False, ok=False, detail="no window"
        )


class _Runner:
    """ReconciliationRunner stub: enough to build a session's options. The tool's
    behavior against a real ValidationService lives in test_office_reconcile.py."""

    async def run(self, spec: Any) -> Any:  # pragma: no cover - never called here
        raise AssertionError("not exercised in option-building tests")

    def payload(self, kind: Any, ref: str) -> Any:  # pragma: no cover
        return None


class _Searcher:
    """WorkspaceSearcher stub: enough to build a session's options. The tool's
    behavior against a real SearchService lives in test_agent_search.py."""

    def search(self, request: Any) -> Any:  # pragma: no cover - never called here
        raise AssertionError("not exercised in option-building tests")


def representative_plan_payload() -> dict[str, Any]:
    """A plan of the size the card was designed for: a choice and some steps."""
    return {
        "title": "Fix the DST boundary in the SE3 bidder",
        "summary": "Two ways to handle the 23/25-hour days.",
        "nodes": [
            {
                "kind": "option_group",
                "node_id": "approach",
                "prompt": "Which representation?",
                "options": [
                    {"option_id": "local", "label": "Local time with tz", "recommended": True},
                    {"option_id": "utc", "label": "UTC everywhere"},
                ],
            },
            {
                "kind": "step_list",
                "node_id": "steps",
                "steps": [
                    {"text": "Normalize the delivery hours", "file_refs": [{"path": "se3/bid.py"}]},
                    {"text": "Backfill the affected days"},
                ],
            },
        ],
    }


def representative_ui_state() -> UiState:
    """A busy-but-ordinary session: a handful of tabs, two of them dirty."""
    return UiState(
        active_file="server/src/workbench_server/services/agent_tools.py",
        open_files=[
            "server/src/workbench_server/services/agent_tools.py",
            "server/src/workbench_server/services/sdk_factory.py",
            "ui/src/registry.ts",
            "ui/src/tools.ts",
            "ROADMAP.md",
            "ARCHITECTURE.md",
        ],
        dirty_files=["ui/src/registry.ts", "ROADMAP.md"],
    )


class TestRegistry:
    def test_every_tool_declares_a_name_schema_and_output_format(self) -> None:
        assert [spec.name for spec in AGENT_TOOLS] == [
            "get_workspace_state",
            "present_plan",
            "office_read",
            "office_write",
            "office_reconcile",
            "run_command",
            "workspace_search",
            "run_gates",
        ]
        for spec in AGENT_TOOLS:
            # ``output_format``, ``max_result_bytes`` and ``max_schema_bytes``
            # are required fields, so an omission is a type error — this asserts
            # every tool ships a sane, bounded value (the format itself is a
            # per-tool choice: office_read is ``text``, the others compact JSON).
            assert spec.output_format in ("compact-json", "text", "markdown")
            assert spec.max_result_bytes > 0
            assert spec.max_schema_bytes > 0
            assert isinstance(spec.input_schema, dict)

    def test_the_orchestrator_toolset_is_measured_like_every_other(self) -> None:
        """Mission Control's five tools are a *separate* tuple, so they could
        have shipped unmeasured. ``ALL_AGENT_TOOLS`` is what the budgets below
        iterate, and this is what fails if a fourth tuple ever appears — as it
        did when M6 PR 2 added ``REVIEWER_TOOLS``, which is exactly the catch
        this assertion exists for."""
        assert ALL_AGENT_TOOLS == AGENT_TOOLS + ORCHESTRATOR_TOOLS + REVIEWER_TOOLS
        for spec in ORCHESTRATOR_TOOLS:
            # Text, not JSON: these results are read by the model, not parsed —
            # an id and a sentence beats an object with three keys of quoting.
            assert spec.output_format == "text"
            assert spec.max_result_bytes > 0
            assert spec.max_schema_bytes > 0

    def test_names_are_unique(self) -> None:
        names = [spec.name for spec in ALL_AGENT_TOOLS]
        assert len(set(names)) == len(names)

    def test_the_allow_list_is_derived_from_the_registry(self) -> None:
        """One place a tool is added. A tool the SDK exposes but the session
        does not allow becomes a permission prompt for our own context bridge."""
        assert allowed_tool_names() == [
            "mcp__workbench__get_workspace_state",
            "mcp__workbench__present_plan",
            "mcp__workbench__office_read",
            "mcp__workbench__office_write",
            "mcp__workbench__office_reconcile",
            "mcp__workbench__run_command",
            "mcp__workbench__workspace_search",
            "mcp__workbench__run_gates",
        ]

    def test_a_chat_session_pays_nothing_for_the_orchestrator_toolset(self) -> None:
        """The reason the two tuples are separate, as a number.

        A schema rides along with **every request**, used or not. Five tools a
        chat session can never call would be that cost on every message every
        user of the app ever sends.
        """
        assert not any("worker" in name for name in allowed_tool_names("chat"))
        extra = sum(spec.schema_bytes + len(spec.description) for spec in ORCHESTRATOR_TOOLS)
        assert extra > 1_000, "if this got small, the split stops being worth its complexity"

    def test_a_session_allows_every_registered_tool(self) -> None:
        """Asserted against the options a real session is built with, so the
        registry and the SDK wiring cannot drift apart silently."""
        options = build_agent_options(
            UiStateStore(),
            Settings(),
            Path.cwd(),
            None,
            _Bridge(),
            _Reader(DocStructure(kind="word", paragraph_count=0)),
            _Commands(),
            _Runner(),
            _Searcher(),
        )
        assert set(allowed_tool_names()) <= set(options.allowed_tools)
        assert set(options.mcp_servers) == {"workbench"}

    def test_an_orchestrator_session_allows_its_own_five_and_no_shell(self) -> None:
        options = build_agent_options(
            UiStateStore(),
            Settings(),
            Path.cwd(),
            None,
            _Bridge(),
            _Reader(DocStructure(kind="word", paragraph_count=0)),
            _Commands(),
            _Runner(),
            _Searcher(),
            "orchestrator",
        )
        assert set(allowed_tool_names("orchestrator")) <= set(options.allowed_tools)
        # The permission story, on the object the SDK is actually handed.
        assert not any("Bash" in name for name in options.allowed_tools)


class TestDescriptionBudget:
    def test_each_description_fits_the_ceiling(self) -> None:
        for spec in ALL_AGENT_TOOLS:
            assert len(spec.description) <= MAX_DESCRIPTION_CHARS, spec.name

    def test_descriptions_are_one_paragraph_of_plain_text(self) -> None:
        """No markdown scaffolding, no examples block: the cheapest description
        that still says what the tool does and what its result means."""
        for spec in ALL_AGENT_TOOLS:
            assert "\n" not in spec.description, spec.name
            assert "```" not in spec.description, spec.name


class TestSchemaBudget:
    """The budget nobody sees until it is huge.

    A description is loaded once per session and a result once per call, but an
    input schema rides along with *every request*, used or not. The scene graph
    made this the binding constraint, and the shape of ``visuals.VisualBlock``
    was chosen against these numbers rather than argued about in review.
    """

    def test_each_input_schema_fits_its_ceiling(self) -> None:
        for spec in ALL_AGENT_TOOLS:
            over = f"{spec.name}: {spec.schema_bytes} bytes of schema"
            assert spec.schema_bytes <= spec.max_schema_bytes, over

    def test_a_tool_that_takes_nothing_advertises_nothing(self) -> None:
        """Two bytes, and a ceiling that fails the day arguments appear here."""
        assert GET_WORKSPACE_STATE.input_schema == {}
        assert GET_WORKSPACE_STATE.schema_bytes == 2

    def test_the_scene_graph_is_most_of_the_plan_schema_and_is_measured(self) -> None:
        """States the split rather than leaving it to a comment: if the five
        original-kind branches or the visual branch move materially, the number
        someone reads in review is this one."""
        schema = PRESENT_PLAN.input_schema
        variants = schema["properties"]["nodes"]["items"]["oneOf"]
        visual = [v for v in variants if v["properties"]["kind"]["const"] == "visual"]
        assert len(visual) == 1
        drawn = len(json.dumps(visual[0], separators=(",", ":")).encode())
        assert 5_000 < drawn < 7_000, drawn
        assert PRESENT_PLAN.schema_bytes < 2 * drawn

    def test_the_schema_carries_no_keyword_that_only_restates_a_name(self) -> None:
        """``title``/``type``-beside-``const``/empty ``default`` are stripped
        (``plans._denoise``) — 1.1 kB of every request, for nothing the model
        can act on. Asserted at the top level, where pydantic always emits one."""
        assert "title" not in PRESENT_PLAN.input_schema
        kind = PRESENT_PLAN.input_schema["properties"]["nodes"]["items"]["oneOf"][0]["properties"][
            "kind"
        ]
        assert set(kind) == {"const"}


class TestResultBudget:
    """Each ceiling is per tool and sized from the payload next to it, so it can
    actually fail. A single global number big enough for the chattiest tool is
    one no other tool can ever exceed — a budget that cannot fail does not
    bind, which is the rule this file exists to keep (CLAUDE.md)."""

    def test_workspace_state_result_is_compact_json(self) -> None:
        text = result_text(workspace_state_result(representative_ui_state()))
        assert len(text.encode()) <= GET_WORKSPACE_STATE.max_result_bytes
        # Compact, not pretty: the indented form of this very payload is
        # materially larger for no gain in what the model can read.
        assert "\n" not in text
        assert ", " not in text
        assert UiState.model_validate_json(text) == representative_ui_state()

    def test_the_compact_form_is_the_cheaper_one(self) -> None:
        """The reason `compact-json` is the default answer, as a number rather
        than an assertion in a comment: same payload, both serializations."""
        state = representative_ui_state()
        compact = len(state.model_dump_json().encode())
        pretty = len(state.model_dump_json(indent=2).encode())
        assert compact < pretty

    async def test_present_plan_result_is_compact_json(self) -> None:
        text = result_text(await handle_present_plan(_Bridge(), representative_plan_payload()))
        assert len(text.encode()) <= PRESENT_PLAN.max_result_bytes
        assert "\n" not in text
        assert PlanResponse.model_validate_json(text).verdict == "approve"

    async def test_an_anchor_reaches_the_agent_as_data_it_can_act_on(self) -> None:
        """The point of the whole anchor design, asserted at the one place the
        agent actually reads: the note comes back naming the row and the column,
        in the agent's own vocabulary — not a selector, not a screen position,
        and not a sentence the agent would have to parse."""
        text = result_text(await handle_present_plan(_Bridge(), representative_plan_payload()))
        note = PlanResponse.model_validate_json(text).annotations[0]
        assert note.anchor.node_id == "scene"
        assert note.anchor.path == ["leaf", 2, "row", 1, "col", "Price"]

    async def test_a_validation_error_stays_within_the_budget(self) -> None:
        """A malformed call must not answer with a wall of pydantic prose — the
        agent reads it, and is charged for every character of it."""
        bad = representative_plan_payload() | {"nodes": [{"kind": "html"}] * 20}
        result = await handle_present_plan(_Bridge(), bad)
        assert result["is_error"] is True
        assert len(result_text(result).encode()) <= PRESENT_PLAN.max_result_bytes

    async def test_a_pathological_validation_error_is_cut_to_the_budget(self) -> None:
        """Ten distinct errors, each naming a long path — the case the ten-error
        cap alone does not bound. Truncation is enforced, not hoped for."""
        bad = representative_plan_payload() | {
            "nodes": [
                {"kind": "step_list", "node_id": f"n{index}" * 20, "steps": "not a list"}
                for index in range(12)
            ]
        }
        result = await handle_present_plan(_Bridge(), bad)
        assert result["is_error"] is True
        assert len(result_text(result).encode()) <= PRESENT_PLAN.max_result_bytes

    def test_clamping_never_exceeds_the_limit_or_splits_a_character(self) -> None:
        """Byte budgets over UTF-8: a cut mid-character must not produce mojibake
        (paths and user comments are not ASCII — `Åsen`, `€/MWh`)."""
        assert clamp_result("short", 64) == "short"
        clamped = clamp_result("Åsen 2 " * 40, 32)
        assert len(clamped.encode()) <= 32
        assert clamped.endswith("…")
        assert "�" not in clamped

    def test_the_argument_schemas_are_the_smallest_that_work(self) -> None:
        """A tool that takes nothing advertises nothing: an empty schema is one
        line of context instead of a properties block the model must read."""
        assert GET_WORKSPACE_STATE.input_schema == {}
        assert PRESENT_PLAN.input_schema["properties"].keys() >= {"title", "nodes"}
        assert "plan_id" not in PRESENT_PLAN.input_schema["properties"]


class TestOfficeReadBudget:
    """office_read is ``text``, and its result is bounded by the window it reads,
    not by the size of the document. These pin that a worst-case window — the
    widest Word body and the largest Excel window the server-side caps allow —
    stays inside the tool's declared ceiling, so a real 2000-row sheet cannot."""

    def test_the_office_read_schema_fits_its_ceiling(self) -> None:
        assert OFFICE_READ.schema_bytes <= OFFICE_READ.max_schema_bytes
        assert OFFICE_READ.input_schema["required"] == ["path"]

    async def test_a_full_word_window_stays_within_budget(self) -> None:
        body = "Para. " * 1000  # ~6 kB, the max_chars ceiling's worth of body
        reader = _Reader(
            DocStructure(kind="word", paragraph_count=200),
            WordText(start_paragraph=0, returned_chars=len(body), total_paragraphs=200, text=body),
        )
        result = await handle_office_read(reader, {"path": "notes.docx"})
        assert len(result_text(result).encode()) <= OFFICE_READ.max_result_bytes

    async def test_a_full_excel_window_stays_within_budget(self) -> None:
        cols = 8
        rows = OFFICE_READ_MAX_CELLS // cols
        cells = [[f"{r}.{c}" for c in range(cols)] for r in range(rows)]
        reader = _Reader(
            DocStructure(kind="excel", sheets=[SheetDim(name="Forecast", rows=2000, cols=cols)]),
            CellWindow(
                sheet="Forecast",
                a1_range=f"A1:{'H'}{rows}",
                rows=rows,
                cols=cols,
                total_rows=2000,
                total_cols=cols,
                cells=cells,
            ),
        )
        result = await handle_office_read(reader, {"path": "forecast.xlsx", "sheet": "Forecast"})
        text = result_text(result)
        assert len(text.encode()) <= OFFICE_READ.max_result_bytes
        # AXI shape 1: a windowed read states what it did not show and how to widen.
        assert "of 2000" in text
        assert "range=" in text

    async def test_non_ascii_word_body_is_clamped_to_the_byte_budget(self) -> None:
        # The char cap (6000) is not a byte cap: dense multibyte body — emoji here,
        # but Norwegian æ/ø/å is the routine case — runs to several bytes per
        # character, so a full window serializes well past max_result_bytes. The
        # tool must clamp it, not hand the model an oversized wall of text.
        body = "⚡" * OFFICE_READ_MAX_CHARS  # 3 bytes/char -> ~18 kB, over the 8 kB cap
        assert len(body.encode()) > OFFICE_READ.max_result_bytes
        reader = _Reader(
            DocStructure(kind="word", paragraph_count=1),
            WordText(start_paragraph=0, returned_chars=len(body), total_paragraphs=1, text=body),
        )
        result = await handle_office_read(reader, {"path": "notat.docx"})
        assert len(result_text(result).encode()) <= OFFICE_READ.max_result_bytes

    async def test_one_long_excel_cell_is_clamped_to_the_byte_budget(self) -> None:
        # A single long cell (a notes column, up to 32k chars) blows the TSV past
        # the ceiling on its own, regardless of the other 599 cells the count cap
        # allows. The tool clamps the rendered grid to its byte budget.
        giant = "Åsen 2 " * 5_000
        reader = _Reader(
            DocStructure(kind="excel", sheets=[SheetDim(name="Notes", rows=1, cols=1)]),
            CellWindow(
                sheet="Notes",
                a1_range="A1:A1",
                rows=1,
                cols=1,
                total_rows=1,
                total_cols=1,
                cells=[[giant]],
            ),
        )
        result = await handle_office_read(reader, {"path": "notes.xlsx", "sheet": "Notes"})
        assert len(result_text(result).encode()) <= OFFICE_READ.max_result_bytes


class TestOfficeWriteBudget:
    """office_write is ``text``, and its confirmation is a single sentence — an
    address, a char count, a short echo and the read-back hint. These pin that
    the confirmation stays inside the declared ceiling even when the content
    written is far larger than the tool would ever echo, and that the schema fits
    the ceiling paid on every request."""

    def test_the_office_write_schema_fits_its_ceiling(self) -> None:
        assert OFFICE_WRITE.schema_bytes <= OFFICE_WRITE.max_schema_bytes
        assert OFFICE_WRITE.input_schema["required"] == ["path", "content"]

    async def test_a_huge_content_write_confirms_within_budget(self) -> None:
        # The content the model sends can be a whole 32k-char Excel cell; the
        # confirmation must not quote it back in full — it echoes a short preview
        # and stays inside max_result_bytes regardless of what was written.
        giant = "Åsen 2 " * 5_000
        reader = _Reader(
            DocStructure(kind="excel", sheets=[SheetDim(name="Notes", rows=1, cols=1)]),
            edit=CellEdit(sheet="Notes", a1_cell="A1", written_chars=len(giant)),
        )
        result = await handle_office_write(
            reader, {"path": "notes.xlsx", "sheet": "Notes", "cell": "A1", "content": giant}
        )
        text = result_text(result)
        assert len(text.encode()) <= OFFICE_WRITE.max_result_bytes
        # AXI shape 3: the confirmation ends with the read-back next step.
        assert "office_read" in text

    async def test_a_word_confirmation_stays_within_budget(self) -> None:
        body = "Para. " * 1000  # ~6 kB of content the model sent
        reader = _Reader(
            DocStructure(kind="word", paragraph_count=5),
            edit=WordEdit(paragraph=2, written_chars=len(body), total_paragraphs=5),
        )
        result = await handle_office_write(
            reader, {"path": "notes.docx", "paragraph": 2, "content": body}
        )
        assert len(result_text(result).encode()) <= OFFICE_WRITE.max_result_bytes

    async def test_a_confirmation_over_budget_is_clamped_without_mojibake(self) -> None:
        # The echo is capped at OFFICE_WRITE_ECHO_CHARS, but the confirmation
        # interpolates the *path* verbatim, so a pathological path (deep,
        # non-ASCII) pushes the sentence past the ceiling. This forces
        # clamp_result's truncation branch on the write path — the mirror of the
        # read path's forced-overflow tests — and asserts the byte-budget
        # backstop holds and a cut mid-character produces no mojibake.
        path = "Åsen 2/" * 100 + "notes.xlsx"  # ~800 chars, over the 512-byte cap
        edit = CellEdit(sheet="Notes", a1_cell="A1", written_chars=7)
        reader = _Reader(
            DocStructure(kind="excel", sheets=[SheetDim(name="Notes", rows=1, cols=1)]),
            edit=edit,
        )
        result = await handle_office_write(
            reader, {"path": path, "sheet": "Notes", "cell": "A1", "content": "Åsen 2"}
        )
        text = result_text(result)
        assert len(text.encode()) <= OFFICE_WRITE.max_result_bytes
        assert text.endswith("…")  # proof the truncation branch actually engaged
        assert "�" not in text


class _GateService:
    """A ``ToolchainRunner`` stub: the real ``ValidationService`` narrowed to the
    two methods the tool uses, holding one canned result and its payloads."""

    def __init__(self, result: ValidationResult, payloads: dict[str, GateLog]) -> None:
        self.result = result
        self.payloads = payloads
        self.specs: list[ValidationSpec] = []

    async def run(self, spec: ValidationSpec) -> ValidationResult:
        self.specs.append(spec)
        return self.result

    def payload(self, kind: EvidenceKind, ref: str) -> BaseModel | None:
        return self.payloads.get(ref)


def gate_result(evidence: list[EvidenceItem], risk: RiskLevel = "high") -> ValidationResult:
    return ValidationResult(
        validation_id="val_gates1",
        subject=ValidationSubject(kind="session_output", ref="wrk_1", label="wrk_1"),
        risk=risk,
        evidence=evidence,
        summary="summary",
        created_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
    )


def failing_gates(log_text: str) -> _GateService:
    """Three clean gates and a failing ``pytest`` whose log is behind a ref."""
    log = GateLog(
        gate="pytest",
        argv=["uv", "run", "pytest", "-q"],
        exit_code=1,
        duration_ms=12_400,
        text=log_text,
    )
    evidence = [
        EvidenceItem(kind="gate", label="ruff check .", outcome="pass", detail="ruff: exit 0."),
        EvidenceItem(kind="gate", label="mypy --strict", outcome="pass", detail="mypy: exit 0."),
        EvidenceItem(
            kind="gate",
            label="pytest -q",
            outcome="fail",
            detail="pytest -q: exit 1 in 12.4s, 2140 bytes of output captured — open the log.",
            payload_ref="gate_abc",
        ),
        EvidenceItem(kind="gate", label="npm run test (ui)", outcome="pass", detail="npm: exit 0."),
    ]
    return _GateService(gate_result(evidence), {"gate_abc": log})


class TestRunGatesBudget:
    """``run_gates`` is the session proving its own work, and it is the widest
    result in the registry on purpose — a failing gate whose captured output the
    model cannot read costs a second call to fetch it. So the ceiling is sized
    for one whole 8 KiB log and pinned here."""

    def test_the_schema_carries_no_argv_no_cwd_and_no_path(self) -> None:
        """The reason this tool can be auto-allowed like every other workbench
        tool without being the shell escape ``_AUTO_ALLOWED``'s omission of
        ``Bash`` exists to prevent: it *cannot express a command*."""
        properties = RUN_GATES.input_schema["properties"]
        assert set(properties) == {"gates", "log_bytes"}
        for forbidden in ("argv", "cwd", "path", "command", "session_id", "slot"):
            assert forbidden not in properties
        assert "required" not in RUN_GATES.input_schema
        assert properties["gates"]["items"] == {"type": "string"}

    def test_the_description_and_schema_fit_their_ceilings(self) -> None:
        assert len(RUN_GATES.description) <= MAX_DESCRIPTION_CHARS
        assert RUN_GATES.schema_bytes <= RUN_GATES.max_schema_bytes

    async def test_the_tool_names_the_session_it_was_called_from(self) -> None:
        """The slot is resolved from ``bridge.session_id``, never from ``args`` —
        so the tool cannot be pointed at another session's checkout. Asserted on
        the spec the tool builds, which is the only place that choice is made."""
        service = failing_gates("1 failed\n")
        await handle_run_gates(service, "wrk_42", {"path": "../elsewhere", "cwd": "/"})
        assert service.specs[0].subject.ref == "wrk_42"
        assert service.specs[0].checks == ["gates"]
        assert set(service.specs[0].params) == {"gates", "log_bytes"}

    async def test_a_failing_run_shows_the_log_and_ends_with_where_to_read(self) -> None:
        """AXI shape 3: the last line is the next action, not a full stop."""
        service = failing_gates(
            "server/tests/test_dispatch.py:118: assert 17 == 18\n1 failed, 118 passed\n"
        )
        text = result_text(await handle_run_gates(service, "wrk_1", {"log_bytes": 4_000}))
        assert "1 of 4 gates FAIL" in text
        assert "1 failed, 118 passed" in text
        assert text.rstrip().endswith("Next: read server/tests/test_dispatch.py:118.")
        assert len(text.encode()) <= RUN_GATES.max_result_bytes

    async def test_a_truncated_excerpt_states_its_size_and_names_the_widener(self) -> None:
        """AXI shape 1: a capped window says how much was cut and which argument
        widens it. Silence is what turns one call into three."""
        service = failing_gates("x" * 2_140)
        text = result_text(await handle_run_gates(service, "wrk_1", {}))
        assert "showing 400 of 2140 bytes" in text
        assert "log_bytes" in text
        assert len(text.encode()) <= RUN_GATES.max_result_bytes

    async def test_the_widest_possible_result_stays_within_the_ceiling(self) -> None:
        """The worst case the check can hand this tool: a full 8 KiB captured log
        asked for in full, on top of four evidence lines."""
        service = failing_gates("E   " * 2_048)
        text = result_text(
            await handle_run_gates(service, "wrk_1", {"log_bytes": MAX_GATE_LOG_BYTES})
        )
        assert len(text.encode()) <= RUN_GATES.max_result_bytes

    async def test_a_clean_run_says_so_explicitly(self) -> None:
        """AXI shape 2: an all-green answer must say it is green, because a blank
        result is one a model reads as either clean or broken."""
        evidence = [
            EvidenceItem(
                kind="gate", label=label, outcome="pass", detail=f"{label}: exit 0 in 1.0s."
            )
            for label in ("ruff check .", "mypy --strict", "pytest -q", "npm run test (ui)")
        ]
        service = _GateService(gate_result(evidence, risk="pass"), {})
        text = result_text(await handle_run_gates(service, "wrk_1", {}))
        assert text.startswith("All 4 gates pass (ruff check .")
        assert "nothing to fix" in text

    async def test_a_session_with_no_slot_gets_the_refusal_not_an_empty_result(self) -> None:
        """The other half of shape 2, and the one that matters most: a refusal
        read as "clean" is exactly the silent green this milestone kills."""
        service = _GateService(
            gate_result(
                [
                    EvidenceItem(
                        kind="gate",
                        label="toolchain gates",
                        outcome="skipped",
                        detail="this session holds no worktree slot; gates run in the checkout…",
                    )
                ],
                risk="low",
            ),
            {},
        )
        text = result_text(await handle_run_gates(service, "wrk_1", {}))
        assert text.startswith("No gates ran")
        assert "holds no worktree slot" in text

    async def test_an_evicted_log_is_said_out_loud(self) -> None:
        service = failing_gates("gone")
        service.payloads.clear()
        text = result_text(await handle_run_gates(service, "wrk_1", {}))
        assert "evicted" in text

    def test_the_schema_declares_the_cap_rather_than_only_enforcing_it(self) -> None:
        """Every id in ``gates`` buys a whole toolchain run, so the list is
        bounded — and a model that can *see* the bound asks inside it, where one
        that cannot spends a turn discovering it."""
        assert RUN_GATES.input_schema["properties"]["gates"]["maxItems"] == MAX_GATES_PER_RUN
        assert RUN_GATES.schema_bytes <= RUN_GATES.max_schema_bytes

    async def test_the_same_gate_asked_for_fifty_times_is_asked_for_once(self) -> None:
        """``["pytest"] * 50`` would otherwise be fifty serial ``pytest`` runs —
        hours — over one unchanged tree, holding the session's slot throughout."""
        service = failing_gates("1 failed\n")
        await handle_run_gates(service, "wrk_1", {"gates": ["pytest"] * 50})
        assert service.specs[0].params["gates"] == ["pytest"]

    async def test_ids_past_the_cap_are_stated_not_silently_dropped(self) -> None:
        """AXI shape 1 on an argument instead of on a log: say what was cut and
        how to get the rest. The tool clips rather than raising, because a raise
        costs the session a whole turn to learn it asked for too much."""
        service = failing_gates("1 failed\n")
        asked = [f"gate-{index}" for index in range(MAX_GATES_PER_RUN + 3)]
        text = result_text(await handle_run_gates(service, "wrk_1", {"gates": asked}))

        assert service.specs[0].params["gates"] == asked[:MAX_GATES_PER_RUN]
        assert f"Only the first {MAX_GATES_PER_RUN} gate ids ran" in text
        assert "3 more were not" in text
        assert "second call" in text
        assert len(text.encode()) <= RUN_GATES.max_result_bytes


class TestReportFindings:
    """The reviewer's only tool — M6 staged review PR 2.

    The three ceilings every agent-facing tool owes, plus the one property that
    separates a review from an opinion: a finding must name the input that
    breaks the change, and the *schema* is where that stops being advice.
    """

    def test_the_three_ceilings_are_declared_and_met(self) -> None:
        assert len(REPORT_FINDINGS.description) <= MAX_DESCRIPTION_CHARS
        assert REPORT_FINDINGS.schema_bytes <= REPORT_FINDINGS.max_schema_bytes
        assert REVIEWER_TOOLS == (REPORT_FINDINGS,)

    def test_the_schema_requires_a_refutation(self) -> None:
        """Declared to the model, not only enforced on the way in: a reviewer
        that can *see* the requirement writes one, where one that cannot spends
        a turn being told."""
        item = REPORT_FINDINGS.input_schema["properties"]["findings"]["items"]
        assert set(item["required"]) == {"severity", "claim", "refutation"}
        assert item["properties"]["severity"]["enum"] == ["must_fix", "should_fix", "nit"]
        assert REPORT_FINDINGS.input_schema["properties"]["findings"]["maxItems"] == MAX_FINDINGS

    def test_a_representative_report_fits_its_result_budget(self) -> None:
        """Sized from the measured payload: the acknowledgement is a count and
        one sentence, and the budget fails the day it becomes an essay."""
        receiver = _Findings()
        result = handle_report_findings(
            receiver,
            "rev-1",
            {
                "findings": [
                    {
                        "severity": "must_fix",
                        "file": "server/x.py",
                        "line": 12,
                        "claim": "Gate closure is compared in local time.",
                        "refutation": "At 02:30 on the spring-forward date the naive "
                        "stamp lands an hour early and the bid misses the window.",
                        "confidence": "likely",
                    }
                ]
            },
        )
        text = result_text(result)
        assert "Recorded 1 finding(s)" in text
        assert len(text.encode()) <= REPORT_FINDINGS.max_result_bytes
        assert len(receiver.taken) == 1

    def test_a_finding_with_no_refutation_is_a_tool_error_the_model_can_fix(self) -> None:
        """Not an exception: the reviewer reads this and fixes its own arguments
        on the next call, which is the whole reason the requirement is
        enforceable at all."""
        result = handle_report_findings(
            _Findings(), "rev-1", {"findings": [{"severity": "must_fix", "claim": "bad"}]}
        )
        assert result["is_error"] is True
        text = result_text(result)
        assert "refutation" in text
        assert len(text.encode()) <= REPORT_FINDINGS.max_result_bytes

    def test_an_empty_report_is_a_real_answer(self) -> None:
        """AXI shape 2 from the other side: "I found nothing" must be sayable,
        and must be distinguishable from a reviewer that never answered."""
        text = result_text(handle_report_findings(_Findings(), "rev-1", {"findings": []}))
        assert "Recorded 0 finding(s)" in text

    def test_findings_nobody_is_waiting_for_are_an_error_not_an_ack(self) -> None:
        """A reviewer that believes it filed a report it did not file is the one
        failure mode of this tool that could look like a clean review."""
        result = handle_report_findings(_Unclaimed(), "rev-1", {"findings": []})
        assert result["is_error"] is True

    def test_the_tool_cannot_approve_anything(self) -> None:
        """The description says so to the model, and the schema gives it no way
        to try: no verdict field, no approval field, no severity above
        ``must_fix``."""
        properties = REPORT_FINDINGS.input_schema["properties"]
        assert set(properties) == {"findings", "note"}
        assert "approve" in REPORT_FINDINGS.description


class _Findings:
    """A ``FindingsReceiver`` that takes what it is given."""

    def __init__(self) -> None:
        self.taken: list[tuple[str, Any]] = []

    def receive_findings(self, session_id: str, report: Any) -> str | None:
        self.taken.append((session_id, report))
        return (
            f"Recorded {len(report.findings)} finding(s). A human reviews them and decides; "
            "nothing is approved by this call. You are done — stop here."
        )


class _Unclaimed:
    """A receiver with no review waiting — the mis-routed report."""

    def receive_findings(self, session_id: str, report: Any) -> str | None:
        return None
