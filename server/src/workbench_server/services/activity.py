"""The fleet, live: what every agent session is touching right now.

**This is not provenance, and it must not become it.**
``services/provenance.py`` correlates a tool call with a *watcher event* to
answer "who wrote this file", after the fact, and it is deliberately
conservative — no match means no claim. This module consumes the same signal one
moment earlier, before anything has landed on disk, and answers a different
question: *what is happening this second, everywhere*. It attributes nothing,
corrects nothing and is allowed to forget; provenance stays the only authority
on authorship.

**The signal already exists.** Every session announces a tool call
(``ToolUseNote``) and settles it when the result arrives (``ToolSettled``).
Those frames go down ``/ws/agent/{id}`` — a socket only the windows that opened
that conversation have. This service is fed from the same two call sites through
the :class:`~workbench_server.services.agent_sessions.ActivityObserver` seam and
republishes a *fleet-wide* row on the shared bus, so a window with no socket for
a session still sees it working.

Four rules make that affordable and honest:

* **A rolling window with a stated cap.** :data:`MAX_ENTRIES_PER_SESSION` per
  session, :data:`MAX_SESSIONS` sessions, both served in the snapshot so the UI
  can say what it is showing. Everything evicted is counted.
* **A running call is the last thing evicted.** Eviction takes the oldest
  *settled* entry first and only falls back to the oldest overall when every
  entry is still running. Without that rule a two-minute ``Bash`` would be
  pushed out by the eight quick calls that followed it, and the panel's headline
  — what this agent is doing *now* — would go blank while it was still working.
* **Coalesced, on the policy ``terminal_stream.py`` proves.** The first change
  after a quiet fleet publishes immediately (a single tool call on an idle
  workbench appears at once); after that, at most one frame per
  :data:`ACTIVITY_WINDOW_S`, with every change in between folded into it. A
  session that fires forty tool calls in a burst contributes *one* row, holding
  its window as it ended up. The window is 250 ms rather than the terminal's
  8 ms because this is a feed a person reads: nobody perceives forty status-line
  changes a second, and the frame budget is what keeps a burst of agent output
  from becoming a re-render storm.
* **Jailed, and no results.** A fleet-wide feed discloses paths from every
  session at once, which is wider than the socket these frames came from, so
  every path argument is normalized workspace-relative (the same normalization
  provenance uses) and one that escapes the workspace is redacted rather than
  printed. Tool *results* never come here at all — only ``ok``.

State is in memory only. This is live state about processes, not workspace data:
a restart reports an empty fleet, which is the truth, and nothing is ever written
to ``.workbench/`` (the usage meters set that precedent).
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import structlog

from workbench_server.models.activity import (
    ActivityEntry,
    ActivitySnapshot,
    SessionActivity,
    SessionActivityEvent,
)
from workbench_server.models.agents import SessionKind
from workbench_server.services.event_bus import EventBus
from workbench_server.services.provenance import workspace_relative

log = structlog.get_logger()

#: Rolling window per session. Eight is "what it is doing and what it just did"
#: with room to see a short burst, and it is what makes the surface O(sessions)
#: rather than O(tool calls) — the retention decision this feature turns on.
MAX_ENTRIES_PER_SESSION: Final = 8

#: Sessions kept in the fleet view, LRU by last activity. Comfortably above
#: ``WORKBENCH_MAX_CONCURRENT_SESSIONS`` (8 at most today) so the cap is a
#: backstop against a long-lived server accumulating closed sessions, not
#: something a normal fleet meets.
MAX_SESSIONS: Final = 16

#: Cap on one summary line. Shorter than the chat row's 120 (``ToolUseNote``)
#: because this is a status line in a narrow pane, and because every one of
#: these rides a channel every window is listening to.
SUMMARY_LIMIT: Final = 100

#: Longest any change waits for the fleet to hear about it. See the module
#: docstring: a person reads this, so 250 ms of coalescing is invisible and
#: bounds the fan-out at four frames a second however hard the fleet works.
ACTIVITY_WINDOW_S: Final = 0.25

#: Argument keys that name a file. Same list, same order, same meaning as
#: ``provenance._PATH_KEYS`` — a tool that names its target one way there must
#: not name it another way here, or the two views would disagree about which
#: file an agent is on.
_PATH_KEYS: Final = ("file_path", "notebook_path", "path")

#: Argument keys worth reading at a glance when the call names no file. These
#: are *not* paths, so they are passed through capped rather than jailed.
_DETAIL_KEYS: Final = ("command", "pattern", "query", "url", "prompt")

#: What a path outside the workspace reads as. Named, never printed: the whole
#: point of the jail is that this feed cannot disclose a home directory.
OUTSIDE_WORKSPACE: Final = "(outside the workspace)"


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    """Is the calling thread running ``loop`` right now?"""
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def describe(
    root: Path,
    folder: Path,
    tool: str,
    tool_input: dict[str, Any],
    extra_roots: Sequence[tuple[str, Path]] = (),
) -> tuple[str, str | None]:
    """One capped, jailed line for this call, and the file it names (or None).

    Built here rather than reusing the chat frame's ``summary`` on purpose. That
    string is ``f"{tool}: {value[:120]}"`` over the *raw* argument, which for a
    ``Read`` of ``C:\\Users\\me\\.ssh\\config`` is exactly the disclosure this
    feed must not make to every window in the workspace.

    ``extra_roots`` are **server-owned** directories outside the workspace that
    this feed may still *name*: today exactly one, the worktree pool root, so a
    Mission Control worker reads as ``Read: slot-01/server/main.py`` rather than
    as a row of ``(outside the workspace)`` (``services/orchestrator.py``). The
    jail is not widened by it — the second element of the tuple, the path the UI
    makes clickable, stays ``None`` for anything outside the workspace, and a
    path in neither the workspace nor a named root is still redacted. Naming is
    what the board needs; opening is what the jail governs, and they are
    separate answers.
    """
    for key in _PATH_KEYS:
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw:
            target = workspace_relative(root, folder, raw)
            if target is not None:
                return f"{tool}: {target}"[:SUMMARY_LIMIT], target
            for label, extra in extra_roots:
                inside = workspace_relative(extra, folder, raw)
                if inside is not None:
                    shown = f"{label}/{inside}" if label else inside
                    return f"{tool}: {shown}"[:SUMMARY_LIMIT], None
            return f"{tool}: {OUTSIDE_WORKSPACE}"[:SUMMARY_LIMIT], None
    for key in _DETAIL_KEYS:
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw:
            return f"{tool}: {raw}"[:SUMMARY_LIMIT], None
    return tool[:SUMMARY_LIMIT], None


@dataclass
class _Window:
    """One session's rolling window. Ordered by the moment each call started."""

    session_id: str
    folder: str
    title: str
    active_at: float
    kind: SessionKind = "chat"
    entries: OrderedDict[str, ActivityEntry] = field(default_factory=OrderedDict)
    dropped: int = 0

    def row(self) -> SessionActivity:
        """The wire shape: newest first, which is the reading order."""
        return SessionActivity(
            session_id=self.session_id,
            folder=self.folder,
            title=self.title,
            kind=self.kind,
            entries=list(reversed(self.entries.values())),
            dropped=self.dropped,
            active_at=self.active_at,
        )

    def evict(self, cap: int) -> None:
        """Trim to ``cap``, taking a settled entry before a running one.

        A running entry is the answer to "what is this agent doing", so it is
        the last thing this window gives up — see the module docstring.
        """
        while len(self.entries) > cap:
            victim = next(
                (key for key, entry in self.entries.items() if entry.settled_at is not None),
                None,
            )
            if victim is None:
                victim = next(iter(self.entries))
            del self.entries[victim]
            self.dropped += 1


