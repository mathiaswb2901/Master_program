"""Live agent sessions: the bridge between /ws/agent WebSockets and the Agent SDK.

The SDK client is injected as a factory so the whole streaming/permission/plan
flow is testable with a fake. The real factory (``sdk_client_factory``) builds a
``ClaudeSDKClient`` bound to a folder, with the context-bridge MCP server and the
callbacks that round-trip to the UI (permission prompts, plan cards).
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog
from pydantic import BaseModel

from workbench_server.models.agents import (
    AgentError,
    PendingPermission,
    PermissionRequest,
    SessionInfo,
    SessionKind,
    SessionPermissionEvent,
    SessionPermissions,
    SessionState,
    SessionStatusEvent,
    StatusChange,
    TextDelta,
    ToolSettled,
    ToolUseNote,
    TurnDone,
)
from workbench_server.models.plans import (
    OptionGroupNode,
    PlanArtifact,
    PlanPresented,
    PlanResolved,
    PlanResponse,
)
from workbench_server.services.plan_anchors import annotation_problems
from workbench_server.services.session_index import SessionIndex
from workbench_server.services.titles import FALLBACK_TITLE, derive_title

log = structlog.get_logger()

PERMISSION_TIMEOUT_S = 600.0
PLAN_TIMEOUT_S = 600.0
#: How long a settled plan's verdict stays replayable to a reconnecting client.
#: Long enough to cover a refresh or a dropped socket, short enough that a card
#: from an hour ago is not re-announced as news.
PLAN_RESOLUTION_REPLAY_S = 300.0
#: Hard cap on a tool result excerpt, applied before the frame is built.
TOOL_EXCERPT_LIMIT = 2000
_LISTENER_QUEUE_LIMIT = 2000


class SdkClient(Protocol):
    """The slice of ClaudeSDKClient the session uses."""

    async def connect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def interrupt(self) -> None: ...
    async def disconnect(self) -> None: ...


class EventPublisher(Protocol):
    """The slice of EventBus a session uses to fan status out to /ws/events."""

    def publish(self, event: BaseModel) -> None: ...


class SessionBridge(Protocol):
    """What a live session offers the client factory: every callback that has to
    reach the human, plus the one thing a tool needs to act *as* this session.
    Bundled into one object so adding the next one (plans were the second) does
    not widen the factory signature again."""

    #: This session's local id. The orchestrator toolset spawns, reads and stops
    #: workers **on behalf of one orchestrator**, and refuses to touch another's
    #: — so its tool bodies have to be able to name the session they are running
    #: inside (``services/orchestrator.py``). Not a callback, which is why it is
    #: the only non-callback member here.
    @property
    def session_id(self) -> str: ...

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool: ...
    async def present_plan(self, artifact: PlanArtifact) -> PlanResponse: ...


class ToolUseObserver(Protocol):
    """Told about every tool call, synchronously, through its whole life.

    The provenance correlator is the only implementor: it needs the *path* a
    write tool named, which the ``ToolUseNote`` frame deliberately does not
    carry (that frame is a chat row, not a machine-readable claim). A direct
    call rather than a bus event because this is an internal signal — every
    payload on ``/ws/events`` is something a client must act on.

    Announcement alone is not enough. A tool is announced *before* it runs, so
    the observer also has to hear how it ended: a declined permission card or an
    error result means nothing was written, and a claim left standing for a
    write that never happened would be credited with whatever changed that path
    next — usually the user's own edit.
    """

    def note_tool_use(
        self,
        *,
        session_id: str,
        session_title: str,
        folder: Path,
        tool: str,
        tool_input: dict[str, Any],
        call_id: str | None = None,
    ) -> None: ...

    def note_tool_result(self, *, call_id: str, ok: bool) -> None: ...

    def note_tool_denied(
        self,
        *,
        session_id: str,
        folder: Path,
        tool: str,
        tool_input: dict[str, Any],
    ) -> None: ...


class ActivityObserver(Protocol):
    """Told what this session is doing, as the chat frames for it are built.

    The third observer on this seam, and the one whose whole point is *reach*:
    ``ToolUseNote`` and ``ToolSettled`` go to the clients that opened this
    session's socket, so a window that has never opened this conversation sees
    nothing of it. The activity service republishes a bounded, jailed row on the
    shared bus, which is what makes "show me everywhere the fleet is working"
    answerable from one pane (``services/activity.py``).

    Distinct from :class:`ToolUseObserver` rather than folded into it, because
    the two want different things from the same moment: provenance wants the
    path a *write* tool named and drops everything else, while this wants every
    call — a Grep, a Bash, a Read — and never wants the result text. A single
    protocol serving both would have to carry the union and let each implementor
    ignore half.

    ``note_session`` is called at creation *and* when the first message derives
    the title, so a fleet with sessions that have run nothing still reads as a
    fleet rather than as an empty panel.
    """

    def note_session(
        self, *, session_id: str, title: str, folder: str, kind: SessionKind = "chat"
    ) -> None: ...

    def note_tool_started(
        self,
        *,
        session_id: str,
        session_title: str,
        folder: Path,
        folder_relative: str,
        call_id: str,
        tool: str,
        tool_input: dict[str, Any],
    ) -> None: ...

    def note_tool_settled(self, *, session_id: str, call_id: str, ok: bool) -> None: ...

    def note_session_gone(self, *, session_id: str) -> None: ...


class UsageObserver(Protocol):
    """Told what the SDK says about the account's limits, and what a turn cost.

    Two signals from the same stream, and both belong to the *account* rather
    than to this session — which is why they leave the session immediately
    instead of becoming frames on ``/ws/agent/{id}``. The service fans the
    resulting snapshot out on the shared bus, so every window sees the figure a
    turn in any one session produced.

    A direct call rather than a bus event for the same reason
    :class:`ToolUseObserver` is one: this is an internal hand-off of raw SDK
    values, not a payload a client acts on.
    """

    def note_rate_limit(self, info: Any) -> None: ...

    def note_turn(
        self, *, session_id: str, cost_usd: float | None, model_usage: Any = None
    ) -> None: ...


#: How a session gets its SDK client. ``kind`` is the fourth argument rather
#: than something the client discovers, because what a session *is* decides what
#: it may do — an orchestrator's toolset is built at construction time and there
#: is no later moment a chat session could acquire one (``sdk_factory.py``).
ClientFactory = Callable[[Path, str | None, SessionBridge, SessionKind], SdkClient]


class PlanAlreadyPendingError(Exception):
    """A second plan was presented while one is still awaiting a decision."""

    def __init__(self) -> None:
        super().__init__("a plan is already awaiting the user's decision")


@dataclass
class _PendingPlan:
    artifact: PlanArtifact
    future: "asyncio.Future[PlanResponse]"


def _no_decision(plan_id: str, reason: str) -> PlanResponse:
    """The only verdict silence may produce — never an implied approval."""
    return PlanResponse(plan_id=plan_id, verdict="no_decision", comment=reason)


def _unchosen_option_groups(artifact: PlanArtifact, response: PlanResponse) -> list[str]:
    """Option groups the response left without a selection."""
    return [
        node.node_id
        for node in artifact.nodes
        if isinstance(node, OptionGroupNode) and node.node_id not in response.choices
    ]


class AgentSession:
    """One live conversation bound to a folder. Emits typed events to listeners."""

    def __init__(
        self,
        local_id: str,
        folder: Path,
        folder_relative: str,
        factory: ClientFactory,
        resume_session_id: str | None = None,
        event_publisher: EventPublisher | None = None,
        tool_observer: ToolUseObserver | None = None,
        usage_observer: UsageObserver | None = None,
        activity_observer: ActivityObserver | None = None,
        kind: SessionKind = "chat",
    ) -> None:
        self.local_id = local_id
        self.folder = folder
        self.folder_relative = folder_relative
        self.kind = kind
        self.sdk_session_id: str | None = resume_session_id
        # Every SDK id this conversation has lived under. Claude Code mints a
        # NEW id on resume (and can again on later turns), leaving one on-disk
        # transcript per id — all of them are this conversation and must stay
        # suppressed in listings, not just the latest.
        self.sdk_session_ids: set[str] = set() if resume_session_id is None else {resume_session_id}
        self.state: SessionState = "idle"
        self.created_at: float = 0.0
        self.title: str | None = None  # derived from the first user message
        self._factory = factory
        self._publisher = event_publisher
        self._tool_observer = tool_observer
        self._usage_observer = usage_observer
        self._activity_observer = activity_observer
        self._client: SdkClient | None = None
        self._listeners: set[asyncio.Queue[BaseModel]] = set()
        self._pending_permissions: dict[str, asyncio.Future[bool]] = {}
        # The emitted frames for those futures, kept so a client that connects
        # after emission still gets the card (see pending_attention).
        self._pending_permission_events: dict[str, PermissionRequest] = {}
        # Wall clock per open prompt. Mission Control ages the card, because a
        # prompt that has waited nine minutes is one minute from being denied.
        self._permission_started_at: dict[str, float] = {}
        self._pending_plan: _PendingPlan | None = None
        # Last plan verdict and when it settled — replayed to a client that was
        # away while the card resolved (see pending_attention).
        self._last_plan_resolution: PlanResolved | None = None
        self._last_plan_resolved_at = 0.0
        self._turn_task: asyncio.Task[None] | None = None

    @property
    def session_id(self) -> str:
        """The :class:`SessionBridge` half of ``local_id`` — see that Protocol."""
        return self.local_id

    # ---- listener plumbing -------------------------------------------------

    def subscribe(self) -> asyncio.Queue[BaseModel]:
        q: asyncio.Queue[BaseModel] = asyncio.Queue(maxsize=_LISTENER_QUEUE_LIMIT)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[BaseModel]) -> None:
        self._listeners.discard(q)

    def pending_attention(self) -> list[BaseModel]:
        """Frames a fresh subscriber missed: still-open permission requests, the
        pending plan card, and the verdict of a recently settled one. Emission is
        fire-and-forget, so a client that connects (or reconnects) after the agent
        blocked would otherwise stare at a `needs_attention` session with nothing
        to answer.

        The settled verdict matters for the same reason as the open card: a
        client that sent a decision and lost the socket before ``plan_resolved``
        arrived would keep an answerable card for a plan the agent already acted
        on — and could "approve" it a second time.
        """
        events: list[BaseModel] = list(self._pending_permission_events.values())
        if self._pending_plan is not None:
            events.append(PlanPresented(plan=self._pending_plan.artifact))
        resolution = self._last_plan_resolution
        if (
            resolution is not None
            and time.monotonic() - self._last_plan_resolved_at <= PLAN_RESOLUTION_REPLAY_S
        ):
            events.append(resolution)
        return events

    def _emit(self, event: BaseModel) -> None:
        for q in self._listeners:
            if q.full():
                q.get_nowait()
            q.put_nowait(event)

    def _set_state(self, state: SessionState) -> None:
        if state == self.state:
            return
        self.state = state
        self._emit(StatusChange(session_id=self.local_id, state=state))
        # Also to the global bus: every client tracks every session's state,
        # including the ones it has no agent socket for.
        if self._publisher is not None:
            self._publisher.publish(
                SessionStatusEvent(
                    session_id=self.local_id, folder=self.folder_relative, state=state
                )
            )

    # ---- permissions (SessionBridge) ---------------------------------------

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        request = PermissionRequest(
            request_id=request_id,
            tool=tool,
            description=_describe_tool_call(tool, tool_input),
        )
        self._pending_permissions[request_id] = fut
        self._pending_permission_events[request_id] = request
        self._permission_started_at[request_id] = time.time()
        self._set_state("needs_attention")
        self._emit(request)
        # And to every window, not only the ones holding this conversation's
        # socket. Without this a blocked agent in a session nobody opened is
        # invisible until it times out ten minutes later — which is the normal
        # case once an orchestrator is spawning workers (Mission Control).
        self._publish_permissions()
        try:
            allowed = await asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT_S)
        except TimeoutError:
            log.info("agent.permission_timed_out", session=self.local_id, request=request_id)
            allowed = False
        finally:
            self._pending_permissions.pop(request_id, None)
            self._pending_permission_events.pop(request_id, None)
            self._permission_started_at.pop(request_id, None)
            self._restore_state_after_prompt()
            # Answered, timed out or abandoned — all three retract the card from
            # every board. The set is republished whole (see
            # ``SessionPermissionEvent``): a delta would leave a board offering
            # Allow for a future that is already resolved.
            self._publish_permissions()
        if not allowed and self._tool_observer is not None:
            # The tool will not run, so the claim its announcement made is not
            # true. Withdraw it now: a user who just refused a write is exactly
            # the person about to make that change themselves.
            self._tool_observer.note_tool_denied(
                session_id=self.local_id, folder=self.folder, tool=tool, tool_input=tool_input
            )
        return allowed

    def permissions(self) -> SessionPermissions:
        """Every prompt this session is blocked on, as the board reads them.

        Built from the same two maps ``pending_attention`` replays from, so a
        card on the board and a card in the chat are one prompt in two places
        rather than two records that can disagree.
        """
        return SessionPermissions(
            session_id=self.local_id,
            title=self.title or FALLBACK_TITLE,
            folder=self.folder_relative,
            kind=self.kind,
            pending=[
                PendingPermission(
                    request_id=request.request_id,
                    tool=request.tool,
                    description=request.description,
                    requested_at=self._permission_started_at.get(request.request_id, 0.0),
                )
                for request in self._pending_permission_events.values()
            ],
        )

    def _publish_permissions(self) -> None:
        if self._publisher is not None:
            self._publisher.publish(SessionPermissionEvent(session=self.permissions()))

    def resolve_permission(self, request_id: str, allow: bool) -> bool:
        """Answer one prompt. ``False`` when there was nothing to answer.

        The return value is what lets ``POST …/permission`` tell "denied" from
        "that card is gone" — the board is answering for a session it has no
        socket for, so a stale click has to come back as a 404 rather than as a
        silent success the user reads as a decision.
        """
        fut = self._pending_permissions.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(allow)
        return True

    def _abandon_pending_permissions(self, reason: str) -> None:
        """Deny every prompt still awaiting the human so ``can_use_tool`` unwinds
        now instead of blocking for the rest of ``PERMISSION_TIMEOUT_S``.

        Same reasoning as ``_abandon_pending_plan``: the SDK interrupt cannot
        unblock a tool call parked on a future of ours, so Stop (or a close) has
        to settle it here or the session sits in ``needs_attention`` for ten more
        minutes with a prompt the user already walked away from."""
        for request_id, fut in list(self._pending_permissions.items()):
            if not fut.done():
                fut.set_result(False)
                log.info(
                    "agent.permission_abandoned",
                    session=self.local_id,
                    request=request_id,
                    reason=reason,
                )
        # Belt and braces for the board. ``ask_permission``'s own ``finally``
        # normally retracts the card, but this is also the close path, where the
        # turn task is cancelled a moment later — and a board still offering
        # Allow for a session that no longer exists is the exact lie this whole
        # channel was added to prevent.
        self._pending_permission_events.clear()
        self._permission_started_at.clear()
        self._publish_permissions()

    def _restore_state_after_prompt(self) -> None:
        """Leave ``needs_attention`` once a prompt settles — but only back into a
        turn that is genuinely still running, and only when nothing else is still
        waiting on the human. An orphaned prompt firing its timeout after the
        turn ended used to flip an idle session to ``working`` permanently."""
        if self._turn_task is None or self._turn_task.done():
            return
        if self._pending_permissions or self._pending_plan is not None:
            return
        self._set_state("working")

    # ---- plans (SessionBridge) ---------------------------------------------

    async def present_plan(self, artifact: PlanArtifact) -> PlanResponse:
        """Show a plan card and block until the user decides.

        Mirrors the permission flow: emit, flip to ``needs_attention``, await a
        future under the same timeout discipline. Both silence (timeout) and an
        interrupt resolve to verdict ``no_decision`` — the agent must then stop
        and ask in chat. Nothing here ever produces an implied approval.

        One plan at a time per session: a second call while one is pending is an
        agent mistake and comes back as a tool error.
        """
        if self._pending_plan is not None:
            raise PlanAlreadyPendingError
        fut: asyncio.Future[PlanResponse] = asyncio.get_running_loop().create_future()
        self._pending_plan = _PendingPlan(artifact=artifact, future=fut)
        self._set_state("needs_attention")
        self._emit(PlanPresented(plan=artifact))
        try:
            response = await asyncio.wait_for(fut, timeout=PLAN_TIMEOUT_S)
        except TimeoutError:
            log.info("agent.plan_timed_out", session=self.local_id, plan=artifact.plan_id)
            response = _no_decision(artifact.plan_id, "no decision before the plan timed out")
        finally:
            self._pending_plan = None
            self._restore_state_after_prompt()
        # Tell every client the card is history — including the one that never
        # answered, and any second client still showing it as live. Kept for
        # replay too: the client that decided may have dropped before this
        # reached it (see pending_attention).
        resolution = PlanResolved(plan_id=artifact.plan_id, verdict=response.verdict)
        self._last_plan_resolution = resolution
        self._last_plan_resolved_at = time.monotonic()
        self._emit(resolution)
        return response

    def resolve_plan(self, response: PlanResponse) -> None:
        pending = self._pending_plan
        if pending is None or pending.artifact.plan_id != response.plan_id:
            log.warning("agent.plan_decision_ignored", session=self.local_id, reason="not_pending")
            return
        unchosen = _unchosen_option_groups(pending.artifact, response)
        if response.verdict == "approve" and unchosen:
            # "approve" means "proceed with the chosen options" — approving with
            # a group unanswered would hand the agent an implied approval for a
            # decision it explicitly asked the user to make, and it would guess.
            log.warning(
                "agent.plan_decision_ignored",
                session=self.local_id,
                reason="approve_without_choices",
                unchosen=unchosen,
            )
            return
        # Anchors are ours, not the agent's and not the user's prose: the
        # renderer emits them and this is where they are proven to point into
        # the artifact they claim. An anchor naming a row that is not there is a
        # malformed decision, not a note we quietly drop — the agent would read
        # it as being about something.
        problems = annotation_problems(pending.artifact, response.annotations)
        if problems:
            log.warning(
                "agent.plan_decision_ignored",
                session=self.local_id,
                reason="unresolvable_anchor",
                problems=problems,
            )
            # Not silent: the client that sent this keeps showing a card it
            # believes it answered, and the only honest thing is to say so.
            self._emit(AgentError(message=f"Plan decision rejected — {problems[0]}"))
            return
        if not pending.future.done():
            pending.future.set_result(response)

    def _abandon_pending_plan(self, reason: str) -> None:
        """Settle a pending plan as ``no_decision`` so the awaiting tool call
        unwinds and the session cannot stay stuck in ``needs_attention``."""
        pending = self._pending_plan
        if pending is not None and not pending.future.done():
            pending.future.set_result(_no_decision(pending.artifact.plan_id, reason))

    # ---- conversation ------------------------------------------------------

    async def _ensure_client(self) -> SdkClient:
        if self._client is None:
            self._client = self._factory(self.folder, self.sdk_session_id, self, self.kind)
            await self._client.connect()
        return self._client

    def send_user_message(self, text: str) -> None:
        """Kick off one turn. Rejected while a turn is already running."""
        if self._turn_task is not None and not self._turn_task.done():
            self._emit(AgentError(message="agent is still working — interrupt it first"))
            return
        if self.title is None:
            self.title = derive_title(text)
            # The fleet view names sessions, and this is the moment a session
            # stops being "new session" — say so once, rather than let the row
            # stay stale until the turn happens to call a tool.
            if self._activity_observer is not None:
                self._activity_observer.note_session(
                    session_id=self.local_id,
                    title=self.title,
                    folder=self.folder_relative,
                    kind=self.kind,
                )
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
                    # The SDK's tool_use id is what the result comes back under;
                    # a local id keeps the frame valid when there is none, and
                    # that row simply falls back to settling at turn_done.
                    raw_id = getattr(block, "id", None)
                    call_id = raw_id if isinstance(raw_id, str) and raw_id else uuid.uuid4().hex
                    self._emit(
                        ToolUseNote(
                            id=call_id, tool=tool, summary=_describe_tool_call(tool, tool_input)
                        )
                    )
                    # Announced before the tool runs, which is what lets the
                    # provenance correlator claim the file change that follows.
                    if self._tool_observer is not None:
                        self._tool_observer.note_tool_use(
                            session_id=self.local_id,
                            session_title=self.title or FALLBACK_TITLE,
                            folder=self.folder,
                            tool=tool,
                            tool_input=tool_input,
                            call_id=call_id,
                        )
                    # The same moment, fanned out fleet-wide: this frame reaches
                    # only the clients that opened *this* session's socket, and
                    # "everywhere Claude is editing" is a question about the
                    # windows that did not (services/activity.py).
                    if self._activity_observer is not None:
                        self._activity_observer.note_tool_started(
                            session_id=self.local_id,
                            session_title=self.title or FALLBACK_TITLE,
                            folder=self.folder,
                            folder_relative=self.folder_relative,
                            call_id=call_id,
                            tool=tool,
                            tool_input=tool_input,
                        )
        elif kind == "UserMessage":
            # Tool results arrive as a synthetic user turn (SDK naming — not a
            # message the human typed); each block settles exactly one row.
            for block in getattr(message, "content", []) or []:
                if type(block).__name__ != "ToolResultBlock":
                    continue
                raw_id = getattr(block, "tool_use_id", None)
                if not isinstance(raw_id, str) or not raw_id:
                    continue
                ok = not bool(getattr(block, "is_error", False))
                self._emit(
                    ToolSettled(
                        id=raw_id,
                        ok=ok,
                        output_excerpt=_result_excerpt(getattr(block, "content", None)),
                    )
                )
                # A failed tool wrote nothing; the correlator drops its claim so
                # it cannot be credited with the next change to that path.
                if self._tool_observer is not None:
                    self._tool_observer.note_tool_result(call_id=raw_id, ok=ok)
                # Only `ok` crosses to the fleet feed — never the excerpt above.
                if self._activity_observer is not None:
                    self._activity_observer.note_tool_settled(
                        session_id=self.local_id, call_id=raw_id, ok=ok
                    )
        elif kind == "RateLimitEvent":
            # The account's plan limits, as the CLI reports them when one of
            # them *transitions*. Nothing about it belongs to this conversation,
            # so it leaves here rather than becoming a frame on this session's
            # socket; the usage service publishes the snapshot on the shared bus
            # (services/usage.py).
            if self._usage_observer is not None:
                self._usage_observer.note_rate_limit(getattr(message, "rate_limit_info", None))
        elif kind == "ResultMessage":
            sdk_id = getattr(message, "session_id", None)
            if isinstance(sdk_id, str):
                self.sdk_session_id = sdk_id
                self.sdk_session_ids.add(sdk_id)
            cost = getattr(message, "total_cost_usd", None)
            cost_usd = cost if isinstance(cost, int | float) else None
            if self._usage_observer is not None:
                # What the turn cost, and per model. The only usage figure an
                # account that never emits a rate-limit event ever produces.
                self._usage_observer.note_turn(
                    session_id=self.local_id,
                    cost_usd=cost_usd,
                    model_usage=getattr(message, "model_usage", None),
                )
            self._emit(
                TurnDone(
                    session_id=self.local_id,
                    cost_usd=cost_usd,
                    is_error=bool(getattr(message, "is_error", False)),
                )
            )

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.local_id,
            folder=self.folder_relative,
            state=self.state,
            live=True,
            title=self.title or FALLBACK_TITLE,
            updated_at=self.created_at,
            kind=self.kind,
        )

    async def interrupt(self) -> None:
        # Settle first: the SDK interrupt cannot unblock a tool call that is
        # awaiting the human, so anything left pending — plan or permission —
        # would wedge the session in needs_attention after the user pressed Stop.
        self._abandon_pending_plan("the user interrupted the turn")
        self._abandon_pending_permissions("the user interrupted the turn")
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.interrupt()

    async def close(self) -> None:
        self._abandon_pending_plan("the session closed")
        self._abandon_pending_permissions("the session closed")
        # Leaves the fleet view now rather than lingering as a row nothing will
        # ever update — and the frame says so, because clients hold their own map.
        #
        # Guarded like every step below it. ``_activity_observer`` is a Protocol
        # this class does not own, and everything after this line *releases a
        # resource*: an exception escaping here would strand the turn task and
        # leave the SDK connection open. Worse at shutdown, where ``close_all``
        # walks the whole fleet through this method and the lifespan handler
        # awaits it before stopping the watcher, the PTYs and Office.
        if self._activity_observer is not None:
            try:
                self._activity_observer.note_session_gone(session_id=self.local_id)
            except Exception:
                log.exception("agent.activity_close_note_failed", session=self.local_id)
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


def _result_excerpt(content: Any) -> str:
    """Flatten an SDK tool result into capped plain text.

    The SDK hands back either a string or a list of content blocks (dicts or
    objects); anything else is not something we can show, so it comes through as
    an empty excerpt and the row simply has nothing to expand.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            value = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(value, str):
                parts.append(value)
        text = "\n".join(parts)
    else:
        text = ""
    text = text.strip()
    return text[:TOOL_EXCERPT_LIMIT] + "…" if len(text) > TOOL_EXCERPT_LIMIT else text


