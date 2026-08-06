"""Agent core tests: streaming, permissions, index, and the full WS pipeline —
all against a scripted fake SDK client (the real SDK is exercised by the
live smoke test in test_live_agent.py)."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server import main
from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.agents import (
    AgentError,
    PermissionRequest,
    SessionKind,
    TextDelta,
    ToolSettled,
    ToolUseNote,
    TurnDone,
)
from workbench_server.models.plans import (
    FileRef,
    OptionGroupNode,
    PlanAnnotation,
    PlanArtifact,
    PlanOption,
    PlanPresented,
    PlanResolved,
    PlanResponse,
    PlanStep,
    StepListNode,
    node_anchor,
)
from workbench_server.services import agent_sessions
from workbench_server.services.agent_sessions import (
    PlanAlreadyPendingError,
    SessionBridge,
    SessionManager,
    TooManySessionsError,
)
from workbench_server.services.session_index import SessionIndex, encode_project_dir
from workbench_server.services.titles import derive_title

# ---- fake SDK message types (duck-typed on class name) ----------------------


class StreamEvent:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event


class ToolUseBlock:
    def __init__(self, name: str, tool_input: dict[str, Any], block_id: str = "") -> None:
        self.name = name
        self.input = tool_input
        if block_id:
            self.id = block_id


class ToolResultBlock:
    def __init__(self, tool_use_id: str, content: Any, is_error: bool = False) -> None:
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class UserMessage:
    """The SDK's carrier for tool results — not something the human typed."""

    def __init__(self, content: list[Any]) -> None:
        self.content = content


class ResultMessage:
    def __init__(self, session_id: str = "sdk-abc", cost: float = 0.01) -> None:
        self.session_id = session_id
        self.total_cost_usd = cost
        self.is_error = False


def delta(text: str) -> StreamEvent:
    return StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    )


class FakeClient:
    """Yields a scripted message sequence; optionally asks permission or presents
    a plan first — both through the same SessionBridge the real SDK factory gets."""

    def __init__(
        self,
        script: list[Any],
        bridge: SessionBridge,
        ask_for: str | None = None,
        plan: PlanArtifact | None = None,
    ) -> None:
        self._script = script
        self._bridge = bridge
        self._ask_for = ask_for
        self._plan = plan
        self.prompts: list[str] = []
        self.permission_outcomes: list[bool] = []
        self.plan_responses: list[PlanResponse] = []
        self.disconnected = False

    async def connect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        if self._ask_for is not None:
            outcome = await self._bridge.ask_permission(self._ask_for, {"command": "pip install x"})
            self.permission_outcomes.append(outcome)
        if self._plan is not None:
            self.plan_responses.append(await self._bridge.present_plan(self._plan))
        for message in self._script:
            yield message

    async def interrupt(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


def make_factory(
    script: list[Any], ask_for: str | None = None, plan: PlanArtifact | None = None
) -> Any:
    created: list[FakeClient] = []

    def factory(
        folder: Path, resume: str | None, bridge: SessionBridge, kind: SessionKind = "chat"
    ) -> FakeClient:
        client = FakeClient(script, bridge, ask_for, plan)
        created.append(client)
        return client

    factory.created = created  # type: ignore[attr-defined]
    return factory


async def drain(queue: asyncio.Queue[BaseModel], until: type[BaseModel]) -> list[BaseModel]:
    events: list[BaseModel] = []
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=10)
        events.append(event)
        if isinstance(event, until):
            return events


# ---- streaming --------------------------------------------------------------