class ActivityService:
    """The fleet's rolling window, and the coalescer that publishes it.

    Deliberately lock-free, and every mutation runs **on the event loop**. Most
    callers are already there (``AgentSession`` handles SDK messages inside its
    own turn task), but ``SessionManager.create`` is reached from FastAPI's sync
    ``POST /api/agents/sessions`` handler, which runs in a worker thread — so
    every entry point marshals, exactly as the provenance correlator does. Two
    things would be wrong off the loop: ``loop.call_later`` is not thread-safe,
    and ``put_nowait`` onto a subscriber queue from another thread resolves a
    waiting getter through ``call_soon`` without touching the selector's wake-up
    pipe, so an idle server would sit on the frame until something else woke it.
    """

    def __init__(
        self,
        root: Path,
        bus: EventBus,
        clock: Callable[[], float] = time.time,
        window_s: float = ACTIVITY_WINDOW_S,
        max_entries: int = MAX_ENTRIES_PER_SESSION,
        max_sessions: int = MAX_SESSIONS,
        extra_roots: Sequence[tuple[str, Path]] = (),
    ) -> None:
        self._root = root.resolve()
        #: Server-owned directories outside the workspace this feed may *name*
        #: but never make clickable — see :func:`describe`. One today: the
        #: worktree pool root, so a Mission Control worker's row says which slot
        #: and file it is on instead of ``(outside the workspace)``.
        self._extra_roots = [(label, path.resolve()) for label, path in extra_roots]
        self._bus = bus
        #: Wall clock, for the stamps the UI renders as "3s ago". Injectable
        #: because ordering and ageing are rules a test has to be able to drive.
        self._clock = clock
        self._window_s = window_s
        self._max_entries = max_entries
        self._max_sessions = max_sessions
        #: LRU by last activity: most recently active last, like the provenance
        #: map. Oldest is what the fleet cap drops.
        self._windows: OrderedDict[str, _Window] = OrderedDict()
        self._dirty: set[str] = set()
        self._removed: set[str] = set()
        self._dropped_sessions = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: asyncio.TimerHandle | None = None
        #: Monotonic, deliberately not ``self._clock``: this decides when the
        #: next frame is due, and a wall clock stepping backwards would stall
        #: the feed until real time caught up. The stamps stay on wall time
        #: because the browser renders them against its own.
        self._last_publish = float("-inf")

    def _dispatch(self, action: Callable[[], None]) -> None:
        """Run ``action`` on the event loop, from whichever thread we are on."""
        loop = self._loop
        if loop is None or _on_loop(loop):
            action()
            return
        loop.call_soon_threadsafe(action)

    # ---- the signal, from the two call sites that build the chat frames -----

    def note_session(
        self, *, session_id: str, title: str, folder: str, kind: SessionKind = "chat"
    ) -> None:
        """A session exists (or has just been named).

        Called when a session is created and again when its first message
        derives its title, so the fleet view lists a session that has not run a
        tool yet — an idle fleet is the common case and "three sessions open,
        none touching anything" is a reading, while an empty panel is not.

        ``kind`` is stamped once, at creation: it is what lets Mission Control
        tell an orchestrator from a chat before either has run a tool.
        """
        # Stamped now, applied on the loop: the row's age is when the session was
        # created, not whenever the loop next gets a slice.
        at = self._clock()
        self._dispatch(lambda: self._apply_session(session_id, title, folder, kind, at))

    def _apply_session(
        self, session_id: str, title: str, folder: str, kind: SessionKind, at: float
    ) -> None:
        window = self._windows.get(session_id)
        if window is None:
            self._windows[session_id] = _Window(
                session_id=session_id, folder=folder, title=title, kind=kind, active_at=at
            )
        elif window.title == title:
            return  # nothing a client would render differently
        else:
            window.title = title
        self._touch(session_id)

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
    ) -> None:
        """A tool call was announced. Announced, not finished: this is the
        moment the whole feature exists for."""
        now = self._clock()
        # Jailed here, before the value is stored anywhere — the raw argument
        # never reaches the window, so no later code path can leak it.
        summary, target = describe(self._root, folder, tool, tool_input, self._extra_roots)
        entry = ActivityEntry(
            entry_id=call_id, tool=tool, summary=summary, target=target, started_at=now
        )
        self._dispatch(
            lambda: self._apply_started(session_id, session_title, folder_relative, entry, now)
        )

    def _apply_started(
        self, session_id: str, title: str, folder: str, entry: ActivityEntry, now: float
    ) -> None:
        window = self._windows.get(session_id)
        if window is None:
            window = _Window(session_id=session_id, folder=folder, title=title, active_at=now)
            self._windows[session_id] = window
        window.title = title
        window.folder = folder
        window.entries[entry.entry_id] = entry
        window.entries.move_to_end(entry.entry_id)
        window.evict(self._max_entries)
        window.active_at = now
        self._touch(session_id)

    def note_tool_settled(self, *, session_id: str, call_id: str, ok: bool) -> None:
        """A result arrived. Patches the entry **where it stands** — the list is
        ordered by when calls started, and a settle that reordered it would
        shuffle rows under someone reading them.

        A settle for an entry this window has already evicted is dropped: it is
        not resurrected (that would break both the cap and the ordering) and no
        frame is published for it, since nothing a client renders changed.
        """
        now = self._clock()
        self._dispatch(lambda: self._apply_settled(session_id, call_id, ok, now))

    def _apply_settled(self, session_id: str, call_id: str, ok: bool, now: float) -> None:
        window = self._windows.get(session_id)
        entry = None if window is None else window.entries.get(call_id)
        if window is None or entry is None:
            log.debug("activity.settle_for_evicted_entry", session=session_id, call=call_id)
            return
        window.entries[call_id] = entry.model_copy(update={"settled_at": now, "ok": ok})
        window.active_at = now
        self._touch(session_id)

    def note_session_gone(self, *, session_id: str) -> None:
        """The session closed. It leaves the fleet view, and the frame says so —
        clients hold their own copy of this map."""
        self._dispatch(lambda: self._apply_gone(session_id))

    def _apply_gone(self, session_id: str) -> None:
        if self._windows.pop(session_id, None) is None:
            return
        self._dirty.discard(session_id)
        self._removed.add(session_id)
        self._schedule()

    # ---- the fleet cap ------------------------------------------------------

    def _touch(self, session_id: str) -> None:
        self._windows.move_to_end(session_id)
        while len(self._windows) > self._max_sessions:
            evicted, _ = self._windows.popitem(last=False)
            self._dirty.discard(evicted)
            self._removed.add(evicted)
            self._dropped_sessions += 1
            log.debug("activity.session_evicted", session=evicted)
        self._dirty.add(session_id)
        self._schedule()

    # ---- publishing ---------------------------------------------------------

    def _schedule(self) -> None:
        """Publish now, or arrange for one frame at the end of the window.

        With no loop (the unit tests, which drive this synchronously) every
        change publishes immediately — the coalescing is a property of the
        clock, and a test that wants to assert it runs a real loop.
        """
        loop = self._loop
        if loop is None:
            self.flush()
            return
        if self._timer is not None:
            return  # a frame is already due; this change rides it
        since = time.monotonic() - self._last_publish
        if since >= self._window_s:
            self.flush()
            return
        self._timer = loop.call_later(self._window_s - since, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        self.flush()

    def flush(self) -> None:
        """Emit one frame for everything that has changed, if anything has."""
        if not self._dirty and not self._removed:
            return
        rows = [
            self._windows[session_id].row()
            for session_id in self._dirty
            if session_id in self._windows
        ]
        rows.sort(key=lambda row: row.active_at, reverse=True)
        self._bus.publish(SessionActivityEvent(sessions=rows, removed=sorted(self._removed)))
        self._dirty.clear()
        self._removed.clear()
        self._last_publish = time.monotonic()

    # ---- what the UI reads --------------------------------------------------

    def snapshot(self) -> ActivitySnapshot:
        """The whole fleet, most recently active first — initial load and
        reconnect, in the same row shape the bus event carries."""
        rows = sorted(
            (window.row() for window in self._windows.values()),
            key=lambda row: row.active_at,
            reverse=True,
        )
        return ActivitySnapshot(
            sessions=rows,
            max_entries_per_session=self._max_entries,
            max_sessions=self._max_sessions,
            dropped_sessions=self._dropped_sessions,
        )

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bind to the running loop, which is what enables the coalescing timer.
        Nothing is subscribed: this service is *fed*, not a bus consumer."""
        self._loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        """Cancel a pending frame. Deliberately does not flush: the server is
        going away, and a frame nobody can receive is not worth a wake-up."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._loop = None
