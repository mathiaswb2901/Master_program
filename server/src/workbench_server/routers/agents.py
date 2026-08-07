"""Agent session REST + WebSocket endpoints."""

import asyncio
import contextlib

import structlog
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from workbench_server.models.agents import (
    CreateSessionRequest,
    FolderSessions,
    Interrupt,
    PermissionAnswer,
    PermissionDecision,
    PermissionsSnapshot,
    SessionInfo,
    SessionLimits,
    TranscriptResponse,
    UiState,
    UserMessage,
    agent_client_message,
)
from workbench_server.models.files import OkResponse
from workbench_server.models.plans import PlanDecision
from workbench_server.services.agent_sessions import SessionManager, TooManySessionsError
from workbench_server.services.local_auth import ws_subprotocol_to_echo
from workbench_server.services.sdk_factory import UiStateStore
from workbench_server.services.session_index import SessionIndex
from workbench_server.services.workspace import PathOutsideWorkspaceError, Workspace

log = structlog.get_logger()

router = APIRouter(prefix="/api/agents", tags=["agents"])
ws_router = APIRouter()


@router.get("/sessions")
def list_sessions(request: Request) -> list[FolderSessions]:
    """All sessions grouped by folder: live ones plus transcripts on disk."""
    workspace: Workspace = request.app.state.workspace
    index: SessionIndex = request.app.state.session_index
    manager: SessionManager = request.app.state.session_manager

    grouped: dict[str, list[SessionInfo]] = {}
    for info in manager.live_infos():
        grouped.setdefault(info.folder, []).append(info)

    # A live session's transcript is already on disk under its SDK uuid — the
    # live entry (keyed by local id) represents that conversation, so the
    # matching transcript row is suppressed.
    live_sdk_ids = manager.live_sdk_ids()

    # folders worth showing: root + first-level dirs (deeper folders appear once used)
    folders = ["", *workspace.top_level_dirs()]
    for folder in folders:
        for info in index.list_sessions(workspace.safe_path(folder), folder):
            if info.session_id in live_sdk_ids:
                continue
            bucket = grouped.setdefault(folder, [])
            if not any(s.session_id == info.session_id for s in bucket):
                bucket.append(info)

    return [
        FolderSessions(folder=folder, sessions=sorted(infos, key=lambda s: -s.updated_at))
        for folder, infos in sorted(grouped.items())
        if infos
    ]


@router.get("/limits")
def limits(request: Request) -> SessionLimits:
    """What a new session would cost against the ceiling, before asking for one."""
    manager: SessionManager = request.app.state.session_manager
    return SessionLimits(max_concurrent=manager.max_concurrent, active=manager.active_count())


@router.post("/sessions")
def create_session(request: Request, body: CreateSessionRequest) -> SessionInfo:
    manager: SessionManager = request.app.state.session_manager
    try:
        # `create`, never `create_at`: a client-named folder is jailed, and
        # `kind` is narrowed at the schema so this path cannot mint a worker
        # (workers live in a pooled worktree outside the workspace, and only the
        # orchestrator service holds a lease on one).
        session = manager.create(body.folder, body.resume_session_id, body.kind)
    except TooManySessionsError as e:
        raise HTTPException(429, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, "folder escapes workspace") from e
    return session.info()


@router.get("/permissions")
def permissions(request: Request) -> PermissionsSnapshot:
    """Every session currently blocked on the human — the fleet, not one chat.

    Load and reconnect for Mission Control's inline permission chips; the live
    path is ``SessionPermissionEvent`` on ``/ws/events``. Without this a board
    opened *after* an agent blocked would show an idle fleet while a worker sat
    waiting for a decision nobody could see.
    """
    manager: SessionManager = request.app.state.session_manager
    return PermissionsSnapshot(sessions=manager.pending_permissions())