async def test_turn_streams_deltas_tools_and_done(tmp_path: Path) -> None:
    script = [
        delta("Hei "),
        delta("verden"),
        AssistantMessage([ToolUseBlock("Edit", {"file_path": "se3/model.py"})]),
        ResultMessage(session_id="sdk-123", cost=0.05),
    ]
    manager = SessionManager(tmp_path, make_factory(script), max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("fix the model")
    events = await drain(queue, TurnDone)

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["Hei ", "verden"]
    tools = [e for e in events if isinstance(e, ToolUseNote)]
    assert tools[0].tool == "Edit"
    assert "se3/model.py" in tools[0].summary
    done = events[-1]
    assert isinstance(done, TurnDone)
    assert done.cost_usd == 0.05
    assert session.sdk_session_id == "sdk-123"  # captured for future resume


async def test_each_tool_row_settles_on_its_own_result(tmp_path: Path) -> None:
    """Rows used to flip in bulk at turn_done, so a Read that failed 30 seconds
    ago looked fine until the turn ended. Each result settles exactly one row."""
    script = [
        AssistantMessage([ToolUseBlock("Read", {"file_path": "a.py"}, block_id="tu-1")]),
        AssistantMessage([ToolUseBlock("Bash", {"command": "pytest"}, block_id="tu-2")]),
        UserMessage([ToolResultBlock("tu-1", [{"type": "text", "text": "VERSION = 2"}])]),
        UserMessage([ToolResultBlock("tu-2", "boom", is_error=True)]),
        ResultMessage(),
    ]
    manager = SessionManager(tmp_path, make_factory(script), max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("go")
    events = await drain(queue, TurnDone)

    notes = [e for e in events if isinstance(e, ToolUseNote)]
    assert [note.id for note in notes] == ["tu-1", "tu-2"]
    settled = [e for e in events if isinstance(e, ToolSettled)]
    assert [(s.id, s.ok) for s in settled] == [("tu-1", True), ("tu-2", False)]
    assert settled[0].output_excerpt == "VERSION = 2"
    assert settled[1].output_excerpt == "boom"


async def test_tool_ids_are_minted_when_the_sdk_gives_none(tmp_path: Path) -> None:
    """A block without an id still produces a valid frame (the row then settles
    at turn_done like before) — and two such rows never share an id."""
    script = [
        AssistantMessage(
            [ToolUseBlock("Read", {"file_path": "a.py"}), ToolUseBlock("Read", {"path": "b.py"})]
        ),
        ResultMessage(),
    ]
    manager = SessionManager(tmp_path, make_factory(script), max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("go")
    events = await drain(queue, TurnDone)

    ids = [e.id for e in events if isinstance(e, ToolUseNote)]
    assert len(ids) == 2
    assert all(i for i in ids)
    assert len(set(ids)) == 2


async def test_tool_output_excerpt_is_capped(tmp_path: Path) -> None:
    """A Grep over a monorepo is not a chat frame."""
    script = [
        AssistantMessage([ToolUseBlock("Grep", {"pattern": "x"}, block_id="tu-1")]),
        UserMessage([ToolResultBlock("tu-1", "y" * 10_000)]),
        ResultMessage(),
    ]
    manager = SessionManager(tmp_path, make_factory(script), max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("go")
    events = await drain(queue, TurnDone)

    settled = next(e for e in events if isinstance(e, ToolSettled))
    assert len(settled.output_excerpt) == agent_sessions.TOOL_EXCERPT_LIMIT + 1
    assert settled.output_excerpt.endswith("…")


async def test_second_message_rejected_while_working(tmp_path: Path) -> None:
    gate: asyncio.Event = asyncio.Event()

    class SlowClient(FakeClient):
        async def receive_response(self) -> AsyncIterator[Any]:
            await gate.wait()
            yield ResultMessage()

    def factory(
        folder: Path, resume: str | None, bridge: SessionBridge, kind: SessionKind = "chat"
    ) -> SlowClient:
        return SlowClient([], bridge)

    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("first")
    await asyncio.sleep(0.05)
    session.send_user_message("second")  # must be rejected, not queued
    gate.set()
    events = await drain(queue, TurnDone)
    assert any(type(e).__name__ == "AgentError" for e in events)


# ---- permissions -------------------------------------------------------------


@pytest.mark.parametrize("allow", [True, False])
async def test_permission_round_trip(tmp_path: Path, allow: bool) -> None:
    factory = make_factory([ResultMessage()], ask_for="Bash")
    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("install deps")

    request: PermissionRequest | None = None
    while request is None:
        event = await asyncio.wait_for(queue.get(), timeout=10)
        if isinstance(event, PermissionRequest):
            request = event
    assert request.tool == "Bash"
    assert "pip install x" in request.description
    assert session.state == "needs_attention"

    session.resolve_permission(request.request_id, allow)
    await drain(queue, TurnDone)
    client: FakeClient = factory.created[0]
    assert client.permission_outcomes == [allow]


async def test_interrupt_settles_a_pending_permission(tmp_path: Path) -> None:
    """Stop must unblock can_use_tool as well as a plan: the SDK interrupt cannot
    reach a tool call parked on our future, so an unsettled prompt would leave
    the session in needs_attention for the rest of the 10-minute timeout."""
    factory = make_factory([ResultMessage()], ask_for="Bash")
    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("install deps")
    while not isinstance(await asyncio.wait_for(queue.get(), timeout=10), PermissionRequest):
        pass

    await session.interrupt()
    await drain(queue, TurnDone)

    assert factory.created[0].permission_outcomes == [False]  # denied, not hung
    assert session.state == "idle"
    assert session.pending_attention() == []


async def test_orphaned_permission_timeout_never_resurrects_a_finished_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt still pending when the turn ends used to fire its timeout and
    unconditionally set "working" — leaving an idle session marked busy forever
    with nothing running."""
    monkeypatch.setattr(agent_sessions, "PERMISSION_TIMEOUT_S", 0.05)

    class OrphanClient(FakeClient):
        orphan: "asyncio.Task[bool] | None" = None

        async def receive_response(self) -> AsyncIterator[Any]:
            # A prompt nobody answers, outliving the turn that raised it.
            OrphanClient.orphan = asyncio.create_task(
                self._bridge.ask_permission("Bash", {"command": "x"})
            )
            await asyncio.sleep(0)
            yield ResultMessage()

    def factory(
        folder: Path, resume: str | None, bridge: SessionBridge, kind: SessionKind = "chat"
    ) -> OrphanClient:
        return OrphanClient([], bridge)

    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("go")
    await drain(queue, TurnDone)
    assert session.state == "idle"

    orphan = OrphanClient.orphan
    assert orphan is not None
    assert await asyncio.wait_for(orphan, timeout=10) is False  # timed out -> deny
    assert session.state == "idle"


# ---- plans -------------------------------------------------------------------


def sample_plan() -> PlanArtifact:
    return PlanArtifact(
        plan_id="plan-1",
        title="Rework the DST handling",
        summary="Pick a representation, then I do the steps.",
        nodes=[
            OptionGroupNode(
                node_id="approach",
                prompt="Which representation?",
                options=[
                    PlanOption(option_id="local", label="Local time + fold", recommended=True),
                    PlanOption(option_id="utc", label="UTC everywhere"),
                ],
            ),
            StepListNode(
                node_id="steps",
                steps=[PlanStep(text="Add a fold-aware index", file_refs=[FileRef(path="a.py")])],
            ),
        ],
    )


async def test_plan_round_trip_returns_the_typed_response(tmp_path: Path) -> None:
    factory = make_factory([ResultMessage()], plan=sample_plan())
    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("plan the DST fix")

    presented: PlanPresented | None = None
    while presented is None:
        event = await asyncio.wait_for(queue.get(), timeout=10)
        if isinstance(event, PlanPresented):
            presented = event
    assert presented.plan.title == "Rework the DST handling"
    assert session.state == "needs_attention"

    session.resolve_plan(
        PlanResponse(
            plan_id=presented.plan.plan_id,
            verdict="approve",
            choices={"approach": "local"},
            annotations=[PlanAnnotation(anchor=node_anchor("steps"), text="skip the backfill")],
            comment="go",
        )
    )
    await drain(queue, TurnDone)

    client: FakeClient = factory.created[0]
    assert len(client.plan_responses) == 1
    answer = client.plan_responses[0]
    assert answer.verdict == "approve"
    assert answer.choices == {"approach": "local"}
    assert answer.annotations[0].text == "skip the backfill"
    # Nothing answerable is left — only the verdict, replayed so a client that
    # dropped after deciding learns how the card ended.
    replay = session.pending_attention()
    assert [type(e).__name__ for e in replay] == ["PlanResolved"]


async def test_plan_timeout_resolves_to_no_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence is never approval: the agent is told nobody answered."""
    monkeypatch.setattr(agent_sessions, "PLAN_TIMEOUT_S", 0.05)
    factory = make_factory([ResultMessage()], plan=sample_plan())
    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("plan it")
    events = await drain(queue, TurnDone)

    answer = factory.created[0].plan_responses[0]
    assert answer.verdict == "no_decision"
    assert answer.plan_id == "plan-1"
    assert session.state == "idle"
    # The card must stop being answerable in every UI too — otherwise a user
    # returning at minute 11 clicks Approve and is told the agent is proceeding.
    resolved = [e for e in events if isinstance(e, PlanResolved)]
    assert [e.verdict for e in resolved] == ["no_decision"]


async def test_interrupt_settles_a_pending_plan(tmp_path: Path) -> None:
    """An interrupt must unblock the waiting tool call and clear the attention
    state — otherwise the session is wedged with a card nobody can answer."""
    factory = make_factory([ResultMessage()], plan=sample_plan())
    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("plan it")
    while not isinstance(await asyncio.wait_for(queue.get(), timeout=10), PlanPresented):
        pass

    await session.interrupt()
    await drain(queue, TurnDone)

    assert factory.created[0].plan_responses[0].verdict == "no_decision"
    assert session.state == "idle"
    replay = session.pending_attention()
    assert [type(e).__name__ for e in replay] == ["PlanResolved"]  # verdict only, no live card


async def test_one_pending_plan_per_session(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path, make_factory([]), max_sessions=4)
    session = manager.create("")
    first = asyncio.create_task(session.present_plan(sample_plan()))
    await asyncio.sleep(0.05)
    with pytest.raises(PlanAlreadyPendingError):
        await session.present_plan(sample_plan())
    session.resolve_plan(PlanResponse(plan_id="plan-1", verdict="reject"))
    assert (await first).verdict == "reject"


async def test_stale_plan_decision_is_ignored(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path, make_factory([]), max_sessions=4)
    session = manager.create("")
    task = asyncio.create_task(session.present_plan(sample_plan()))
    await asyncio.sleep(0.05)
    session.resolve_plan(
        PlanResponse(plan_id="some-other-plan", verdict="approve", choices={"approach": "local"})
    )
    assert not task.done()
    session.resolve_plan(
        PlanResponse(plan_id="plan-1", verdict="approve", choices={"approach": "local"})
    )
    assert (await task).verdict == "approve"


async def test_approve_without_every_option_chosen_is_ignored(tmp_path: Path) -> None:
    """'approve' means "proceed with the chosen options". A group the user never
    answered (one with no recommended option starts unselected) would reach the
    agent as an implied approval of a decision it explicitly asked about, and it
    would guess. Revise and reject legitimately need no choices."""
    manager = SessionManager(tmp_path, make_factory([]), max_sessions=4)
    session = manager.create("")
    task = asyncio.create_task(session.present_plan(sample_plan()))
    await asyncio.sleep(0.05)
    session.resolve_plan(PlanResponse(plan_id="plan-1", verdict="approve"))
    await asyncio.sleep(0.05)
    assert not task.done()  # dropped, and the card stays answerable

    session.resolve_plan(PlanResponse(plan_id="plan-1", verdict="revise", comment="split step 1"))
    assert (await task).verdict == "revise"


async def test_an_anchor_that_names_nothing_makes_the_whole_decision_malformed(
    tmp_path: Path,
) -> None:
    """Anchors are ours, not the agent's and not the user's prose. One that does
    not point into the artifact would reach the agent as a note *about* a row
    that is not there, so the decision is refused rather than half-delivered —
    and refused audibly, because a client that believes it answered would
    otherwise sit in front of a card that never settles."""
    manager = SessionManager(tmp_path, make_factory([]), max_sessions=4)
    session = manager.create("")
    listener = session.subscribe()
    task = asyncio.create_task(session.present_plan(sample_plan()))
    await asyncio.sleep(0.05)

    session.resolve_plan(
        PlanResponse(
            plan_id="plan-1",
            verdict="revise",
            annotations=[
                PlanAnnotation(anchor=node_anchor("steps"), text="this one is fine"),
                PlanAnnotation(anchor=node_anchor("ghost"), text="and this one is not"),
            ],
        )
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # the card stays answerable
    error = (await drain(listener, AgentError))[-1]
    assert isinstance(error, AgentError)
    assert "no node 'ghost'" in error.message

    # The same decision without the bad anchor goes through untouched.
    session.resolve_plan(
        PlanResponse(
            plan_id="plan-1",
            verdict="revise",
            annotations=[PlanAnnotation(anchor=node_anchor("steps"), text="this one is fine")],
        )
    )
    answer = await task
    assert [note.text for note in answer.annotations] == ["this one is fine"]


async def test_plan_resolution_is_broadcast_to_every_client(tmp_path: Path) -> None:
    """Settlement is a frame, not a local guess: a second client (or the one that
    never answered) must not keep a live card for a plan the agent stopped
    waiting on — nor be able to flip it to "Approved" afterwards."""
    factory = make_factory([ResultMessage()], plan=sample_plan())
    manager = SessionManager(tmp_path, factory, max_sessions=4)
    session = manager.create("")
    first = session.subscribe()
    second = session.subscribe()
    session.send_user_message("plan it")
    while not isinstance(await asyncio.wait_for(first.get(), timeout=10), PlanPresented):
        pass

    session.resolve_plan(
        PlanResponse(plan_id="plan-1", verdict="approve", choices={"approach": "local"})
    )
    for queue in (first, second):
        resolved = [e for e in await drain(queue, TurnDone) if isinstance(e, PlanResolved)]
        assert [(e.plan_id, e.verdict) for e in resolved] == [("plan-1", "approve")]


# ---- session titles ---------------------------------------------------------


class TestDeriveTitle:
    def test_short_message_kept_verbatim(self) -> None:
        assert derive_title("fix the DST bug") == "fix the DST bug"

    def test_whitespace_collapsed(self) -> None:
        assert derive_title("  fix\n\nthe   DST bug\t") == "fix the DST bug"

    def test_long_message_truncated_to_60_with_ellipsis(self) -> None:
        text = "improve the intraday bidding strategy for the SE3 battery portfolio please"
        title = derive_title(text)
        assert len(title) == 60
        assert title.endswith("…")
        assert title.startswith("improve the intraday")

    def test_empty_message_falls_back(self) -> None:
        assert derive_title("   \n ") == "new session"


async def test_live_title_derived_from_first_user_message(tmp_path: Path) -> None:
    script = [ResultMessage()]
    manager = SessionManager(tmp_path, make_factory(script), max_sessions=4)
    session = manager.create("")
    assert manager.live_infos()[0].title == "new session"

    queue = session.subscribe()
    session.send_user_message("tighten the gate-closure check")
    await drain(queue, TurnDone)
    assert manager.live_infos()[0].title == "tighten the gate-closure check"

    session.send_user_message("second message must not retitle")
    await drain(queue, TurnDone)
    assert manager.live_infos()[0].title == "tighten the gate-closure check"


# ---- limits -----------------------------------------------------------------


async def test_concurrent_session_cap(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path, make_factory([]), max_sessions=2)
    a = manager.create("")
    b = manager.create("")
    a.state = "working"
    b.state = "working"
    with pytest.raises(TooManySessionsError):
        manager.create("")


async def test_active_count_is_the_rule_create_enforces(tmp_path: Path) -> None:
    """The UI greys "New agent session" off this number, so it must be the same
    one ``create`` refuses on: sessions *working*, never sessions open."""
    manager = SessionManager(tmp_path, make_factory([]), max_sessions=2)
    assert manager.max_concurrent == 2
    a = manager.create("")
    b = manager.create("")
    assert manager.active_count() == 0, "open but idle sessions hold no slot"

    a.state = "working"
    assert manager.active_count() == 1
    b.state = "needs_attention"
    assert manager.active_count() == 2

    with pytest.raises(TooManySessionsError):
        manager.create("")

    # …and a slot freed by a session going idle is a slot the picker offers again.
    a.state = "idle"
    assert manager.active_count() == 1
    manager.create("")


def test_limits_endpoint_reports_cap_and_refusal(settings: Settings, tmp_path: Path) -> None:
    """`GET /api/agents/limits` is what the picker predicts from, and 429 is what
    it predicts — the two must agree at the boundary."""
    app = create_app(settings)
    app.state.session_manager = SessionManager(tmp_path, make_factory([]), max_sessions=1)

    with TestClient(app) as client:
        assert client.get("/api/agents/limits").json() == {"max_concurrent": 1, "active": 0}

        created = client.post("/api/agents/sessions", json={"folder": ""})
        assert created.status_code == 200
        # Still idle: a session that is merely open has not spent the slot.
        assert client.get("/api/agents/limits").json() == {"max_concurrent": 1, "active": 0}

        manager: SessionManager = app.state.session_manager
        next(iter(manager.sessions.values())).state = "working"
        assert client.get("/api/agents/limits").json() == {"max_concurrent": 1, "active": 1}

        refused = client.post("/api/agents/sessions", json={"folder": ""})
        assert refused.status_code == 429
        assert "limit" in refused.json()["detail"]


async def test_folder_jail(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path, make_factory([]), max_sessions=4)
    with pytest.raises(ValueError, match="escapes"):
        manager.create("../elsewhere")


# ---- session index ----------------------------------------------------------


class TestSessionIndex:
    def test_encoding_matches_claude_code(self) -> None:
        assert (
            encode_project_dir(Path("C:/Users/mathi/GnistEnergy_repo"))
            == "C--Users-mathi-GnistEnergy-repo"
        )

    def test_stored_titles_match_live_derivation(self, tmp_path: Path) -> None:
        """Stored transcripts use derive_title too — a conversation must not
        retitle (length/whitespace/ellipsis) when it goes from live to on-disk."""
        folder = tmp_path / "ws"
        folder.mkdir()
        project = tmp_path / "projects" / encode_project_dir(folder)
        project.mkdir(parents=True)
        first = "improve\n\nthe   intraday bidding strategy for the SE3 battery portfolio please"
        (project / "bbb-222.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": first}}),
            encoding="utf-8",
        )

        sessions = SessionIndex(tmp_path / "projects").list_sessions(folder, "")
        assert sessions[0].title == derive_title(first)
        assert len(sessions[0].title) == 60
        assert sessions[0].title.endswith("…")

    def test_lists_and_reads_transcripts(self, tmp_path: Path) -> None:
        folder = tmp_path / "ws" / "se3"
        folder.mkdir(parents=True)
        project = tmp_path / "projects" / encode_project_dir(folder)
        project.mkdir(parents=True)
        lines = [
            {"type": "user", "message": {"role": "user", "content": "improve the forecast"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Looking at the model now."}],
                },
            },
            {"type": "summary", "summary": "not a message"},
        ]
        (project / "aaa-111.jsonl").write_text(
            "\n".join(json.dumps(entry) for entry in lines), encoding="utf-8"
        )

        index = SessionIndex(tmp_path / "projects")
        sessions = index.list_sessions(folder, "se3")
        assert len(sessions) == 1
        assert sessions[0].session_id == "aaa-111"
        assert sessions[0].title == "improve the forecast"
        assert sessions[0].folder == "se3"

        messages = index.read_transcript(folder, "aaa-111")
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[1].text == "Looking at the model now."

    def test_transcript_id_is_sanitized(self, tmp_path: Path) -> None:
        index = SessionIndex(tmp_path)
        with pytest.raises(FileNotFoundError):
            index.read_transcript(tmp_path, "../../etc/passwd")


# ---- which folders the session list looks in --------------------------------


class TestSessionFolders:
    """`GET /api/agents/sessions` reports history per first-level folder.

    Which folders it looks in used to come from a full recursive walk of the
    workspace (`workspace.tree()`), and now comes from one `os.scandir`
    (`Workspace.top_level_dirs`). These are the behaviours that must not have
    changed with it — the perf side of the same change is in
    `test_perf_budgets.py`.
    """

    def _transcript(self, projects: Path, folder: Path, session_id: str, text: str) -> None:
        project = projects / encode_project_dir(folder.resolve())
        project.mkdir(parents=True, exist_ok=True)
        (project / f"{session_id}.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": text}}),
            encoding="utf-8",
        )

    def test_history_in_a_first_level_folder_is_grouped_under_it(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        folder = workspace / "se3"
        folder.mkdir(parents=True)
        projects = tmp_path / "projects"
        self._transcript(projects, workspace, "root-1", "at the root")
        self._transcript(projects, folder, "se3-1", "improve the forecast")

        settings = Settings(workspace_root=workspace, claude_projects_dir=projects)
        with TestClient(create_app(settings)) as client:
            groups = client.get("/api/agents/sessions").json()

        by_folder = {g["folder"]: [s["session_id"] for s in g["sessions"]] for g in groups}
        assert by_folder == {"": ["root-1"], "se3": ["se3-1"]}

    def test_a_build_cache_is_not_a_folder_the_list_looks_in(self, tmp_path: Path) -> None:
        """A tagged cache is invisible to the tree, so it is invisible here too —
        otherwise `target/` would appear in the session list of every Rust repo."""
        workspace = tmp_path / "ws"
        cache = workspace / "target"
        cache.mkdir(parents=True)
        (cache / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        projects = tmp_path / "projects"
        self._transcript(projects, cache, "cached-1", "should not be listed")

        settings = Settings(workspace_root=workspace, claude_projects_dir=projects)
        with TestClient(create_app(settings)) as client:
            groups = client.get("/api/agents/sessions").json()

        assert groups == []


# ---- full WS pipeline -------------------------------------------------------


@pytest.mark.timeout(60)
def test_ws_pipeline_end_to_end(settings: Settings, tmp_path: Path) -> None:
    """REST create -> WS connect -> user message -> streamed frames -> turn_done."""
    app = create_app(settings)
    script = [delta("done: "), delta("42"), ResultMessage(session_id="sdk-e2e")]
    app.state.session_manager = SessionManager(tmp_path, make_factory(script), max_sessions=4)

    with TestClient(app) as client:
        created = client.post("/api/agents/sessions", json={"folder": ""})
        assert created.status_code == 200
        local_id = created.json()["session_id"]

        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "answer?"}))
            seen_types: list[str] = []
            text = ""
            while "turn_done" not in seen_types:
                frame = json.loads(ws.receive_text())
                seen_types.append(frame["type"])
                if frame["type"] == "text_delta":
                    text += frame["text"]
            assert text == "done: 42"
            assert "status" in seen_types  # working state was broadcast

        listing = client.get("/api/agents/sessions")
        assert listing.status_code == 200
        assert any(s["session_id"] == local_id for g in listing.json() for s in g["sessions"])


