"""Plan artifact schema + the present_plan tool body.

The schema is the product contract: agents author plans against it and the UI
renders exactly these node kinds, so both the happy round-trip and every cap
that keeps a card readable are pinned here.
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from workbench_server.models.agents import agent_client_message, agent_server_message
from workbench_server.models.plans import (
    MAX_NODES,
    OptionGroupNode,
    PlanAnnotation,
    PlanArtifact,
    PlanDecision,
    PlanPresented,
    PlanResolved,
    PlanResponse,
    StepListNode,
    node_anchor,
    plan_input_schema,
)
from workbench_server.services.agent_sessions import PlanAlreadyPendingError, SessionBridge
from workbench_server.services.agent_tools import handle_present_plan


def node_variants(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``oneOf`` branches of the node union, one per PlanNode kind."""
    variants: list[dict[str, Any]] = schema["properties"]["nodes"]["items"]["oneOf"]
    return variants


def plan_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Fix the DST boundary in the SE3 bidder",
        "summary": "Two ways to handle the 23/25-hour days.",
        "nodes": [
            {"kind": "markdown", "node_id": "ctx", "text": "The bug bites on **spring forward**."},
            {
                "kind": "option_group",
                "node_id": "approach",
                "prompt": "Which representation?",
                "options": [
                    {
                        "option_id": "local",
                        "label": "Keep local time, tag the fold",
                        "pros": ["Matches market rules"],
                        "cons": ["Every join needs the tag"],
                        "recommended": True,
                    },
                    {
                        "option_id": "utc",
                        "label": "Store UTC, convert at the edges",
                        "pros": ["Simple storage"],
                        "cons": ["Gate closure math gets subtle"],
                    },
                ],
            },
            {
                "kind": "step_list",
                "node_id": "steps",
                "steps": [
                    {
                        "text": "Add a fold-aware index",
                        "file_refs": [{"path": "se3/bidder.py"}, {"path": "se3/calendar.py"}],
                    },
                    {"text": "Backfill the affected settlement periods"},
                ],
            },
            {"kind": "question", "node_id": "q1", "text": "Is the 2025 backfill in scope?"},
        ],
    }
    payload.update(overrides)
    return payload


class TestPlanArtifact:
    def test_round_trip_keeps_every_node_kind(self) -> None:
        artifact = PlanArtifact.model_validate(plan_payload())
        again = PlanArtifact.model_validate_json(artifact.model_dump_json())
        assert [node.kind for node in again.nodes] == [
            "markdown",
            "option_group",
            "step_list",
            "question",
        ]
        group = again.nodes[1]
        assert isinstance(group, OptionGroupNode)
        assert [option.recommended for option in group.options] == [True, False]
        steps = again.nodes[2]
        assert isinstance(steps, StepListNode)
        refs = [ref.path for ref in steps.steps[0].file_refs]
        assert refs == ["se3/bidder.py", "se3/calendar.py"]

    def test_plan_id_is_minted_server_side(self) -> None:
        first = PlanArtifact.model_validate(plan_payload())
        second = PlanArtifact.model_validate(plan_payload())
        assert first.plan_id and first.plan_id != second.plan_id

    def test_node_cap(self) -> None:
        nodes = [
            {"kind": "markdown", "node_id": f"n{i}", "text": "x"} for i in range(MAX_NODES + 1)
        ]
        with pytest.raises(ValidationError, match="at most 15"):
            PlanArtifact.model_validate(plan_payload(nodes=nodes))

    def test_empty_plan_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanArtifact.model_validate(plan_payload(nodes=[]))

    def test_duplicate_node_ids_rejected(self) -> None:
        nodes = [
            {"kind": "markdown", "node_id": "same", "text": "a"},
            {"kind": "question", "node_id": "same", "text": "b"},
        ]
        with pytest.raises(ValidationError, match="node_id must be unique"):
            PlanArtifact.model_validate(plan_payload(nodes=nodes))

    def test_duplicate_option_ids_rejected(self) -> None:
        nodes = [
            {
                "kind": "option_group",
                "node_id": "g",
                "prompt": "pick",
                "options": [
                    {"option_id": "a", "label": "A"},
                    {"option_id": "a", "label": "B"},
                ],
            }
        ]
        with pytest.raises(ValidationError, match="option_id must be unique"):
            PlanArtifact.model_validate(plan_payload(nodes=nodes))

    def test_two_recommended_options_rejected(self) -> None:
        """Accent is spent on exactly one option (DESIGN.md principle 3)."""
        nodes = [
            {
                "kind": "option_group",
                "node_id": "g",
                "prompt": "pick",
                "options": [
                    {"option_id": "a", "label": "A", "recommended": True},
                    {"option_id": "b", "label": "B", "recommended": True},
                ],
            }
        ]
        with pytest.raises(ValidationError, match="at most one option"):
            PlanArtifact.model_validate(plan_payload(nodes=nodes))

    def test_single_option_group_rejected(self) -> None:
        nodes = [
            {
                "kind": "option_group",
                "node_id": "g",
                "prompt": "pick",
                "options": [{"option_id": "a", "label": "A"}],
            }
        ]
        with pytest.raises(ValidationError):
            PlanArtifact.model_validate(plan_payload(nodes=nodes))

    def test_unknown_node_kind_rejected(self) -> None:
        nodes = [{"kind": "html", "node_id": "x", "text": "<script>"}]
        with pytest.raises(ValidationError):
            PlanArtifact.model_validate(plan_payload(nodes=nodes))

    def test_overlong_text_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanArtifact.model_validate(plan_payload(title="x" * 200))

    def test_missing_node_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanArtifact.model_validate(plan_payload(nodes=[{"kind": "markdown", "text": "hi"}]))


