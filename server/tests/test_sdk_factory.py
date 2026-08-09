"""Session isolation, asserted on the objects the SDK is actually handed.

M6 staged review PR 2 gives the adversarial review a **read-only** reviewer
session. The word ``"reviewer"`` in ``SessionKind`` buys almost none of that: the
isolation is three changes to logic every *other* kind runs through, in
``services/sdk_factory.py`` and ``services/permission_broker.py``. This file is
where those changes stop being a claim.

Three things are proven here, and the third is the one that is easy to fake and
easy to get wrong:

1. **The toolset is a selection, per kind, by name.** Before PR 2,
   ``build_context_bridge`` hardcoded a base list for *every* kind and only ever
   added the orchestrator's five — no kind could subtract, and ``tools_for`` was
   not consulted at all. So a reviewer assembled by bolting a branch on the side
   would still have carried ``office_write`` and ``run_command``. The assertions
   below name **exact tool names**, never counts: a count passes while a tool is
   quietly swapped, and it also passes if PR 1's ``run_gates`` is dropped by this
   very refactor — the hazard the plan named out loud.

2. **The builtin tools are gated both ways.** ``allowed_tools`` decides what is
   *auto-approved ahead of* ``can_use_tool``; only ``disallowed_tools`` takes a
   tool out of the model's context. This server passed neither before PR 2, so a
   reviewer would have had ``Write`` and ``Edit`` pre-approved with the deny path
   below never reached.

3. **A reviewer's blocked call wakes nobody.** "It is denied" and "it is denied
   *without waking the user*" are different claims and only the second is the
   feature — a check that runs unattended must not be able to raise a permission
   card on a screen whose owner never asked for a reviewer. So the spies here
   assert **zero awaits** on ``ask_permission``, on *both* escalation paths:
   ``can_use_tool`` and the ``PreToolUse`` broker. Two paths to the user's
   screen, not one, and short-circuiting only the first would leave a reviewer's
   ``Bash`` request still able to raise a card.
"""

from pathlib import Path
from typing import Any

from workbench_server.config import Settings
from workbench_server.models.agents import SessionKind
from workbench_server.models.commands import CommandInvokeResult, CommandManifest
from workbench_server.models.office_bridge import DocStructure
from workbench_server.models.orchestrator import (
    OrchestratorBudget,
    SpawnRefusal,
    WorkerInfo,
)
from workbench_server.models.plans import PlanArtifact, PlanResponse
from workbench_server.models.review import ReportFindingsRequest
from workbench_server.services.agent_tools import (
    AGENT_TOOLS,
    ORCHESTRATOR_TOOLS,
    REVIEWER_TOOLS,
    OrchestratorHandle,
    allowed_tool_names,
    tools_for,
)
from workbench_server.services.permission_broker import (
    UNATTENDED_KINDS,
    build_pre_tool_use_hook,
)
from workbench_server.services.sdk_factory import (
    REVIEWER_DENY_MESSAGE,
    UiStateStore,
    auto_allowed_for,
    build_agent_options,
    build_context_bridge,
    disallowed_for,
    is_unattended,
)

# --------------------------------------------------------------------------- doubles


class _SpyBridge:
    """A ``SessionBridge`` that **counts** escalations instead of answering them.

    ``awaits`` is the number this file exists to hold at zero for a reviewer. It
    is incremented on entry, before any decision, so a call that reached
    ``ask_permission`` at all is caught even if it would have been denied.
    """

    session_id = "spy-session"

    def __init__(self) -> None:
        self.awaits: list[tuple[str, dict[str, Any]]] = []

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        self.awaits.append((tool, tool_input))
        return True

    async def present_plan(self, artifact: PlanArtifact) -> PlanResponse:
        return PlanResponse(plan_id=artifact.plan_id, verdict="approve")


