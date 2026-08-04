"""Live agent sessions: the bridge between /ws/agent WebSockets and the Agent SDK.

The SDK client is injected as a factory so the whole streaming/permission flow
is testable with a fake. The real factory (``sdk_client_factory``) builds a
``ClaudeSDKClient`` bound to a folder, with the context-bridge MCP server and a
permission callback that round-trips to the UI.
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import structlog
from pydantic import BaseModel

from workbench_server.models.agents import (
    AgentError,
    PermissionRequest,
    SessionInfo,
    SessionState,
    StatusChange,
    TextDelta,
    ToolUseNote,
    TurnDone,
)

log = structlog.get_logger()

PERMISSION_TIMEOUT_S = 600.0
_LISTENER_QUEUE_LIMIT = 2000


class SdkClient(Protocol):
    """The slice of ClaudeSDKClient the session uses."""

    async def connect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def interrupt(self) -> None: ...
    async def disconnect(self) -> None: ...


PermissionAsk = Callable[[str, dict[str, Any]], Awaitable[bool]]
ClientFactory = Callable[[Path, str | None, PermissionAsk], SdkClient]


class AgentSession:
    """One live conversation bound to a folder. Emits typed events to listeners."""

    def __init__(
        self,
        local_id: str,
        folder: Path,
        folder_relative: str,
        factory: ClientFactory,
        resume_session_id: str | None = None,
    ) -> None:
        self.local_id = local_id
        self.folder = folder
        self.folder_relative = folder_relative
        self.sdk_session_id: str | None = resume_session_id
        self.state: SessionState = "idle"
        self.created_at: float = 0.0
        self._factory = factory
        self._client: SdkClient | None = None
        self._listeners: set[asyncio.Queue[BaseModel]] = set()
        self._pending_permissions: dict[str, asyncio.Future[bool]] = {}
        self._turn_task: asyncio.Task[None] | None = None

    # ---- listener plumbing -------------------------------------------------

    def subscribe(self) -> asyncio.Queue[BaseModel]:
        q: asyncio.Queue[BaseModel] = asyncio.Queue(maxsize=_LISTENER_QUEUE_LIMIT)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[BaseModel]) -> None:
        self._listeners.discard(q)

    def _emit(self, event: BaseModel) -> None:
        for q in self._listeners:
            if q.full():
                q.get_nowait()
            q.put_nowait(event)

    def _set_state(self, state: SessionState) -> None:
        if state != self.state:
            self.state = state
            self._emit(StatusChange(session_id=self.local_id, state=state))

    # ---- permissions -------------------------------------------------------

    async def _ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_permissions[request_id] = fut
        self._set_state("needs_attention")
        self._emit(
            PermissionRequest(
                request_id=request_id,
                tool=tool,
                description=_describe_tool_call(tool, tool_input),
            )
        )
        try:
            return await asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT_S)
        except TimeoutError:
            return False
        finally:
            self._pending_permissions.pop(request_id, None)
            self._set_state("working")

    def resolve_permission(self, request_id: str, allow: bool) -> None:
        fut = self._pending_permissions.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(allow)

    # ---- conversation ------------------------------------------------------

    async def _ensure_client(self) -> SdkClient:
        if self._client is None:
            self._client = self._factory(self.folder, self.sdk_session_id, self._ask_permission)
            await self._client.connect()
        return self._client

    def send_user_message(self, text: str) -> None:
        """Kick off one turn. Rejected while a turn is already running."""
        if self._turn_task is not None and not self._turn_task.done():
            self._emit(AgentError(message="agent is still working — interrupt it first"))
            return
        self._turn_task = asyncio.create_task(self._run_turn(text))

    async def _run_turn(self, text: str) -> None:
        self._set_state("working")
        try:
            client = await self._ensure_client()
            await client.query(text)
            async for message in client.receive_response():
                self._handle_sdk_message(message)
        except Exception as exc:  # surface, never crash the server
            log.exception("agent.turn_failed", session=self.local_id)
            self._emit(AgentError(message=str(exc)))
            self._emit(TurnDone(session_id=self.local_id, is_error=True))
        finally:
            self._set_state("idle")

    def _handle_sdk_message(self, message: Any) -> None:
        kind = type(message).__name__
        if kind == "StreamEvent":
            event = getattr(message, "event", None) or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    self._emit(TextDelta(text=str(delta.get("text", ""))))
        elif kind == "AssistantMessage":
            for block in getattr(message, "content", []) or []:
                if type(block).__name__ == "ToolUseBlock":
                    tool = str(getattr(block, "name", "tool"))
                    raw_input = getattr(block, "input", None)
                    tool_input = raw_input if isinstance(raw_input, dict) else {}
                    self._emit(
                        ToolUseNote(tool=tool, summary=_describe_tool_call(tool, tool_input))
                    )
        elif kind == "ResultMessage":
            sdk_id = getattr(message, "session_id", None)
            if isinstance(sdk_id, str):
                self.sdk_session_id = sdk_id
            cost = getattr(message, "total_cost_usd", None)
            self._emit(
                TurnDone(
                    session_id=self.local_id,
                    cost_usd=cost if isinstance(cost, int | float) else None,
                    is_error=bool(getattr(message, "is_error", False)),
                )
            )

    async def interrupt(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.interrupt()

    async def close(self) -> None:
        if self._turn_task is not None:
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn_task
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None


def _describe_tool_call(tool: str, tool_input: dict[str, Any]) -> str:
    for key in ("file_path", "path", "command", "pattern", "prompt", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{tool}: {value[:120]}"
    return tool


class SessionManager:
    def __init__(self, workspace_root: Path, factory: ClientFactory, max_sessions: int) -> None:
        self._root = workspace_root
        self._factory = factory
        self._max = max_sessions
        self._sessions: dict[str, AgentSession] = {}

    @property
    def sessions(self) -> dict[str, AgentSession]:
        return self._sessions

    def create(self, folder_relative: str, resume_session_id: str | None = None) -> AgentSession:
        live = sum(1 for s in self._sessions.values() if s.state != "idle")
        if live >= self._max:
            raise TooManySessionsError(self._max)
        folder = (self._root / folder_relative).resolve()
        if folder != self._root and self._root not in folder.parents:
            raise ValueError("folder escapes workspace")
        local_id = uuid.uuid4().hex[:12]
        session = AgentSession(
            local_id=local_id,
            folder=folder,
            folder_relative=folder_relative,
            factory=self._factory,
            resume_session_id=resume_session_id,
        )
        # time.time, not loop.time: create() is called from sync REST handlers too
        session.created_at = time.time()
        self._sessions[local_id] = session
        log.info("agent.session_created", local_id=local_id, folder=folder_relative)
        return session

    def get(self, local_id: str) -> AgentSession | None:
        return self._sessions.get(local_id)

    def live_infos(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                session_id=s.local_id,
                folder=s.folder_relative,
                state=s.state,
                live=True,
                title=f"live session ({s.state})",
                updated_at=s.created_at,
            )
            for s in self._sessions.values()
        ]

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()


class TooManySessionsError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"concurrent session limit ({limit}) reached")