@router.post("/sessions/{local_id}/permission")
def answer_permission(request: Request, local_id: str, body: PermissionAnswer) -> SessionInfo:
    """Answer one prompt without holding that session's socket.

    The board is by definition looking at sessions it has not opened a chat
    for — that is the whole point of it — so opening a WebSocket to click Deny
    would mean subscribing to a conversation to answer a card. Same future,
    second door.

    A card that is already settled comes back **404 rather than 200**: the click
    changed nothing, and reporting success would let the user believe they had
    denied something the agent was already allowed to do.
    """
    manager: SessionManager = request.app.state.session_manager
    session = manager.get(local_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    if not session.resolve_permission(body.request_id, body.allow):
        raise HTTPException(404, "that request is no longer awaiting a decision")
    return session.info()


@router.get("/transcript")
def transcript(request: Request, folder: str = "", session_id: str = "") -> TranscriptResponse:
    workspace: Workspace = request.app.state.workspace
    index: SessionIndex = request.app.state.session_index
    try:
        messages = index.read_transcript(workspace.safe_path(folder), session_id)
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "folder escapes workspace") from e
    except FileNotFoundError as e:
        raise HTTPException(404, "transcript not found") from e
    return TranscriptResponse(session_id=session_id, messages=messages)


@router.get("/{local_id}/transcript")
def live_transcript(request: Request, local_id: str) -> TranscriptResponse:
    """A LIVE session's transcript, so a client that (re)connects after a reload
    can replay the conversation that streamed while it was away.

    ``/ws/agent/{id}`` only carries a session *forward* — it replays pending
    permission and plan cards on connect but never the text and tool rows
    already streamed — so a reloaded window opens a live socket over a blank
    chat (the client holds chat state in memory). This fills that gap from disk:
    the union of the session's SDK transcripts (a resume adds a fresh id, hence
    more than one) read in mtime order. 404 when no live session has this id —
    a transcript that is only on disk is served by ``GET /transcript`` instead.
    """
    manager: SessionManager = request.app.state.session_manager
    index: SessionIndex = request.app.state.session_index
    session = manager.get(local_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    folder, sdk_ids = session.transcript_sources()
    return TranscriptResponse(session_id=local_id, messages=index.read_transcripts(folder, sdk_ids))


@router.put("/ui-state")
def put_ui_state(request: Request, body: UiState) -> OkResponse:
    store: UiStateStore = request.app.state.ui_state_store
    store.state = body
    return OkResponse()


@ws_router.websocket("/ws/agent/{local_id}")
async def agent_ws(ws: WebSocket, local_id: str) -> None:
    manager: SessionManager = ws.app.state.session_manager
    session = manager.get(local_id)
    if session is None:
        await ws.close(code=4404, reason="unknown session")
        return
    # Echo the offered token subprotocol (middleware already validated it), or a
    # browser fails the handshake. None when none was offered.
    await ws.accept(subprotocol=ws_subprotocol_to_echo(ws.scope))
    queue = session.subscribe()
    # Replay what this client missed. A session blocked on a permission prompt
    # or a plan card emitted that frame once; without this, a client connecting
    # afterwards (first open of a session started elsewhere, or a reconnect)
    # sees needs_attention with nothing to answer and the agent waits forever.
    for pending in session.pending_attention():
        queue.put_nowait(pending)

    async def pump() -> None:
        with contextlib.suppress(RuntimeError):
            while True:
                event = await queue.get()
                await ws.send_text(event.model_dump_json())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = agent_client_message.validate_json(raw)
            except ValidationError:
                log.warning("agent.bad_message", session=local_id)
                continue
            if isinstance(msg, UserMessage):
                session.send_user_message(msg.text)
            elif isinstance(msg, PermissionDecision):
                session.resolve_permission(msg.request_id, msg.allow)
            elif isinstance(msg, PlanDecision):
                session.resolve_plan(msg.response)
            elif isinstance(msg, Interrupt):
                await session.interrupt()
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
        session.unsubscribe(queue)