class _Reader:
    """``OfficeDocumentAccess`` stub — enough to build a session's options.

    These four stubs exist only so the SDK wiring constructs; every one of them
    has its behaviour exercised for real elsewhere (``test_office_document_bridge.py``,
    ``test_commands.py``, ``test_office_reconcile.py``, ``test_agent_search.py``).
    What *this* file asserts is which of them a session can see at all.
    """

    async def document_structure(self, path: str) -> DocStructure:
        return DocStructure(kind="word", paragraph_count=0)

    async def read_document(
        self,
        path: str,
        *,
        max_chars: int,
        max_cells: int,
        sheet: str | None = None,
        a1_range: str | None = None,
        start_paragraph: int = 0,
    ) -> Any:  # pragma: no cover - never called here
        raise AssertionError("not exercised in option-building tests")

    async def write_document(
        self,
        path: str,
        *,
        content: str,
        paragraph: int | None = None,
        sheet: str | None = None,
        cell: str | None = None,
    ) -> Any:  # pragma: no cover
        raise AssertionError("not exercised in option-building tests")


class _Commands:
    def manifest(self) -> CommandManifest:
        return CommandManifest()

    def is_registered(self, command_id: str) -> bool:
        return False

    async def invoke(self, command_id: str, params: dict[str, Any]) -> CommandInvokeResult:
        return CommandInvokeResult(
            invocation_id="x", dispatched=False, ok=False, detail="no window"
        )


class _Runner:
    async def run(self, spec: Any) -> Any:  # pragma: no cover - never called here
        raise AssertionError("not exercised in option-building tests")

    def payload(self, kind: Any, ref: str) -> Any:  # pragma: no cover
        return None


class _Searcher:
    def search(self, request: Any) -> Any:  # pragma: no cover - never called here
        raise AssertionError("not exercised in option-building tests")


class _Findings:
    """A ``FindingsReceiver`` that records what a reviewer reported."""

    def __init__(self) -> None:
        self.reports: list[tuple[str, ReportFindingsRequest]] = []

    def receive_findings(self, session_id: str, report: ReportFindingsRequest) -> str | None:
        self.reports.append((session_id, report))
        return "taken"


class _Orchestrator:
    """Enough of an ``OrchestratorHandle`` to make the five closures buildable."""

    async def spawn(
        self, orchestrator_id: str, task: str, base: str | None = None
    ) -> WorkerInfo | SpawnRefusal:  # pragma: no cover - never called here
        raise AssertionError("not exercised in option-building tests")

    def workers_of(self, orchestrator_id: str) -> list[WorkerInfo]:
        return []

    def read(self, orchestrator_id: str, worker_id: str, window: int) -> str:
        return ""

    def send(self, orchestrator_id: str, worker_id: str, text: str) -> str | SpawnRefusal:
        return ""

    async def stop_worker(self, orchestrator_id: str, worker_id: str) -> str:
        return ""

    @property
    def budget(self) -> OrchestratorBudget:
        return OrchestratorBudget(
            max_workers=4,
            max_worker_turns=40,
            max_worker_cost_usd=5.0,
            max_fleet_turns=200,
            max_fleet_cost_usd=20.0,
        )


async def _bridge_names(
    kind: SessionKind, *, orchestrator: OrchestratorHandle | None = None
) -> set[str]:
    """The tool names ``build_context_bridge`` really exposes for a kind.

    Read by **dispatching the MCP server's own ``tools/list`` request**, not off
    ``tools_for`` and not off a list comprehension in the factory. That is the
    whole point: the bug this guards against is a construction that ignores
    ``tools_for``, and asserting ``tools_for`` against itself would have passed
    happily on the code as it stood before PR 2. This is the same answer the
    model gets.
    """
    import mcp.types as types

    server = build_context_bridge(
        UiStateStore(),
        _SpyBridge(),
        _Reader(),
        _Commands(),
        _Runner(),
        _Searcher(),
        kind,
        orchestrator,
        None,
        _Findings(),
    )
    instance = server["instance"]
    handler = instance.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest(method="tools/list"))
    return {tool.name for tool in result.root.tools}


