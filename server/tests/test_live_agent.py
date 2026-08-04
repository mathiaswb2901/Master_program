"""Live smoke test against the real Agent SDK and the machine's Claude login.

Run explicitly with:  WORKBENCH_LIVE_AGENT=1 uv run pytest server/tests/test_live_agent.py
Skipped everywhere else (CI has no credentials). This is the M1 verification that
a Claude subscription login — no API key — powers sessions end to end.
"""

import asyncio
import os
from pathlib import Path

import pytest

from workbench_server.models.agents import TextDelta, TurnDone
from workbench_server.services.agent_sessions import SessionManager
from workbench_server.services.sdk_factory import UiStateStore, sdk_client_factory

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKBENCH_LIVE_AGENT") != "1",
    reason="live agent smoke test; set WORKBENCH_LIVE_AGENT=1 to run",
)


@pytest.mark.timeout(300)
async def test_real_sdk_round_trip(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path, sdk_client_factory(UiStateStore()), max_sessions=1)
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message(
        "Reply with exactly the word: pong. No punctuation, nothing else."
    )
    text = ""
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=240)
        if isinstance(event, TextDelta):
            text += event.text
        if isinstance(event, TurnDone):
            assert not event.is_error, "turn ended in error"
            break
    assert "pong" in text.lower()
    assert session.sdk_session_id is not None  # transcript now exists on disk
    await manager.close_all()
