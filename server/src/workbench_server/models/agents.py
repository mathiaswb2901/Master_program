"""Agent session schemas: REST shapes and the /ws/agent/{id} protocol."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from workbench_server.models.plans import PlanDecision, PlanPresented, PlanResolved

SessionState = Literal["idle", "working", "needs_attention"]


class SessionInfo(BaseModel):
    session_id: str
    # Workspace-relative folder the session is bound to ("" = root) — or, for a
    # live session that survived a workspace switch, its own absolute path. See
    # `SessionManager.set_workspace_root`: a running agent is not killed by the
    # user opening another folder, and labelling it with a relative path would
    # file it under a same-named folder of the project it has nothing to do with.
    folder: str
    state: SessionState
    live: bool  # True = running in this process; False = resumable transcript on disk
    title: str  # first user message or a fallback
    updated_at: float  # unix mtime of the transcript (or creation time for live)


class FolderSessions(BaseModel):
    folder: str
    sessions: list[SessionInfo]


class SessionStatusEvent(BaseModel):
    """Bus event: a live session changed state.

    Published on the in-process EventBus (fanned out on ``/ws/events``) rather
    than only on ``/ws/agent/{id}``: the agent socket exists solely for sessions
    the user has opened, so without this a session started elsewhere — or simply
    not on screen — never updates its dot, chip or attention badge.
    """

    type: Literal["session_status"] = "session_status"
    session_id: str
    folder: str  # workspace-relative folder the session is bound to ("" = root)
    state: SessionState


class SessionLimits(BaseModel):
    """The concurrency ceiling, and how close this workspace is to it.

    Served rather than re-derived in the UI on purpose: what the ceiling counts
    is sessions that are *working*, not sessions that are open, and a client
    guessing that rule from the listing would grey a button the server would
    have honoured. ``max_concurrent`` is ``WORKBENCH_MAX_CONCURRENT_SESSIONS``.
    """

    max_concurrent: int
    active: int


class CreateSessionRequest(BaseModel):
    folder: str = ""  # workspace-relative
    resume_session_id: str | None = None


class TranscriptMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class TranscriptResponse(BaseModel):
    session_id: str
    messages: list[TranscriptMessage]


class UiState(BaseModel):
    """Pushed by the frontend; served to agents via the context-bridge MCP tool."""

    active_file: str | None = None
    open_files: list[str] = Field(default_factory=list)
    dirty_files: list[str] = Field(default_factory=list)


# ---- client -> server over /ws/agent/{id} ----------------------------------


class UserMessage(BaseModel):
    type: Literal["user_message"]
    text: str


class PermissionDecision(BaseModel):
    type: Literal["permission_decision"]
    request_id: str
    allow: bool


class Interrupt(BaseModel):
    type: Literal["interrupt"]


AgentClientMessage = Annotated[
    UserMessage | PermissionDecision | PlanDecision | Interrupt, Field(discriminator="type")
]
agent_client_message: TypeAdapter[UserMessage | PermissionDecision | PlanDecision | Interrupt] = (
    TypeAdapter(AgentClientMessage)
)


# ---- server -> client over /ws/agent/{id} ----------------------------------


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolUseNote(BaseModel):
    """server -> client: the agent started a tool call.

    ``id`` is the SDK's ``tool_use`` id when there is one (minted locally
    otherwise) and is what :class:`ToolSettled` refers back to, so each row in
    the chat settles on its own result instead of the whole batch flipping at
    ``turn_done``.
    """

    type: Literal["tool_use"] = "tool_use"
    id: str = Field(min_length=1, max_length=200)
    tool: str
    summary: str


class ToolSettled(BaseModel):
    """server -> client: the result for one tool call arrived.

    ``output_excerpt`` is truncated to ``TOOL_EXCERPT_LIMIT`` characters by the
    session before it is ever put on the wire — a Grep over a monorepo is not a
    chat frame, and the expander is a glance, not a log viewer.
    """

    type: Literal["tool_settled"] = "tool_settled"
    id: str = Field(min_length=1, max_length=200)
    ok: bool
    output_excerpt: str = ""


class PermissionRequest(BaseModel):
    type: Literal["permission_request"] = "permission_request"
    request_id: str
    tool: str
    description: str


class StatusChange(BaseModel):
    type: Literal["status"] = "status"
    session_id: str
    state: SessionState


class TurnDone(BaseModel):
    type: Literal["turn_done"] = "turn_done"
    session_id: str
    cost_usd: float | None = None
    is_error: bool = False


class AgentError(BaseModel):
    type: Literal["agent_error"] = "agent_error"
    message: str


_AgentServerMessage = (
    TextDelta
    | ToolUseNote
    | ToolSettled
    | PermissionRequest
    | PlanPresented
    | PlanResolved
    | StatusChange
    | TurnDone
    | AgentError
)
AgentServerMessage = Annotated[_AgentServerMessage, Field(discriminator="type")]
agent_server_message: TypeAdapter[_AgentServerMessage] = TypeAdapter(AgentServerMessage)