# --------------------------------------------------------------------------- tests


class TestTheToolsetIsSelectedByKind:
    """Names, not counts. A count passes while a tool is swapped."""

    async def test_chat_and_worker_carry_the_base_set_including_run_gates(self) -> None:
        """The regression test for the refactor itself.

        PR 1 appended ``run_gates`` to the hardcoded list PR 2 replaced with this
        selection, and the plan named dropping it as the hazard of doing the two
        lanes in this order: a green suite and a missing tool. So the base set is
        asserted whole, and ``run_gates`` is named again on its own line so the
        failure message says which tool went missing.
        """
        expected = {spec.name for spec in AGENT_TOOLS}
        assert await _bridge_names("chat") == expected
        assert await _bridge_names("worker") == expected
        assert "run_gates" in await _bridge_names("chat")
        assert "run_gates" in await _bridge_names("worker")

    async def test_an_orchestrator_carries_the_base_set_plus_its_five(self) -> None:
        expected = {spec.name for spec in AGENT_TOOLS + ORCHESTRATOR_TOOLS}
        assert await _bridge_names("orchestrator", orchestrator=_Orchestrator()) == expected

    async def test_a_reviewer_carries_report_findings_and_nothing_else(self) -> None:
        """The whole isolation claim, on the object the SDK is handed.

        Not "no write tool" — *nothing else at all*. A reviewer that could still
        see ``office_read`` or ``workspace_search`` would be a different feature
        with a different threat model, and the reason to state it as an equality
        is that every future tool added to ``AGENT_TOOLS`` must fail this test
        rather than quietly join the reviewer's context.
        """
        assert await _bridge_names("reviewer") == {"report_findings"}
        assert {spec.name for spec in REVIEWER_TOOLS} == {"report_findings"}

    async def test_no_other_kind_can_see_report_findings(self) -> None:
        """The converse, and it is a budget claim as well as a safety one: a
        schema every chat session pays for on every request, for a tool it can
        never usefully call."""
        for kind in ("chat", "worker"):
            assert "report_findings" not in await _bridge_names(kind)
        assert "report_findings" not in await _bridge_names(
            "orchestrator", orchestrator=_Orchestrator()
        )

    async def test_the_allow_list_follows_the_same_selection(self) -> None:
        """``allowed_tool_names`` and the bridge read the same function, so a
        reviewer never has an allow entry for a tool it does not carry."""
        assert allowed_tool_names("reviewer") == ["mcp__workbench__report_findings"]
        assert {spec.name for spec in tools_for("reviewer")} == await _bridge_names("reviewer")


