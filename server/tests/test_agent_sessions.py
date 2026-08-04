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

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.agents import (
    PermissionRequest,
    TextDelta,
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
    PlanResponse,
    PlanStep,
    StepListNode,
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
    def __init__(self, name: str, tool_input: dict[str, Any]) -> None:
        self.name = name
        self.input = tool_input


class AssistantMessage:
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

    def factory(folder: Path, resume: str | None, bridge: SessionBridge) -> FakeClient:
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


async def test_second_message_rejected_while_working(tmp_path: Path) -> None:
    gate: asyncio.Event = asyncio.Event()

    class SlowClient(FakeClient):
        async def receive_response(self) -> AsyncIterator[Any]:
            await gate.wait()
            yield ResultMessage()

    def factory(folder: Path, resume: str | None, bridge: SessionBridge) -> SlowClient:
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
            annotations=[PlanAnnotation(node_id="steps", text="skip the backfill")],
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
    assert session.pending_attention() == []  # nothing left to replay


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
    await drain(queue, TurnDone)

    answer = factory.created[0].plan_responses[0]
    assert answer.verdict == "no_decision"
    assert answer.plan_id == "plan-1"
    assert session.state == "idle"


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
    assert session.pending_attention() == []


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
    session.resolve_plan(PlanResponse(plan_id="some-other-plan", verdict="approve"))
    assert not task.done()
    session.resolve_plan(PlanResponse(plan_id="plan-1", verdict="approve"))
    assert (await task).verdict == "approve"


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
                            "annotations": [{"node_id": "steps", "text": "add a rollback step"}],
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
    assert answer.annotations[0].node_id == "steps"
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
                        "response": {"plan_id": "plan-1", "verdict": "approve"},
                    }
                )
            )
            while json.loads(ws2.receive_text())["type"] != "turn_done":
                pass

    assert factory.created[0].plan_responses[0].verdict == "approve"


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