@pytest.mark.timeout(60)
def test_ws_plan_decision_round_trip(settings: Settings, tmp_path: Path) -> None:
    """present_plan -> plan_presented frame -> plan_decision from the client ->
    the agent's tool call returns the typed response."""
    app = create_app(settings)
    factory = make_factory([ResultMessage()], plan=sample_plan())
    app.state.session_manager = SessionManager(tmp_path, factory, max_sessions=4)

    with TestClient(app) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "plan it"}))
            frame = json.loads(ws.receive_text())
            while frame["type"] != "plan_presented":
                frame = json.loads(ws.receive_text())
            plan = frame["plan"]
            assert [node["kind"] for node in plan["nodes"]] == ["option_group", "step_list"]
            assert plan["nodes"][1]["steps"][0]["file_refs"] == [{"path": "a.py"}]

            ws.send_text(
                json.dumps(
                    {
                        "type": "plan_decision",
                        "response": {
                            "plan_id": plan["plan_id"],
                            "verdict": "revise",
                            "choices": {"approach": "utc"},
                            "annotations": [
                                {
                                    "anchor": {"kind": "node", "node_id": "steps"},
                                    "text": "add a rollback step",
                                }
                            ],
                            "comment": "close, but split step 1",
                        },
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "turn_done":
                pass

    answer = factory.created[0].plan_responses[0]
    assert answer.verdict == "revise"
    assert answer.choices == {"approach": "utc"}
    assert answer.annotations[0].anchor.node_id == "steps"
    assert answer.comment == "close, but split step 1"


@pytest.mark.timeout(60)
def test_pending_plan_is_replayed_to_a_reconnecting_client(
    settings: Settings, tmp_path: Path
) -> None:
    """A client that connects after the card was emitted still gets it."""
    app = create_app(settings)
    factory = make_factory([ResultMessage()], plan=sample_plan())
    app.state.session_manager = SessionManager(tmp_path, factory, max_sessions=4)

    with TestClient(app) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "plan it"}))
            while json.loads(ws.receive_text())["type"] != "plan_presented":
                pass  # emitted once, to this connection only

        with client.websocket_connect(f"/ws/agent/{local_id}") as ws2:
            replayed = json.loads(ws2.receive_text())
            assert replayed["type"] == "plan_presented"
            assert replayed["plan"]["plan_id"] == "plan-1"
            ws2.send_text(
                json.dumps(
                    {
                        "type": "plan_decision",
                        "response": {
                            "plan_id": "plan-1",
                            "verdict": "approve",
                            "choices": {"approach": "local"},
                        },
                    }
                )
            )
            seen: list[str] = []
            while "turn_done" not in seen:
                seen.append(json.loads(ws2.receive_text())["type"])
            assert "plan_resolved" in seen  # the card settles on the wire, not by click

    assert factory.created[0].plan_responses[0].verdict == "approve"


