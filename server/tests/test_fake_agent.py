"""Fake-agent mode: the scripted client factory, driven through the same seams
the SDK factory plugs into (``ClientFactory`` + ``SessionBridge``), plus the one
production line that wires it — ``main.py`` reading ``Settings.fake_agent``.

The E2E suite depends on every trigger here behaving exactly as asserted, so
these are its contract tests: if a trigger stops producing its frames, this
fails in seconds instead of a Playwright journey failing in minutes.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.agents import PermissionRequest, TextDelta, ToolSettled, ToolUseNote
from workbench_server.models.agents import TurnDone as TurnDoneEvent
from workbench_server.models.plans import PlanPresented, PlanResponse
from workbench_server.services import fake_agent
from workbench_server.services.agent_sessions import SessionManager
from workbench_server.services.fake_agent import fake_client_factory, first_workspace_file

from .test_agent_sessions import drain


def workspace_with_notes(tmp_path: Path) -> Path:
    (tmp_path / "notes.md").write_text("# Notes\n\nSE3 battery notes.\n", encoding="utf-8")
    return tmp_path


def manager_for(folder: Path) -> SessionManager:
    return SessionManager(folder, fake_client_factory(), max_sessions=4)


def text_of(events: list[BaseModel]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


# ---- the scripted turn -------------------------------------------------------


async def test_plain_message_streams_a_deterministic_markdown_reply(tmp_path: Path) -> None:
    session = manager_for(workspace_with_notes(tmp_path)).create("")
    queue = session.subscribe()
    session.send_user_message("hello there")
    events = await drain(queue, TurnDoneEvent)

    text = text_of(events)
    assert "**Fake agent**" in text  # markdown, so the chat renderer has work to do
    assert "echo: hello there" in text
    done = events[-1]
    assert isinstance(done, TurnDoneEvent)
    assert done.is_error is False
    assert session.state == "idle"


async def test_use_tool_emits_a_read_note_and_its_own_result(tmp_path: Path) -> None:
    """Two frames, not one: the row appears, then settles on its own result."""
    session = manager_for(workspace_with_notes(tmp_path)).create("")
    queue = session.subscribe()
    session.send_user_message("use tool please")
    events = await drain(queue, TurnDoneEvent)

    note = next(e for e in events if isinstance(e, ToolUseNote))
    settled = next(e for e in events if isinstance(e, ToolSettled))
    assert note.tool == "Read"
    assert "notes.md" in note.summary
    assert settled.id == note.id
    assert settled.ok is True
    assert "SE3 battery notes." in settled.output_excerpt


async def test_read_targets_a_real_file_and_degrades_when_there_is_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert first_workspace_file(empty) is None
    (empty / "b.txt").write_text("b", encoding="utf-8")
    (empty / "a.txt").write_text("a", encoding="utf-8")
    picked = first_workspace_file(empty)
    assert picked is not None
    assert picked.name == "a.txt"

    (tmp_path / "nothing").mkdir()
    session = SessionManager(tmp_path, fake_client_factory(), max_sessions=4).create("nothing")
    queue = session.subscribe()
    session.send_user_message("use tool")
    events = await drain(queue, TurnDoneEvent)
    settled = next(e for e in events if isinstance(e, ToolSettled))
    assert settled.ok is False  # still a settled row, never a hung one


async def test_ask_permission_round_trips_through_the_bridge(tmp_path: Path) -> None:
    session = manager_for(workspace_with_notes(tmp_path)).create("")
    queue = session.subscribe()
    session.send_user_message("ask permission first")

    request: PermissionRequest | None = None
    while request is None:
        event = await asyncio.wait_for(queue.get(), timeout=10)
        if isinstance(event, PermissionRequest):
            request = event
    assert request.tool == "Bash"
    assert fake_agent.PERMISSION_COMMAND in request.description
    assert session.state == "needs_attention"  # what the title badge reads

    session.resolve_permission(request.request_id, True)
    events = await drain(queue, TurnDoneEvent)
    assert "permission: allowed" in text_of(events)


async def test_plan_please_presents_a_card_and_echoes_the_decision(tmp_path: Path) -> None:
    session = manager_for(workspace_with_notes(tmp_path)).create("")
    queue = session.subscribe()
    session.send_user_message("plan please")

    presented: PlanPresented | None = None
    while presented is None:
        event = await asyncio.wait_for(queue.get(), timeout=10)
        if isinstance(event, PlanPresented):
            presented = event
    plan = presented.plan
    assert [node.kind for node in plan.nodes] == ["option_group", "step_list"]
    group = plan.nodes[0]
    assert group.kind == "option_group"
    assert [option.recommended for option in group.options] == [True, False]

    session.resolve_plan(
        PlanResponse(plan_id=plan.plan_id, verdict="approve", choices={"approach": "utc"})
    )
    events = await drain(queue, TurnDoneEvent)
    # The echo is what proves the agent side received the user's choice.
    assert "plan approve: approach=utc" in text_of(events)


async def test_each_presentation_is_a_fresh_card(tmp_path: Path) -> None:
    """Two plans in a row must not share a plan_id — the UI dedupes on it."""
    assert fake_agent.fake_plan().plan_id != fake_agent.fake_plan().plan_id


async def test_stay_busy_holds_the_turn_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold is what lets a UI observe the working state; the turn still
    completes normally afterwards."""
    monkeypatch.setattr(fake_agent, "BUSY_HOLD_S", 0.05)
    session = manager_for(workspace_with_notes(tmp_path)).create("")
    queue = session.subscribe()
    session.send_user_message("stay busy for a moment")
    await asyncio.sleep(0)
    during_the_hold = session.state
    await drain(queue, TurnDoneEvent)
    assert during_the_hold == "working"  # what the status-bar chip shows
    assert session.state == "idle"


async def test_factory_builds_one_client_per_session(tmp_path: Path) -> None:
    factory = fake_client_factory()
    manager = SessionManager(tmp_path, factory, max_sessions=4)
    first = manager.create("")
    second = manager.create("")
    for session in (first, second):
        queue = session.subscribe()
        session.send_user_message("hi")
        await drain(queue, TurnDoneEvent)
    assert first.sdk_session_id != second.sdk_session_id


# ---- the production wiring ---------------------------------------------------


@pytest.mark.timeout(60)
def test_app_uses_the_fake_only_when_the_setting_is_on(tmp_path: Path) -> None:
    """The one line in main.py the whole mode hangs on. Runs the full REST + WS
    path, so a wiring regression fails here rather than in the E2E suite."""
    workspace = workspace_with_notes(tmp_path)
    settings = Settings(
        workspace_root=workspace,
        claude_projects_dir=tmp_path / "projects",
        fake_agent=True,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "use tool"}))
            text = ""
            tools: list[str] = []
            frame = json.loads(ws.receive_text())
            while frame["type"] != "turn_done":
                if frame["type"] == "text_delta":
                    text += frame["text"]
                elif frame["type"] == "tool_use":
                    tools.append(frame["tool"])
                frame = json.loads(ws.receive_text())
            assert "**Fake agent**" in text
            assert tools == ["Read"]


def test_fake_agent_is_off_by_default() -> None:
    assert Settings().fake_agent is False