class TestTheBuiltinToolsAreGatedBothWays:
    def test_a_reviewer_auto_allows_no_write_or_edit(self) -> None:
        """The gate that had to be *added*: this list was unconditional, and a
        whole-tool entry in it auto-approves ahead of ``can_use_tool``."""
        allowed = auto_allowed_for("reviewer")
        assert "Write" not in allowed
        assert "Edit" not in allowed
        # Still able to read, which is what makes it a reviewer rather than a
        # session that can only stare at the prompt.
        assert {"Read", "Glob", "Grep"} <= set(allowed)

    def test_a_reviewer_disallows_write_edit_and_the_shell(self) -> None:
        disallowed = set(disallowed_for("reviewer"))
        assert {"Write", "Edit", "Bash"} <= disallowed
        # Windows-first: the CLI exposes the shell as ``PowerShell`` there, so
        # naming only ``Bash`` would leave a Windows reviewer's context holding
        # a shell — the same reason ``BROKERED_TOOLS`` names both.
        assert "PowerShell" in disallowed

    def test_every_other_kind_is_untouched(self) -> None:
        """This PR tightens one kind and changes nothing for the others. Asserted
        rather than assumed, because the edit was to shared code."""
        for kind in ("chat", "worker", "orchestrator"):
            assert auto_allowed_for(kind) == ["Read", "Edit", "Write", "Glob", "Grep"]
            assert disallowed_for(kind) == []

    def test_the_options_object_carries_both(self) -> None:
        """On the real ``ClaudeAgentOptions``, not on the helpers: the helpers
        could be right and the wiring still not pass them."""
        options = _options("reviewer")
        assert "Write" not in options.allowed_tools
        assert "Edit" not in options.allowed_tools
        assert {"Write", "Edit", "Bash"} <= set(options.disallowed_tools)

    def test_a_chat_session_still_auto_allows_editing(self) -> None:
        """The ergonomics that must not regress: ordinary editing does not
        prompt, which is what ``allowed_tools`` buys and why dropping
        ``acceptEdits`` cost nothing."""
        options = _options("chat")
        assert {"Read", "Edit", "Write", "Glob", "Grep"} <= set(options.allowed_tools)
        assert options.disallowed_tools == []

    def test_the_reviewer_gets_its_ceilings(self) -> None:
        """``max_turns`` and ``max_budget_usd`` — both unused in this codebase
        until now. A check that spends without a ceiling is one nobody leaves
        switched on."""
        options = _options("reviewer", max_turns=7, max_budget_usd=1.25)
        assert options.max_turns == 7
        assert options.max_budget_usd == 1.25

    def test_other_kinds_keep_the_sdk_defaults(self) -> None:
        options = _options("chat")
        assert options.max_turns is None
        assert options.max_budget_usd is None


class TestBundledSkillsAreGatedForAnUnattendedReviewer:
    """The fourth per-kind gate, and the one M6 PR 2 missed.

    ``auto_allowed_for``, ``disallowed_for`` and ``is_unattended`` were all added
    to tighten the reviewer, but the ``Skill(...)`` allow entries next to them
    stayed unconditional. A whole-tool ``Skill(workbench:remember)`` in
    ``allowed_tools`` auto-approves *ahead of* ``can_use_tool`` exactly like a
    bare ``Write`` does — so an unattended reviewer would silently auto-invoke
    ``workbench:remember`` (whose own trigger names the start of unfamiliar work,
    which every fresh reviewer is), burn a turn out of its cap, and then stall
    because Edit/Write are disallowed: a stalled reviewer recorded as a failed
    review. The gate belongs on the same ``is_unattended`` seam its siblings use.
    """

    def test_a_reviewer_auto_allows_no_bundled_skill(self) -> None:
        options = _options("reviewer")
        # Not a vacuous pass: the *same* ``Settings()`` populates the plugin for a
        # chat session, so a chat session really does carry ``Skill(...)`` entries.
        # A reviewer carrying none is therefore the gate working, not the bundle
        # being absent — which would make this assertion true for the wrong reason.
        assert any(name.startswith("Skill(") for name in _options("chat").allowed_tools)
        assert not any(name.startswith("Skill(") for name in options.allowed_tools)

    def test_every_attended_kind_still_auto_allows_a_bundled_skill(self) -> None:
        """The middle of the change, by name across every attended kind.

        The reviewer test above pins the two endpoints — reviewer denied, chat
        still allowed — but ``is_unattended`` gates all three of the others, and
        chat alone standing in for ``worker`` and ``orchestrator`` is the gap a
        typo widening ``is_unattended`` to ``worker``, or a future kind added on
        the unattended side, would slip through unseen. Its per-kind siblings
        (``test_every_other_kind_is_untouched``, the two-modules-agree pin) all
        loop the attended kinds by name, so this one does too — names, not
        counts, with the kind in the message so a failure says which one lost
        its skills.
        """
        for kind in ("chat", "worker", "orchestrator"):
            allowed = _options(kind).allowed_tools
            assert any(name.startswith("Skill(") for name in allowed), kind

    def test_a_chat_session_still_auto_allows_the_two_named_skills(self) -> None:
        """The ergonomics the skill allows exist to protect must not regress: an
        attended session still opens plan-visual and remember without a prompt."""
        allowed = _options("chat").allowed_tools
        assert "Skill(workbench:plan-visual)" in allowed
        assert "Skill(workbench:remember)" in allowed