@pytest.mark.timeout(60)
def test_settled_plan_verdict_is_replayed_after_a_reconnect(
    settings: Settings, tmp_path: Path
) -> None:
    """A client that decides and then loses the socket before ``plan_resolved``
    arrives used to never learn the verdict: its card stayed live and answerable
    for a plan the agent had already acted on. The replay closes that window."""
    app = create_app(settings)
    factory = make_factory([ResultMessage()], plan=sample_plan())
    app.state.session_manager = SessionManager(tmp_path, factory, max_sessions=4)

    with TestClient(app) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "plan it"}))
            while json.loads(ws.receive_text())["type"] != "plan_presented":
                pass
            # Decide, then drop the connection immediately — the frame settling
            # the card is emitted while nobody is listening.
            ws.send_text(
                json.dumps(
                    {
                        "type": "plan_decision",
                        "response": {
                            "plan_id": "plan-1",
                            "verdict": "approve",
                            "choices": {"approach": "local"},
                        },
                    }
                )
            )

        with client.websocket_connect(f"/ws/agent/{local_id}") as ws2:
            replayed = json.loads(ws2.receive_text())
            assert replayed["type"] == "plan_resolved"
            assert replayed["plan_id"] == "plan-1"
            assert replayed["verdict"] == "approve"

    assert factory.created[0].plan_responses[0].verdict == "approve"


