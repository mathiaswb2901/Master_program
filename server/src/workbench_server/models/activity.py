"""Live agent activity: what every session is touching *right now*.

The other end of provenance. ``models/provenance.py`` answers "who changed this
file", after the fact, conservatively — a claim about the user's own files, so it
would rather say nothing than say the wrong thing. This answers "what is
happening this second", across the whole fleet, whether or not the window has a
socket open for that conversation. Same signal, an earlier moment, a different
promise: this one is allowed to be *incomplete* (a bounded window that forgets),
because nothing downstream treats it as a claim about a file's author.

Three properties are in the schema rather than in a comment, because each one is
a promise a later edit could quietly break:

1. **Bounded by construction.** A session keeps at most
   :data:`~workbench_server.services.activity.MAX_ENTRIES_PER_SESSION` entries
   and the fleet at most
   :data:`~workbench_server.services.activity.MAX_SESSIONS` sessions. What falls
   out is *counted* (``dropped``, ``dropped_sessions``), never silently lost —
   a feed that quietly forgets is a feed you cannot read a number off.
2. **Jailed.** A fleet-wide feed is wider than the per-session socket these
   frames came from, so every path is normalized workspace-relative and one that
   escapes the workspace is redacted. ``target`` is therefore always a path the
   window may open, or ``None``.
3. **No results.** ``ToolSettled.output_excerpt`` never reaches this feed; a
   settled entry carries ``ok`` and nothing else. The excerpt stays on the
   socket belonging to the conversation that produced it.

Nothing here is persisted — this is live state, like ``models/usage.py``, and a
restart honestly reports an empty fleet.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ActivityEntry(BaseModel):
    """One tool call, from the moment it was announced to the moment it settled.

    The same row twice, updated in place rather than appended to: a call is
    announced *before* it runs (that is what makes a live view live) and the
    result patches this entry by ``entry_id``.
    """

    #: The SDK's ``tool_use`` id — the same id ``ToolUseNote``/``ToolSettled``
    #: use, so a settle finds its start without a second correlation rule.
    entry_id: str = Field(min_length=1, max_length=200)
    tool: str
    #: One capped line: the tool and, where it named one, its jailed target.
    #: Built here rather than passed through from the chat frame, because that
    #: one may carry an absolute path from outside the workspace.
    summary: str
    #: Workspace-relative path this call names, or None when it named none (or
    #: named one outside the workspace). The UI makes exactly this clickable.
    target: str | None = None
    #: Unix seconds when the call was announced.
    started_at: float
    #: Unix seconds when its result arrived; None while it is still running.
    settled_at: float | None = None
    #: Whether the result was an error. None while it is still running — which is
    #: the distinction the whole panel is built on, so it is not a bool.
    ok: bool | None = None


class SessionActivity(BaseModel):
    """One session's row in the fleet view: its bounded window, newest first."""

    session_id: str
    #: Workspace-relative folder the session is bound to ("" = root).
    folder: str
    #: The session's own title, as ``SessionInfo`` reports it. Carried here so
    #: this endpoint alone answers the whole fleet view — a window that has
    #: never listed sessions still renders readable rows.
    title: str
    #: Newest first, capped. Entries keep the order their calls *started* in;
    #: a settle patches an entry where it stands rather than moving it, so the
    #: list never reshuffles under a reader.
    entries: list[ActivityEntry] = Field(default_factory=list)
    #: Entries this session's window has dropped since the server started.
    #: Shown, because "and 40 more" is information and a silent gap is not.
    dropped: int = 0
    #: Unix seconds of the most recent thing that happened here — creation, a
    #: tool starting, a tool settling. The fleet is ordered by it.
    active_at: float


class ActivitySnapshot(BaseModel):
    """``GET /api/activity``: the whole fleet, most recently active first.

    Serves initial load and reconnect, exactly as ``GET /api/usage`` does for the
    usage meters — the live path (:class:`SessionActivityEvent`) carries the same
    :class:`SessionActivity` rows, so the client has one renderer and one merge.
    """

    sessions: list[SessionActivity] = Field(default_factory=list)
    #: The caps this snapshot was built under, served rather than restated in
    #: the UI: a panel that says "8 most recent" has to be told the 8.
    max_entries_per_session: int
    max_sessions: int
    #: Sessions the fleet-wide LRU has dropped. Nonzero means the *view* is
    #: incomplete, which is a different statement from a session's own
    #: ``dropped`` and belongs on the fleet rather than on a row.
    dropped_sessions: int = 0


class SessionActivityEvent(BaseModel):
    """Bus event: some sessions' activity changed.

    Published on the shared ``EventBus`` (fanned out on ``/ws/events``) rather
    than only on ``/ws/agent/{id}``, for the same reason as
    :class:`~workbench_server.models.agents.SessionStatusEvent`: the agent socket
    exists only for conversations the user has opened, so without this a window
    sees activity for the one chat it is looking at and nothing else.

    **Carries rows, not deltas, and only the rows that changed.** Coalescing
    happens before this is built (``services/activity.py``), so a session that
    fired forty tool calls inside one window contributes one row here with its
    window already updated — the frame count is bounded by the clock, not by how
    hard the fleet is working.
    """

    type: Literal["session_activity"] = "session_activity"
    #: Sessions whose window changed since the last frame, most recently active
    #: first. Never the whole fleet unless the whole fleet moved.
    sessions: list[SessionActivity] = Field(default_factory=list)
    #: Sessions that are no longer in the fleet view — closed, or dropped by the
    #: LRU. Said out loud for the reason ``provenance`` says its evictions out
    #: loud: clients hold their own copy of this map, and a row we have
    #: forgotten can never be corrected later.
    removed: list[str] = Field(default_factory=list)
