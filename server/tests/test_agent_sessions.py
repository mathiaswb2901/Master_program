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
from workbench_server.services.agent_sessions import (
    PermissionAsk,
    SessionManager,
    TooManySessionsError,
)
from workbench_server.services.session_index import SessionIndex, encode_project_dir

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
    """Yields a scripted message sequence; optionally asks permission first."""

    def __init__(self, script: list[Any], ask: PermissionAsk, ask_for: str | None = None) -> None:
        self._script = script
        self._ask = ask
        self._ask_for = ask_for
        self.prompts: list[str] = []
        self.permission_outcomes: list[bool] = []
        self.disconnected = False

    async def connect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        if self._ask_for is not None:
            outcome = await self._ask(self._ask_for, {"command": "pip install x"})
            self.permission_outcomes.append(outcome)
        for message in self._script:
            yield message

    async def interrupt(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


def make_factory(script: list[Any], ask_for: str | None = None) -> Any:
    created: list[FakeClient] = []

    def factory(folder: Path, resume: str | None, ask: PermissionAsk) -> FakeClient:
        client = FakeClient(script, ask, ask_for)
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

    def factory(folder: Path, resume: str | None, ask: PermissionAsk) -> SlowClient:
        return SlowClient([], ask)

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
