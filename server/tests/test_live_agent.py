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
from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    WordEdit,
    WordText,
)
from workbench_server.services.agent_sessions import SessionManager
from workbench_server.services.office_host.document_bridge import DocNotHostedError
from workbench_server.services.sdk_factory import UiStateStore, sdk_client_factory

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKBENCH_LIVE_AGENT") != "1",
    reason="live agent smoke test; set WORKBENCH_LIVE_AGENT=1 to run",
)


class _NoReader:
    """No document is docked in this smoke test — office_read/write just refuse."""

    async def document_structure(self, path: str) -> DocStructure:
        raise DocNotHostedError(path)

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
        raise DocNotHostedError(path)

    async def write_document(
        self,
        path: str,
        *,
        content: str,
        paragraph: int | None = None,
        sheet: str | None = None,
        cell: str | None = None,
    ) -> WordEdit | CellEdit:
        raise DocNotHostedError(path)


@pytest.mark.timeout(300)
async def test_real_sdk_round_trip(tmp_path: Path) -> None:
    manager = SessionManager(
        tmp_path, sdk_client_factory(UiStateStore(), _NoReader()), max_sessions=1
    )
    session = manager.create("")
    queue = session.subscribe()
    session.send_user_message("Reply with exactly the word: pong. No punctuation, nothing else.")
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
