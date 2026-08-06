"""Attributing a file change to the agent session that made it.

Agents edit files directly on disk with their own tools, so a file can change
under the user with nothing on screen saying who did it. Two signals already
exist and neither is enough alone: a live session announces a tool call
(``Write``/``Edit``/…) naming a path, and moments later the watcher reports
that the file on disk changed. This service correlates them.

**The rules, in full.** An attribution is a claim about the user's own files,
and a confidently wrong one is worse than none:

* a change is attributed only to a session whose *file-writing* tool named
  **exactly that path** within :data:`ATTRIBUTION_WINDOW_S`;
* two sessions claiming the same path inside the window is a real ambiguity —
  the most recent exact match wins, and nothing else is inferred;
* no matching claim means the change is reported **unattributed**: the user, an
  external editor, a git checkout. Never the most recent session as a guess;
* a claim is *withdrawn* when the tool it came from turns out not to have
  written anything — the permission card was declined, or the tool came back an
  error (:meth:`ProvenanceService.note_tool_denied`,
  :meth:`ProvenanceService.note_tool_result`). A claim is announced *before* the
  tool runs, so without this a refused ``Write`` would sit there for the rest of
  the window and explain the user's own fix to that same file;
* a claim that *did* write is not consumed by the first change it explains. One
  logical write routinely surfaces as several watcher events on Windows, and
  calling the second one "the user" would be exactly the false claim we are
  avoiding;
* an unattributed change to a tracked path *clears* its entry — the file's last
  writer is no longer that agent;
* an entry dropped by the LRU is cleared on the wire too: a client holds its own
  map, and an entry we have forgotten can never be corrected later;
* deletions are ignored (see :meth:`ProvenanceService.note_file_change`).

**What it cannot know.** A path the agent spelled differently than the watcher
reports it (a different case on Windows, a path outside the workspace) does not
match and comes back unattributed. An agent that writes through a shell command
rather than a file tool is unattributed. A change from a git checkout, another
editor or a background process is unattributed — which is the correct answer,
not a gap.

State is in memory only: a server restart forgets every attribution, and the
map is bounded by :data:`MAX_TRACKED_PATHS` and :data:`MAX_PENDING_CLAIMS` so a
long session cannot grow it without limit.
"""

import asyncio
import contextlib
import os
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from workbench_server.models.files import FileChangedEvent
from workbench_server.models.provenance import (
    AgentAttribution,
    FileProvenanceEvent,
    ProvenanceEntry,
    ProvenanceMap,
)
from workbench_server.services.event_bus import EventBus

log = structlog.get_logger()

#: How long a file-writing tool call stays eligible to explain a file change.
#: Covers the watcher's debounce plus a slow write; short enough that a user's
#: own save a minute later can never be mistaken for the agent's.
ATTRIBUTION_WINDOW_S = 10.0

#: LRU cap on the attribution map — a long session cannot grow it without limit.
MAX_TRACKED_PATHS = 500

#: Cap on unmatched tool claims kept in flight. Claims also expire by age; this
#: bounds a burst (a MultiEdit over hundreds of files) between expiries.
MAX_PENDING_CLAIMS = 200

#: Tools that write a file. Matched case-insensitively on the last name segment,
#: so an MCP tool arriving as ``mcp__<server>__Write`` counts too.
WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit"})

#: Argument keys a write tool names its target with, most specific first.
_PATH_KEYS = ("file_path", "notebook_path", "path")


def is_write_tool(tool: str) -> bool:
    """Does this tool name mean "an agent wrote a file"?"""
    return tool.rsplit("__", 1)[-1].strip().lower() in WRITE_TOOLS