class SessionManager:
    def __init__(
        self,
        workspace_root: Path,
        factory: ClientFactory,
        max_sessions: int,
        session_index: SessionIndex | None = None,
        event_publisher: EventPublisher | None = None,
        tool_observer: ToolUseObserver | None = None,
        usage_observer: UsageObserver | None = None,
        activity_observer: ActivityObserver | None = None,
    ) -> None:
        self._root = workspace_root
        self._factory = factory
        self._max = max_sessions
        self._index = session_index
        self._publisher = event_publisher
        self._tool_observer = tool_observer
        self._usage_observer = usage_observer
        self._activity_observer = activity_observer
        self._sessions: dict[str, AgentSession] = {}

    @property
    def sessions(self) -> dict[str, AgentSession]:
        return self._sessions

    @property
    def max_concurrent(self) -> int:
        """The ceiling ``create`` enforces (``WORKBENCH_MAX_CONCURRENT_SESSIONS``)."""
        return self._max

    def active_count(self) -> int:
        """Sessions counted against that ceiling.

        The rule is *working*, not *open*: an idle session costs nothing, so it
        does not hold a slot. One definition, read by ``create`` below and by
        ``GET /api/agents/limits`` — the UI greys "New agent session" off this
        number, and two definitions would grey it at the wrong moment.
        """
        return sum(1 for s in self._sessions.values() if s.state != "idle")

    def create(
        self,
        folder_relative: str,
        resume_session_id: str | None = None,
        kind: SessionKind = "chat",
    ) -> AgentSession:
        """A session in a folder a *client* named — so the jail applies."""
        folder = (self._root / folder_relative).resolve()
        if folder != self._root and self._root not in folder.parents:
            raise ValueError("folder escapes workspace")
        return self.create_at(folder, folder_relative, resume_session_id, kind)

    def create_at(
        self,
        folder: Path,
        folder_label: str,
        resume_session_id: str | None = None,
        kind: SessionKind = "chat",
    ) -> AgentSession:
        """A session in a folder the **server** chose, jail not applied.

        The workspace jail on :meth:`create` exists to stop a *client-supplied*
        folder escaping the workspace. This entry point is reached from exactly
        one place — ``services/orchestrator.py``, with a path the worktree pool
        just handed it under a lease it holds — and a pooled slot is outside the
        workspace by design (``services/worktrees.py``: a slot inside it would
        put N copies of the project in the file tree). Routing a worker through
        :meth:`create` would therefore have meant either weakening the jail or
        not using the pool, and the pool exists for exactly this.

        Nothing reaches this from HTTP: ``POST /api/agents/sessions`` refuses
        ``kind="worker"`` at the schema and calls :meth:`create`, which is what
        ``test_orchestrator.py`` asserts rather than assumes.
        """
        if self.active_count() >= self._max:
            raise TooManySessionsError(self._max)
        local_id = uuid.uuid4().hex[:12]
        session = AgentSession(
            local_id=local_id,
            folder=folder,
            folder_relative=folder_label,
            factory=self._factory,
            resume_session_id=resume_session_id,
            event_publisher=self._publisher,
            tool_observer=self._tool_observer,
            usage_observer=self._usage_observer,
            activity_observer=self._activity_observer,
            kind=kind,
        )
        if resume_session_id is not None and self._index is not None:
            # A resumed conversation keeps the title of the transcript it continues.
            first = self._index.first_user_text(folder, resume_session_id)
            if first is not None:
                session.title = derive_title(first)
        # time.time, not loop.time: create() is called from sync REST handlers too
        session.created_at = time.time()
        self._sessions[local_id] = session
        # The fleet view lists sessions, not only busy ones: a session that has
        # run nothing yet is a row saying so, which is what makes an idle fleet
        # readable instead of an empty panel.
        if self._activity_observer is not None:
            self._activity_observer.note_session(
                session_id=local_id,
                title=session.title or FALLBACK_TITLE,
                folder=folder_label,
                kind=session.kind,
            )
        log.info("agent.session_created", local_id=local_id, folder=folder_label, kind=kind)
        return session

    def set_workspace_root(self, root: Path) -> None:
        """Follow a workspace switch. **Running sessions are not touched.**

        A live session is a `claude` process with its own cwd, possibly mid-turn
        and possibly holding an edit it has not written yet; the user opening
        another folder says nothing about whether that agent has finished. So the
        switch changes where *new* sessions are created and nothing else — the
        conversations keep running, their sockets keep working (they are keyed by
        session id, which no root enters into), and their transcripts stay where
        the SDK put them.

        What does change is how a survivor is *labelled*. ``folder_relative`` is
        a path relative to the workspace, and after a switch a session started in
        ``src`` of the old project would be listed under the new project's
        ``src`` — the same group as a folder it has nothing to do with. So a
        session whose folder is no longer inside the root is relabelled with its
        own absolute path, which cannot collide with any relative folder and
        reads, in the session list, as the other project it belongs to.
        """
        self._root = root.resolve()
        for session in self._sessions.values():
            session.folder_relative = self._label_for(session.folder)

    def _label_for(self, folder: Path) -> str:
        """``folder`` as ``SessionInfo.folder``: workspace-relative when it is
        inside the workspace, its own absolute path when it is not."""
        if folder == self._root:
            return ""
        if self._root in folder.parents:
            return folder.relative_to(self._root).as_posix()
        return folder.as_posix()

    def get(self, local_id: str) -> AgentSession | None:
        return self._sessions.get(local_id)

    def live_infos(self) -> list[SessionInfo]:
        return [s.info() for s in self._sessions.values()]

    def pending_permissions(self) -> list[SessionPermissions]:
        """Every session currently blocked on the human, oldest prompt first.

        The load-and-reconnect half of the fleet-wide permission channel: the
        live path (:class:`SessionPermissionEvent`) fires when a prompt opens or
        closes, and a board that connects in between would otherwise show an
        idle fleet while an agent sat blocked. Only sessions with something
        pending appear — an empty list is a fleet nobody is waiting on.
        """
        rows = [s.permissions() for s in self._sessions.values()]
        blocked = [row for row in rows if row.pending]
        blocked.sort(key=lambda row: min(p.requested_at for p in row.pending))
        return blocked

    def live_sdk_ids(self) -> set[str]:
        """Every SDK id any live session has consumed — their on-disk transcripts
        are the same conversations and must not be listed twice. The union (not
        just the latest id) matters: resuming mints a fresh id, and the resumed
        transcript would otherwise reappear after the first turn."""
        ids: set[str] = set()
        for session in self._sessions.values():
            ids |= session.sdk_session_ids
        return ids

    def live_by_sdk_id(self) -> dict[str, str]:
        """SDK transcript id -> the local id of the live session continuing it.

        :meth:`live_sdk_ids` answers "is this transcript already open"; this
        answers "and *where*". The conversation browser needs the second one:
        resuming a conversation that is already running would fork it into a
        second session with the same history, so the browser focuses the pane it
        is already in instead (a pane's identity is its local session id).
        """
        return {
            sdk_id: session.local_id
            for session in self._sessions.values()
            for sdk_id in session.sdk_session_ids
        }

    async def close_all(self) -> None:
        """Close every session, isolating each one.

        This is the shutdown path (``main.py``'s lifespan awaits it), so one
        session that cannot be closed must not keep the other sessions' SDK
        clients alive — nor stop the watcher, the PTYs and the Office host from
        being torn down after it. ``close`` already guards its own steps; this
        is the backstop for whatever it does not.
        """
        for session in list(self._sessions.values()):
            try:
                await session.close()
            except Exception:
                log.exception("agent.session_close_failed", local_id=session.local_id)
        self._sessions.clear()


class TooManySessionsError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"concurrent session limit ({limit}) reached")