class TestPlanInputSchema:
    def test_no_refs_survive_and_plan_id_is_not_offered(self) -> None:
        schema = plan_input_schema()
        assert "$defs" not in schema
        assert "$ref" not in json.dumps(schema)
        assert set(schema["properties"]) == {"title", "summary", "nodes"}
        assert "plan_id" not in schema.get("required", [])

    def test_schema_describes_all_five_node_kinds(self) -> None:
        kinds = {
            variant["properties"]["kind"]["const"] for variant in node_variants(plan_input_schema())
        }
        assert kinds == {"option_group", "step_list", "question", "markdown", "visual"}

    def test_the_scene_graph_inlines_its_leaves_exactly_once(self) -> None:
        """The whole shape of ``visuals.VisualBlock`` is chosen for this number.
        Inlining is per *occurrence*, so a block union of three container kinds
        would put five leaf schemas in the model's context four times over,
        on every request of every session. One container, one copy — and this is
        the test that fails if someone "tidies" it into three."""
        compact = json.dumps(plan_input_schema(), separators=(",", ":"))
        assert compact.count('"const":"table"') == 1

    def test_the_scene_graph_terminates(self) -> None:
        """Non-recursive is not a style note: ``_flatten`` inlines every ref, so
        one recursive edge would never return. Reaching this assertion at all is
        half the test; the other half is that the leaves really are the bottom."""
        visual = next(
            variant
            for variant in node_variants(plan_input_schema())
            if variant["properties"]["kind"]["const"] == "visual"
        )
        block = visual["properties"]["blocks"]["items"]
        leaf_kinds = {
            option["properties"]["kind"]["const"]
            for option in block["properties"]["items"]["items"]["oneOf"]
        }
        assert leaf_kinds == {"table", "chart", "diagram", "code_diff", "metrics"}
        # A leaf's own properties are data, never another block.
        for option in block["properties"]["items"]["items"]["oneOf"]:
            assert "blocks" not in option["properties"]
            assert "items" not in option["properties"] or option["properties"]["kind"]["const"] in {
                "metrics"
            }

    def test_noise_keywords_are_stripped_but_the_title_field_survives(self) -> None:
        """``title`` is both a schema keyword pydantic invents for every field
        and a real field on every visual leaf. Dropping the keyword is worth
        ~1.1 kB of every request; dropping the field would delete a caption from
        the product."""
        schema = plan_input_schema()
        assert "title" not in schema["properties"]["nodes"]["items"]["oneOf"][0]
        table = next(
            option
            for variant in node_variants(schema)
            if variant["properties"]["kind"]["const"] == "visual"
            for option in variant["properties"]["blocks"]["items"]["properties"]["items"]["items"][
                "oneOf"
            ]
            if option["properties"]["kind"]["const"] == "table"
        )
        assert table["properties"]["title"]["maxLength"] == 80
        # A meaningful default stays; an empty one restates `required` and goes.
        assert table["properties"]["columns"]["items"]["properties"]["type"]["default"] == "text"
        assert "default" not in table["properties"]["title"]