@pytest.mark.timeout(60)
def test_session_status_reaches_the_events_websocket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions without an open agent socket must still update: state changes go
    onto the same in-process bus the watcher uses and out over /ws/events.

    Runs against ``create_app``'s OWN SessionManager — only the SDK client
    factory is swapped out — so the one production line the feature hangs on
    (``event_publisher=event_bus`` in ``main.py``) is what carries the fan-out.
    Rebuilding the manager here would let that kwarg be deleted with the suite
    still green while chips and counts silently stopped updating.
    """
    factory = make_factory([delta("ok"), ResultMessage()])

    # Mirrors the real signature, Settings included: main.py passes the settings
    # through so a session's options (bundled skills, setting sources) are
    # configurable, and a stub that drops the argument fails at call time.
    def fake_sdk_client_factory(
        _ui_state_store: Any,
        _settings: Any = None,
        _orchestrator: Any = None,
        _session_cost: Any = None,
    ) -> Any:
        return factory

    monkeypatch.setattr(main, "sdk_client_factory", fake_sdk_client_factory)
    app = create_app(Settings(workspace_root=tmp_path, claude_projects_dir=tmp_path / "projects"))

    with TestClient(app) as client, client.websocket_connect("/ws/events") as events:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "go"}))
            while json.loads(ws.receive_text())["type"] != "turn_done":
                pass

        statuses: list[dict[str, Any]] = []
        while len(statuses) < 2:  # working, then idle
            frame = json.loads(events.receive_text())
            if frame["type"] == "session_status":
                statuses.append(frame)

    assert [f["state"] for f in statuses] == ["working", "idle"]
    assert {f["session_id"] for f in statuses} == {local_id}
    assert statuses[0]["folder"] == ""


@pytest.mark.timeout(60)
def test_pending_permission_is_replayed_to_a_reconnecting_client(
    settings: Settings, tmp_path: Path
) -> None:
    """The same replay gap existed for permission prompts: a session blocked on
    one used to look like a hung agent to any client that connected later."""
    app = create_app(settings)
    factory = make_factory([ResultMessage()], ask_for="Bash")
    app.state.session_manager = SessionManager(tmp_path, factory, max_sessions=4)

    with TestClient(app) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "install deps"}))
            while json.loads(ws.receive_text())["type"] != "permission_request":
                pass

        with client.websocket_connect(f"/ws/agent/{local_id}") as ws2:
            replayed = json.loads(ws2.receive_text())
            assert replayed["type"] == "permission_request"
            ws2.send_text(
                json.dumps(
                    {
                        "type": "permission_decision",
                        "request_id": replayed["request_id"],
                        "allow": True,
                    }
                )
            )
            while json.loads(ws2.receive_text())["type"] != "turn_done":
                pass

    assert factory.created[0].permission_outcomes == [True]


@pytest.mark.timeout(60)
def test_live_session_suppresses_its_own_transcript(tmp_path: Path) -> None:
    """After the first turn the conversation exists twice on the backend — live
    (local id) and on disk (SDK uuid). The listing must show it once: live."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    projects = tmp_path / "projects"
    settings = Settings(workspace_root=workspace_root, claude_projects_dir=projects)
    app = create_app(settings)
    script = [delta("ok"), ResultMessage(session_id="sdk-e2e")]
    app.state.session_manager = SessionManager(
        settings.resolved_workspace(),
        make_factory(script),
        max_sessions=4,
        session_index=app.state.session_index,
    )

    project_dir = projects / encode_project_dir(settings.resolved_workspace())
    project_dir.mkdir(parents=True)

    def transcript_line(text: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": text}})

    (project_dir / "sdk-e2e.jsonl").write_text(transcript_line("check the bids"), "utf-8")
    (project_dir / "other-11.jsonl").write_text(transcript_line("unrelated session"), "utf-8")

    with TestClient(app) as client:
        created = client.post("/api/agents/sessions", json={"folder": ""})
        local_id = created.json()["session_id"]

        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "check the bids"}))
            while json.loads(ws.receive_text())["type"] != "turn_done":
                pass

        rows = [s for g in client.get("/api/agents/sessions").json() for s in g["sessions"]]
        ids = [s["session_id"] for s in rows]
        assert local_id in ids  # the live entry represents the conversation
        assert "sdk-e2e" not in ids  # its transcript row is suppressed
        assert "other-11" in ids  # unrelated transcripts still listed
        live_row = next(s for s in rows if s["session_id"] == local_id)
        assert live_row["title"] == "check the bids"
        assert live_row["live"] is True

        # Resuming a transcript keeps its title and absorbs its disk row too.
        resumed = client.post(
            "/api/agents/sessions", json={"folder": "", "resume_session_id": "other-11"}
        )
        assert resumed.status_code == 200
        assert resumed.json()["title"] == "unrelated session"
        ids_after = [
            s["session_id"]
            for g in client.get("/api/agents/sessions").json()
            for s in g["sessions"]
        ]
        assert "other-11" not in ids_after