def workspace_relative(root: Path, base: Path, raw: str) -> str | None:
    """Normalize a tool's path argument to the form the watcher publishes.

    Handles what agents actually emit on Windows: backslashes, quotes, paths
    relative to the session's own folder (the agent's cwd, not the workspace
    root), and ``.``/``..`` segments. Returns None when the value is empty or
    lands outside the workspace — a write we could never see a watcher event
    for, so it must not sit in the claim list waiting to match something else.

    Purely textual (``normpath``, never ``resolve``): the file usually does not
    exist yet when the claim is recorded, and both roots are already resolved.
    """
    text = raw.strip().strip("\"'").strip()
    if not text:
        return None
    candidate = Path(text.replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = base / candidate
    normalized = Path(os.path.normpath(candidate))
    try:
        return normalized.relative_to(root).as_posix()
    except ValueError:
        return None


@dataclass(frozen=True)
class _Claim:
    """One "this path is about to change, and here is who is changing it"."""

    path: str
    at: float  # monotonic clock reading
    #: None = the user's own save through the files API. A user claim beats a
    #: stale agent claim for the same path, which is how "the user saved the
    #: file the agent had just written" stays the user's change.
    agent: AgentAttribution | None
    #: The SDK's ``tool_use`` id, so the tool's own result can withdraw it.
    #: None for a user claim and for an agent claim with no usable id.
    call_id: str | None = None


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    """Is the calling thread running ``loop`` right now?"""
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


class ProvenanceService:
    """Correlator + the in-memory map the UI reads.

    Deliberately lock-free: watcher events and the (async) REST handlers all run
    on the event loop, and every method that records a *claim* marshals itself
    onto the loop when it is called from anywhere else — which the sync file
    handlers are, since FastAPI runs those in a worker thread. Mutating the
    claim list, or publishing onto ``/ws/events`` subscriber queues, from off
    the loop is what that avoids: ``put_nowait`` there resolves a waiting
    getter through ``call_soon`` without touching the selector's wakeup pipe, so
    an idle server would sit on the frame until something else woke it.
    """

    def __init__(
        self,
        root: Path,
        bus: EventBus,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = root.resolve()
        self._bus = bus
        self._clock = clock
        self._claims: deque[_Claim] = deque(maxlen=MAX_PENDING_CLAIMS)
        self._entries: OrderedDict[str, ProvenanceEntry] = OrderedDict()
        self._queue: asyncio.Queue[BaseModel] | None = None
        self._task: asyncio.Task[None] | None = None
        #: The loop this service lives on; None before :meth:`start` (the unit
        #: tests drive it synchronously, with no loop at all).
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_workspace_root(self, root: Path) -> None:
        """Re-root, and **forget everything**.

        Both halves of this map are keyed by a workspace-relative path, so
        carrying them across a switch would attribute ``src/main.py`` in the
        project you just opened to an agent that wrote ``src/main.py`` in the
        one you left — a claim that is not merely stale but about a different
        file. Pending claims go for the same reason; the watcher events they
        were waiting for describe paths under the old root and will never come.

        Nothing is published. The client re-reads ``GET /api/provenance`` as part
        of adopting the new workspace, and an empty map is the correct answer.
        """
        self._root = root.resolve()
        self._claims.clear()
        self._entries.clear()

    def _dispatch(self, action: Callable[[], None]) -> None:
        """Run ``action`` on the event loop, from whichever thread we are on."""
        loop = self._loop
        if loop is None or _on_loop(loop):
            action()
            return
        loop.call_soon_threadsafe(action)

    # ---- signal 1: an agent announced a file-writing tool call --------------

    def note_tool_use(
        self,
        *,
        session_id: str,
        session_title: str,
        folder: Path,
        tool: str,
        tool_input: dict[str, Any],
        call_id: str | None = None,
    ) -> None:
        """Record a claim from a live session. Anything that is not a file
        write, or does not name a path inside the workspace, is dropped here —
        an unusable claim must never sit in the list matching later changes.

        ``call_id`` is the SDK's ``tool_use`` id: it is what lets the tool's own
        result withdraw this claim when the write never happened.
        """
        if not is_write_tool(tool):
            return
        path = self._claimed_path(folder, tool_input)
        if path is None:
            return
        claim = _Claim(
            path=path,
            at=self._clock(),
            agent=AgentAttribution(session_id=session_id, session_title=session_title, tool=tool),
            call_id=call_id,
        )
        self._dispatch(lambda: self._claims.append(claim))

    def note_tool_result(self, *, call_id: str, ok: bool) -> None:
        """Settle the claim a tool call made, once its result comes back.

        A *failed* call wrote nothing — an ``Edit`` whose old string was not
        found, a ``Write`` to a read-only path — so its claim is withdrawn. A
        claim is announced before the tool runs, so leaving it would let a write
        that never happened explain the *next* change to that path: the user
        making the same fix by hand, or `git checkout` putting the file back.

        A successful call keeps its claim: one logical write surfaces as several
        watcher events on Windows, and the result routinely arrives before the
        last of them.
        """
        if ok:
            return
        self._dispatch(lambda: self._withdraw(lambda claim: claim.call_id == call_id))

    def note_tool_denied(
        self,
        *,
        session_id: str,
        folder: Path,
        tool: str,
        tool_input: dict[str, Any],
    ) -> None:
        """The user declined this tool call in the permission card, so it never
        ran. Withdraws the newest claim this session made for that exact path.

        Belt and braces with :meth:`note_tool_result` — a denial normally comes
        back as an error result too — but this fires the moment the user clicks
        Deny, while the result frame depends on the SDK, and the gap between the
        two is precisely when a user who just refused a write is most likely to
        make the change themselves.
        """
        if not is_write_tool(tool):
            return
        path = self._claimed_path(folder, tool_input)
        if path is None:
            return
        self._dispatch(lambda: self._drop_newest(path, session_id))

    def _drop_newest(self, path: str, session_id: str) -> None:
        """Remove the newest claim this session holds on ``path``. Only the
        newest: a session that legitimately wrote the file earlier in the turn
        is still the author of *that* change."""
        for index in range(len(self._claims) - 1, -1, -1):
            claim = self._claims[index]
            if claim.path == path and claim.agent is not None:
                if claim.agent.session_id == session_id:
                    del self._claims[index]
                return

    def _withdraw(self, matches: Callable[[_Claim], bool]) -> None:
        kept = [claim for claim in self._claims if not matches(claim)]
        if len(kept) == len(self._claims):
            return
        self._claims.clear()
        self._claims.extend(kept)

    def note_user_write(self, relative_path: str) -> None:
        """Record a write Workbench itself made on the user's behalf: the
        editor's ``PUT /api/files/content``, a file created or renamed from the
        tree, a document flushed by the OnlyOffice save callback.

        Without this, any of those landing within seconds of an agent's write
        would be attributed to the agent: the claim is still inside the window
        and the watcher event looks identical. With it, the newer user claim
        wins and the file is correctly reported as the user's.

        Safe to call from a worker thread — the sync file handlers do.
        """
        path = workspace_relative(self._root, self._root, relative_path)
        if path is None:
            return
        # Stamped now, applied on the loop: the claim's time is when the user
        # saved, not whenever the loop next gets a slice.
        claim = _Claim(path=path, at=self._clock(), agent=None)
        self._dispatch(lambda: self._claims.append(claim))

    def _claimed_path(self, folder: Path, tool_input: dict[str, Any]) -> str | None:
        for key in _PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return workspace_relative(self._root, folder, value)
        return None

    # ---- signal 2: the watcher saw the file change -------------------------

    def note_file_change(self, event: FileChangedEvent) -> None:
        """Attribute one watcher event, and publish if what the UI shows changed.

        Deletions are skipped: on Windows one logical write surfaces as a
        delete+add burst whose order within a watchfiles batch is not
        guaranteed, so treating a delete as "this file is gone" would throw away
        the attribution for the very write that produced it. A file that really
        is deleted leaves an entry nobody can see (it is out of the tree) until
        the LRU evicts it.
        """
        if event.change == "deleted":
            return
        claim = self._match(event.path)
        if claim is not None and claim.agent is not None:
            entry = ProvenanceEntry(
                path=event.path, changed_at=time.time(), agent=claim.agent, acknowledged=False
            )
            self._store(entry)
            self._bus.publish(FileProvenanceEvent(entry=entry))
            log.info(
                "provenance.attributed",
                path=entry.path,
                session=claim.agent.session_id,
                tool=claim.agent.tool,
            )
            return
        # Unattributed. If we were claiming this path for an agent, that claim
        # is no longer true — clear it rather than let it go stale on screen.
        if self._entries.pop(event.path, None) is not None:
            self._bus.publish(
                FileProvenanceEvent(entry=ProvenanceEntry(path=event.path, changed_at=time.time()))
            )

    def _match(self, path: str) -> _Claim | None:
        """Most recent claim for exactly this path inside the window, or None.

        Exact match only, and claims are never consumed — both are deliberate;
        see the module docstring.
        """
        cutoff = self._clock() - ATTRIBUTION_WINDOW_S
        while self._claims and self._claims[0].at < cutoff:
            self._claims.popleft()
        for claim in reversed(self._claims):
            if claim.path == path:
                return claim
        return None

    def _store(self, entry: ProvenanceEntry) -> None:
        self._entries[entry.path] = entry
        self._entries.move_to_end(entry.path)
        while len(self._entries) > MAX_TRACKED_PATHS:
            evicted, _ = self._entries.popitem(last=False)
            # Say the eviction out loud. Clients keep their own copy of the map,
            # and a path we have forgotten can never be corrected afterwards —
            # the clear branch of note_file_change only fires for paths we still
            # hold — so a silent drop would leave a stale attribution on screen
            # for the rest of the session.
            self._bus.publish(
                FileProvenanceEvent(entry=ProvenanceEntry(path=evicted, changed_at=time.time()))
            )
            log.debug("provenance.evicted", path=evicted)

    # ---- what the UI reads -------------------------------------------------

    def snapshot(self) -> ProvenanceMap:
        """Every path currently attributed to a session (newest last)."""
        return ProvenanceMap(entries=list(self._entries.values()))

    def acknowledge(self, path: str) -> ProvenanceEntry | None:
        """The user opened or dismissed this change: keep the attribution, drop
        the "you have not seen this" marker. Unknown paths are a no-op — the UI
        acknowledges on every open and must not have to check first."""
        entry = self._entries.get(path)
        if entry is None or entry.acknowledged:
            return entry
        updated = entry.model_copy(update={"acknowledged": True})
        self._entries[path] = updated
        self._bus.publish(FileProvenanceEvent(entry=updated))
        return updated

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Subscribe to the bus the watcher publishes on. Publishing back onto
        the same bus is safe: our own frames are not ``FileChangedEvent``."""
        self._loop = asyncio.get_running_loop()
        self._queue = self._bus.subscribe()
        self._task = asyncio.create_task(self._run(), name="provenance")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._queue is not None:
            self._bus.unsubscribe(self._queue)
            self._queue = None
        self._loop = None

    async def _run(self) -> None:
        queue = self._queue
        if queue is None:  # pragma: no cover - start() always sets it
            return
        log.info("provenance.started")
        while True:
            event = await queue.get()
            if not isinstance(event, FileChangedEvent):
                continue
            try:
                self.note_file_change(event)
            except Exception:  # a dead task would stop attributing, silently
                log.exception("provenance.attribution_failed", path=event.path)
