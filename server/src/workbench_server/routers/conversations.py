"""Browse Claude Code's conversation store. Thin: the service owns the scan."""

import asyncio

from fastapi import APIRouter, Query, Request

from workbench_server.models.conversations import ConversationStore
from workbench_server.services.agent_sessions import SessionManager
from workbench_server.services.conversations import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ConversationBrowser,
)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
async def conversations(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> ConversationStore:
    """Every conversation on this machine, grouped by the folder it ran in.

    Read-only: this reflects storage Claude Code owns and nothing here writes to
    it. ``limit`` bounds the *expensive* half only — every conversation that
    exists is counted and reported, and the newest ``limit`` of them are read in
    full for their title and turn count.

    Handed to a thread: enumerating the store is fast (≈5 ms for 22 projects on
    the author's machine) but a cold read of their transcripts is not (≈1.2 s for
    397 MB), and neither belongs on the event loop.
    """
    browser: ConversationBrowser = request.app.state.conversations
    manager: SessionManager = request.app.state.session_manager
    # Read on the loop, where the session manager lives, and handed to the scan
    # as a plain mapping — a live session continuing a transcript is what stops
    # the browser resuming the same conversation a second time.
    live = manager.live_by_sdk_id()
    return await asyncio.to_thread(browser.browse, limit=limit, live_by_sdk_id=live)
