"""Agent session schemas: REST shapes and the /ws/agent/{id} protocol."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from workbench_server.models.plans import PlanDecision, PlanPresented, PlanResolved

SessionState = Literal["idle", "working", "needs_attention"]

#: What a session *is*, which decides what it may do and how Mission Control
#: draws it.
#:
#: ``chat`` is every session Workbench has ever had, and stays the default on
#: every path. ``orchestrator`` is one the user started that carries the
#: mission-control toolset (``services/orchestrator.py``). ``worker`` is one an
#: orchestrator spawned — it carries no toolset of its own, which is what stops
#: an orchestrator building a tree of orchestrators out of one budget.
#:
#: ``reviewer`` is M6 staged review PR 2's: a **fresh-context, read-only** session
#: the ``review`` check puts in front of another session's diff
#: (``services/review.py``). This word is cheap and buys almost nothing on its
#: own — a reviewer row in the activity feed and Mission Control, and a place in
#: the concurrent-session cap. The isolation it *reads* like is built explicitly
#: in ``services/sdk_factory.py``: a toolset selected by kind, a kind-gated
#: auto-allow list, ``disallowed_tools``, and a deny-and-log answer to every
#: permission on both escalation paths. A branch that adds the word and stops
#: ships a reviewer that can still call ``office_write`` and ``Bash``.
SessionKind = Literal["chat", "orchestrator", "worker", "reviewer"]


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
    #: What this session is. A transcript on disk is always ``chat``: kinds are
    #: live state about a running process, not something the store records.
    kind: SessionKind = "chat"


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
    #: ``orchestrator`` gives this session the mission-control toolset. A client
    #: may **not** ask for ``worker``: a worker is bound to a pooled worktree
    #: outside the workspace jail, and only ``services/orchestrator.py`` — which
    #: holds the lease — is allowed to create one (``routers/agents.py``).
    kind: Literal["chat", "orchestrator"] = "chat"


# ---- permissions, fleet-wide -----------------------------------------------
#
# A permission prompt reaches the human over ``/ws/agent/{id}``, which exists
# only for conversations a window has opened. That is the gap Mission Control
# closes: a blocked agent in a session nobody is looking at is invisible until
# it times out ten minutes later, and with an orchestrator spawning workers the
# session that blocks is *by definition* one the user never opened.


class PendingPermission(BaseModel):
    """One prompt awaiting the human, addressed to the whole window.

    The same ``request_id``, ``tool`` and ``description`` as
    :class:`PermissionRequest` — answering from the board and answering from the
    chat are the same call into the same future, not two mechanisms.

    **The description is not redacted, and that is deliberate.** The activity
    feed jails every path it publishes because it is a firehose of things the
    user never asked to see. This is the opposite: an approval the user cannot
    read is one they cannot give informedly, and a permission card that hid the
    command it is about would be worse than no card. What crosses here is
    exactly what the session's own card shows.
    """

    request_id: str
    tool: str
    description: str
    #: Unix seconds when the agent blocked. The board ages it, because a prompt
    #: that has been waiting nine minutes is about to be denied by timeout.
    requested_at: float


class SessionPermissions(BaseModel):
    """Every prompt one session is blocked on, with enough to render a row."""

    session_id: str
    title: str
    folder: str
    kind: SessionKind = "chat"
    pending: list[PendingPermission] = Field(default_factory=list)


class PermissionsSnapshot(BaseModel):
    """``GET /api/agents/permissions``: the fleet's open prompts.

    Load and reconnect, in the same row shape :class:`SessionPermissionEvent`
    carries live — the pattern ``GET /api/activity`` established.
    """

    sessions: list[SessionPermissions] = Field(default_factory=list)


class SessionPermissionEvent(BaseModel):
    """Bus event: one session's set of open prompts changed.

    Rows, not deltas, and **always the whole set for that session** — including
    the empty set, which is how a board learns a prompt was answered somewhere
    else (or timed out). A delta would leave a card standing for a future that
    is already resolved, and the user would "allow" something the agent gave up
    on minutes ago.
    """

    type: Literal["session_permission"] = "session_permission"
    session: SessionPermissions


class PermissionAnswer(BaseModel):
    """``POST /api/agents/sessions/{id}/permission`` — the board's own answer.

    The same decision :class:`PermissionDecision` carries over the agent socket.
    A second door onto one future rather than a second mechanism: the board has
    no socket for the session it is answering for, and opening one to answer a
    single card would mean subscribing to a whole conversation to click Deny.
    """

    request_id: str
    allow: bool


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
