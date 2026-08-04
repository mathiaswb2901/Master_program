"""Agent session schemas: REST shapes and the /ws/agent/{id} protocol."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from workbench_server.models.plans import PlanDecision, PlanPresented, PlanResolved

SessionState = Literal["idle", "working", "needs_attention"]


class SessionInfo(BaseModel):
    session_id: str
    folder: str  # workspace-relative folder the session is bound to ("" = root)
    state: SessionState
    live: bool  # True = running in this process; False = resumable transcript on disk
    title: str  # first user message or a fallback
    updated_at: float  # unix mtime of the transcript (or creation time for live)


class FolderSessions(BaseModel):
    folder: str
    sessions: list[SessionInfo]


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
    type: Literal["tool_use"] = "tool_use"
    tool: str
    summary: str


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
    | PermissionRequest
    | PlanPresented
    | PlanResolved
    | StatusChange
    | TurnDone
    | AgentError
)
AgentServerMessage = Annotated[_AgentServerMessage, Field(discriminator="type")]
agent_server_message: TypeAdapter[_AgentServerMessage] = TypeAdapter(AgentServerMessage)