class TestWireFrames:
    def test_plan_presented_is_a_server_frame(self) -> None:
        frame = PlanPresented(plan=PlanArtifact.model_validate(plan_payload()))
        parsed = agent_server_message.validate_json(frame.model_dump_json())
        assert isinstance(parsed, PlanPresented)
        assert parsed.plan.title.startswith("Fix the DST")

    def test_plan_resolved_is_a_server_frame(self) -> None:
        parsed = agent_server_message.validate_json(
            PlanResolved(plan_id="p1", verdict="no_decision").model_dump_json()
        )
        assert isinstance(parsed, PlanResolved)
        assert parsed.verdict == "no_decision"

    def test_plan_decision_is_a_client_frame(self) -> None:
        decision = PlanDecision(
            response=PlanResponse(
                plan_id="p1",
                verdict="approve",
                choices={"approach": "local"},
                annotations=[PlanAnnotation(anchor=node_anchor("q1"), text="yes, 2025 only")],
                comment="go",
            )
        )
        parsed = agent_client_message.validate_json(decision.model_dump_json())
        assert isinstance(parsed, PlanDecision)
        assert parsed.response.choices == {"approach": "local"}

    def test_unknown_verdict_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanResponse.model_validate({"plan_id": "p1", "verdict": "maybe"})


# ---- the MCP tool body -------------------------------------------------------


class FakeBridge:
    """SessionBridge stub: records what the tool handed to the session."""

    def __init__(
        self, response: PlanResponse | None = None, raises: Exception | None = None
    ) -> None:
        self._response = response
        self._raises = raises
        self.presented: list[PlanArtifact] = []

    #: The orchestrator toolset acts *as* a session, so the bridge names one.
    session_id = "stub-session"

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        return True

    async def present_plan(self, artifact: PlanArtifact) -> PlanResponse:
        self.presented.append(artifact)
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response


class TestPresentPlanTool:
    async def test_returns_the_typed_response_as_json(self) -> None:
        response = PlanResponse(
            plan_id="p1", verdict="approve", choices={"approach": "local"}, comment="ship it"
        )
        bridge = FakeBridge(response)
        result = await handle_present_plan(bridge, plan_payload())

        assert result.get("is_error") is not True
        assert PlanResponse.model_validate_json(result["content"][0]["text"]) == response
        assert bridge.presented[0].title.startswith("Fix the DST")

    async def test_an_agent_supplied_plan_id_is_replaced(self) -> None:
        """Stripping plan_id from the *schema* is advisory — the model can still
        send one, and the default factory would keep it. It must not: the id is
        in the tool result the agent reads, and echoing it back when re-presenting
        after a 'revise' would produce a card the UI dedupes away (leaving the
        user nothing to answer while the tool blocks for the full timeout)."""
        bridge = FakeBridge(PlanResponse(plan_id="p1", verdict="approve"))
        first = await handle_present_plan(bridge, plan_payload(plan_id="forged-or-echoed"))
        second = await handle_present_plan(bridge, plan_payload(plan_id="forged-or-echoed"))

        assert first.get("is_error") is not True
        assert second.get("is_error") is not True
        minted = [artifact.plan_id for artifact in bridge.presented]
        assert "forged-or-echoed" not in minted
        assert minted[0] != minted[1]  # every presentation is a fresh card
        assert all(plan_id for plan_id in minted)

    async def test_validation_errors_come_back_as_tool_errors(self) -> None:
        """The agent must be able to self-correct, not crash the turn."""
        bridge = FakeBridge(PlanResponse(plan_id="p1", verdict="approve"))
        result = await handle_present_plan(bridge, plan_payload(nodes=[{"kind": "html"}]))

        assert result["is_error"] is True
        assert "Invalid plan" in result["content"][0]["text"]
        assert bridge.presented == []

    async def test_second_pending_plan_is_a_tool_error(self) -> None:
        bridge = FakeBridge(raises=PlanAlreadyPendingError())
        result = await handle_present_plan(bridge, plan_payload())
        assert result["is_error"] is True
        assert "already awaiting" in result["content"][0]["text"]

    def test_fake_bridge_satisfies_the_protocol(self) -> None:
        bridge: SessionBridge = FakeBridge(PlanResponse(plan_id="p1", verdict="approve"))
        assert bridge is not None
