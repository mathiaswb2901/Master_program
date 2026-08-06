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
from pathlib import Path
from typing import Any

from workbench_server.config import Settings
from workbench_server.models.agents import UiState
from workbench_server.models.plans import (
    AnnotationAnchor,
    PlanAnnotation,
    PlanArtifact,
    PlanResponse,
)
from workbench_server.services.agent_tools import (
    AGENT_TOOLS,
    ALL_AGENT_TOOLS,
    GET_WORKSPACE_STATE,
    MAX_DESCRIPTION_CHARS,
    ORCHESTRATOR_TOOLS,
    PRESENT_PLAN,
    allowed_tool_names,
    clamp_result,
    handle_present_plan,
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
        assert [spec.name for spec in AGENT_TOOLS] == ["get_workspace_state", "present_plan"]
        for spec in AGENT_TOOLS:
            # ``output_format``, ``max_result_bytes`` and ``max_schema_bytes``
            # are required fields, so an omission is a type error — this asserts
            # the values are the ones we actually ship.
            assert spec.output_format == "compact-json"
            assert spec.max_result_bytes > 0
            assert spec.max_schema_bytes > 0
            assert isinstance(spec.input_schema, dict)

    def test_the_orchestrator_toolset_is_measured_like_every_other(self) -> None:
        """Mission Control's five tools are a *separate* tuple, so they could
        have shipped unmeasured. ``ALL_AGENT_TOOLS`` is what the budgets below
        iterate, and this is what fails if a third tuple ever appears."""
        assert ALL_AGENT_TOOLS == AGENT_TOOLS + ORCHESTRATOR_TOOLS
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
        options = build_agent_options(UiStateStore(), Settings(), Path.cwd(), None, _Bridge())
        assert set(allowed_tool_names()) <= set(options.allowed_tools)
        assert set(options.mcp_servers) == {"workbench"}

    def test_an_orchestrator_session_allows_its_own_five_and_no_shell(self) -> None:
        options = build_agent_options(
            UiStateStore(), Settings(), Path.cwd(), None, _Bridge(), "orchestrator"
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