def _options(
    kind: SessionKind,
    *,
    bridge: _SpyBridge | None = None,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
) -> Any:
    return build_agent_options(
        UiStateStore(),
        Settings(),
        Path.cwd(),
        None,
        bridge or _SpyBridge(),
        _Reader(),
        _Commands(),
        _Runner(),
        _Searcher(),
        kind,
        _Orchestrator() if kind == "orchestrator" else None,
        None,
        _Findings(),
        max_turns,
        max_budget_usd,
    )


class TestADeniedReviewerWakesNobody:
    """Zero awaits, on both paths. The feature is the *absence* of the prompt."""

    async def test_can_use_tool_denies_without_asking_the_user(self) -> None:
        bridge = _SpyBridge()
        options = _options("reviewer", bridge=bridge)
        decision = await options.can_use_tool("Write", {"file_path": "x.py"}, None)
        # Denied…
        assert type(decision).__name__ == "PermissionResultDeny"
        assert decision.message == REVIEWER_DENY_MESSAGE
        # …and, the half that is actually the feature, nobody was woken. If this
        # list is non-empty a permission card reached a user who never asked for
        # a reviewer, and the validation that started it is parked on a future.
        assert bridge.awaits == []

    async def test_the_brokered_hook_denies_without_asking_the_user(self) -> None:
        """The second door. ``can_use_tool`` is only reached for calls the CLI
        resolves to *ask*; a ``PreToolUse`` hook is dispatched for every call, so
        a short-circuit in one place is a short-circuit in half the places."""
        bridge = _SpyBridge()
        hook = build_pre_tool_use_hook(bridge.ask_permission, bridge.session_id, "reviewer")
        decision = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf ."}}, None, None
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert bridge.awaits == []

    async def test_a_chat_session_still_reaches_the_user(self) -> None:
        """The control. Without this, a bug that denied *everything* everywhere
        would pass the two tests above and break the whole app."""
        bridge = _SpyBridge()
        options = _options("chat", bridge=bridge)
        decision = await options.can_use_tool("Bash", {"command": "ls"}, None)
        assert type(decision).__name__ == "PermissionResultAllow"
        assert len(bridge.awaits) == 1

    async def test_a_worker_still_reaches_the_board(self) -> None:
        """Mission Control's whole permission story: a worker's shell request
        blocks and escalates to a human. It must not have been swept up by the
        unattended branch."""
        bridge = _SpyBridge()
        hook = build_pre_tool_use_hook(bridge.ask_permission, bridge.session_id, "worker")
        await hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)
        assert len(bridge.awaits) == 1

    def test_the_two_modules_agree_on_which_kinds_are_unattended(self) -> None:
        """``permission_broker`` keeps its own copy so it stays SDK-free. That is
        a duplication, so it is pinned: a kind that is unattended on one path and
        not the other is exactly the half-closed door these tests exist for."""
        for kind in ("chat", "worker", "orchestrator", "reviewer"):
            assert is_unattended(kind) == (kind in UNATTENDED_KINDS)


class TestTheSystemPromptMatchesTheToolset:
    def test_a_reviewer_is_not_told_about_tools_it_does_not_have(self) -> None:
        """Naming ``present_plan`` to a session that has no ``present_plan``
        costs it a turn to discover, and the editing advice is the opposite of
        this session's job."""
        append = _options("reviewer").system_prompt["append"]
        assert "present_plan" not in append
        assert "report_findings" in append
        assert "read-only" in append

    def test_every_other_kind_keeps_the_shipped_append(self) -> None:
        append = _options("chat").system_prompt["append"]
        assert "present_plan" in append
        assert "get_workspace_state" in append