@pytest.mark.timeout(60)
def test_resumed_transcript_stays_suppressed_after_a_turn(tmp_path: Path) -> None:
    """Claude Code mints a NEW sdk id when a conversation is resumed. After the
    first turn the session must suppress BOTH transcripts — the one it resumed
    from and the one written under the fresh id — or the stale row reappears
    next to the live entry and clicking it re-resumes an outdated fork."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    projects = tmp_path / "projects"
    settings = Settings(workspace_root=workspace_root, claude_projects_dir=projects)
    app = create_app(settings)
    # The resumed conversation comes back under a fresh sdk id.
    script = [delta("continuing"), ResultMessage(session_id="sdk-fresh")]
    app.state.session_manager = SessionManager(
        settings.resolved_workspace(),
        make_factory(script),
        max_sessions=4,
        session_index=app.state.session_index,
    )

    project_dir = projects / encode_project_dir(settings.resolved_workspace())
    project_dir.mkdir(parents=True)

    def transcript_line(text: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": text}})

    (project_dir / "old-transcript.jsonl").write_text(transcript_line("original ask"), "utf-8")

    with TestClient(app) as client:
        resumed = client.post(
            "/api/agents/sessions",
            json={"folder": "", "resume_session_id": "old-transcript"},
        )
        assert resumed.status_code == 200
        local_id = resumed.json()["session_id"]
        assert resumed.json()["title"] == "original ask"  # via SessionManager.create

        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "keep going"}))
            while json.loads(ws.receive_text())["type"] != "turn_done":
                pass

        # The SDK persisted the resumed conversation under its fresh id too.
        (project_dir / "sdk-fresh.jsonl").write_text(transcript_line("original ask"), "utf-8")

        rows = [s for g in client.get("/api/agents/sessions").json() for s in g["sessions"]]
        ids = [s["session_id"] for s in rows]
        assert local_id in ids  # the live entry represents the conversation
        assert "old-transcript" not in ids  # resumed-from transcript still suppressed
        assert "sdk-fresh" not in ids  # fresh-id transcript suppressed as well
