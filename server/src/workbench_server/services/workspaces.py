"""Switching the workspace at runtime, and remembering where you have been.

Until M5 the workspace was whatever directory the server was launched from, and
opening another project meant killing the server and restarting it with an
environment variable. This service makes the root a thing the app can change.

**One root, one write.** Everything rooted in the workspace holds that root in
one of two ways: it keeps the :class:`~workbench_server.services.workspace.Workspace`
object (the routers, the office services), or it copied the path into a field of
its own at construction (the watcher, layouts, shortcuts, provenance, sessions,
the worktree pool). The first kind follows for free — re-pointing one object
re-roots every caller at once. The second kind each owe a ``set_workspace_root``,
and :meth:`WorkspaceService.switch` calls **all** of them in one place. That list
being in one function is the point: a service added later that copies the root
and is not added here is a service that keeps serving the old project, and the
symptom is data from the wrong workspace rather than a crash.

**The jail does not move.** ``Workspace.safe_path`` is still the only way a wire
path becomes a filesystem path, and it still answers "inside the root or not".
What a switch changes is which root it asks about — so a path from the workspace
you just left is refused by exactly the rule that has always refused ``../``.
That is asserted rather than described (``test_workspaces.py``).

**Order matters, and it is: jail first, watcher last.** The jail is re-rooted
before anything else so that no request in flight can be answered against a mix
of the two; the watcher is restarted last so its first events describe a tree the
rest of the server already agrees about.

**And one switch at a time.** That order is only an order if nothing runs
between its steps, so the whole sequence is held under one lock
(:meth:`WorkspaceService.switch`). Two windows on one server is a supported
arrangement, and two switches in one tick would otherwise interleave into a
server whose jail and whose watch are pointed at different projects.

**Recents are the user's, not a project's.** They go under the machine's app data
dir (``services/app_data.py``), never into a ``.workbench/`` folder — a list of
the projects you work on is data about *you*, and writing it into one of those
projects would both commit your other clients' folder names into a git repo and
make the list disagree with itself depending on where you happened to be.
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Protocol

import structlog

from workbench_server.models.workspaces import (
    MAX_RECENT_WORKSPACES,
    MAX_RECENTS_FILE_BYTES,
    RECENTS_VERSION,
    RecentWorkspacesFile,
    WorkspaceChangedEvent,
    WorkspaceRef,
    WorkspaceState,
)
from workbench_server.services.app_data import app_data_dir
from workbench_server.services.event_bus import EventBus
from workbench_server.services.workspace import Workspace

log = structlog.get_logger()

RECENTS_FILE = "workspaces.json"


class WorkspaceRefusedError(Exception):
    """A root that cannot be served, with a sentence the user can act on."""


class SyncRootable(Protocol):
    """A service that copied the workspace root and re-points synchronously."""

    def set_workspace_root(self, root: Path) -> None: ...


class AsyncRootable(Protocol):
    """A service whose re-rooting restarts something and must be awaited."""

    async def set_workspace_root(self, root: Path) -> None: ...


def _name_of(root: Path) -> str:
    """The folder's own name, or the path when it has none (a drive root)."""
    return root.name or str(root)


def validate_root(candidate: Path) -> Path:
    """Resolve ``candidate`` and refuse anything that cannot be a workspace.

    Three refusals, each with its own message because each has a different fix:
    it is not there, it is a file, or the OS will not let us read it. The last
    one is checked by *listing* it rather than by reading a permission bit —
    ``os.access`` on Windows reports the read-only attribute and not the ACL, so
    it would happily approve a directory the next ``scandir`` cannot open.
    """
    try:
        root = candidate.expanduser().resolve()
    except (OSError, RuntimeError) as err:  # RuntimeError: ~ with no home
        raise WorkspaceRefusedError(f"{candidate} is not a usable path: {err}") from err
    if not root.exists():
        raise WorkspaceRefusedError(f"{root} does not exist")
    if not root.is_dir():
        raise WorkspaceRefusedError(f"{root} is a file, not a folder")
    try:
        with os.scandir(root) as scan:
            next(iter(scan), None)
    except OSError as err:
        raise WorkspaceRefusedError(f"{root} cannot be read: {err.strerror or err}") from err
    return root


class RecentsStore:
    """The recent-workspaces list on disk. Never raises — see the module note.

    Losing this file costs the user a list they can rebuild by opening a folder;
    it must never cost them a server that will not start, so every failure here
    resolves to "empty, plus a sentence saying why".
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._path = (directory or app_data_dir()) / RECENTS_FILE
        self._problem: str | None = None
        self._refs: list[WorkspaceRef] = []
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def problem(self) -> str | None:
        self._ensure_loaded()
        return self._problem

    def entries(self) -> list[WorkspaceRef]:
        """Most recent first, each stamped with whether it is still there.

        ``exists`` is re-checked on every read rather than cached: a folder on a
        drive that is not plugged in comes back when it is, and a picker that
        remembered "missing" from startup would keep refusing a workspace that
        is available now. It is one ``is_dir`` per row on a list capped at
        `MAX_RECENT_WORKSPACES`.
        """
        self._ensure_loaded()
        return [ref.model_copy(update={"exists": self._exists(ref.path)}) for ref in self._refs]

    def record(self, root: Path, *, at: float | None = None) -> None:
        """Put ``root`` at the front, deduped, capped, and write it out."""
        self._ensure_loaded()
        key = self._key(str(root))
        kept = [ref for ref in self._refs if self._key(ref.path) != key]
        self._refs = [
            WorkspaceRef(path=str(root), name=_name_of(root), opened_at=at or time.time()),
            *kept,
        ][:MAX_RECENT_WORKSPACES]
        self._save()

    # ---- disk ---------------------------------------------------------------

    @staticmethod
    def _key(path: str) -> str:
        """Identity of a workspace path. Case-folded because Windows paths are
        case-insensitive, and ``C:\\Work`` and ``c:\\work`` being two rows in the
        recent list is the kind of small wrongness that reads as a broken app."""
        return os.path.normcase(path)

    @staticmethod
    def _exists(path: str) -> bool:
        try:
            return Path(path).is_dir()
        except OSError:  # an unmounted drive letter, a path the OS rejects
            return False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._refs, self._problem = self._load()

    def _load(self) -> tuple[list[WorkspaceRef], str | None]:
        try:
            if self._path.stat().st_size > MAX_RECENTS_FILE_BYTES:
                return [], f"{RECENTS_FILE}: larger than a recents file can be — ignored"
            # utf-8-sig for the same reason `services/layouts.py` uses it: this
            # is a small JSON file a curious user may well open in Notepad.
            raw = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return [], None  # nothing recorded yet is not a problem
        except OSError as err:
            return [], f"{RECENTS_FILE}: unreadable ({err.strerror or err})"
        try:
            document = RecentWorkspacesFile.model_validate(json.loads(raw))
        except (ValueError, TypeError) as err:
            return [], f"{RECENTS_FILE}: not a recents document ({err.__class__.__name__})"
        if document.version != RECENTS_VERSION:
            return [], f"{RECENTS_FILE}: written by another version of Workbench — ignored"
        return document.recents[:MAX_RECENT_WORKSPACES], None

    def _save(self) -> None:
        document = RecentWorkspacesFile(version=RECENTS_VERSION, recents=self._refs)
        data = document.model_dump_json().encode("utf-8")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as tmp:
                    tmp.write(data)
                os.replace(tmp_name, self._path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as err:
            # A history that could not be written is a history, not an outage.
            log.warning("workspaces.recents_unwritable", path=str(self._path), detail=str(err))


class WorkspaceService:
    """The current root, the history, and the one function that moves both."""

    def __init__(
        self,
        workspace: Workspace,
        bus: EventBus,
        *,
        sync_rootables: list[SyncRootable],
        async_rootables: list[AsyncRootable],
        explicit: bool,
        recents: RecentsStore | None = None,
    ) -> None:
        self._workspace = workspace
        self._bus = bus
        self._sync = sync_rootables
        self._async = async_rootables
        self._explicit = explicit
        self._recents = recents or RecentsStore()
        #: One switch at a time. See :meth:`switch` for why the whole sequence
        #: and not just the awaited half.
        self._switching = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._workspace.root

    def start(self) -> None:
        """Record the launch workspace, so the first switch has somewhere back."""
        self._recents.record(self._workspace.root)

    def state(self) -> WorkspaceState:
        root = self._workspace.root
        return WorkspaceState(
            root=str(root),
            name=_name_of(root),
            explicit=self._explicit,
            recents=self._recents.entries(),
            problem=self._recents.problem,
        )

    async def switch(self, path: str) -> WorkspaceState:
        """Re-root the whole server. Raises :class:`WorkspaceRefusedError` for a
        root that cannot be served — nothing is touched in that case.

        Switching to the root that is already current is a no-op that still
        succeeds: it is what a picker row for "where you already are" does, and
        tearing the watcher down to arrive where we started would be a visible
        flicker for nothing.

        **One switch at a time, and the lock spans the whole sequence.** Two
        windows on one server is a supported arrangement — that is what the
        ``workspace_changed`` frame is for — so two switches landing in the same
        tick is a thing users do, not a thing tests invent. Re-rooting is a
        sequence with an await in the middle of it, and unserialized that
        sequence interleaves: both jail writes land, then both watcher restarts
        do, and nothing makes those two orders agree. The results are a server
        whose jail and whose watch point at different projects, and a watch on a
        workspace nobody is in that no ``stop`` can reach any more, because the
        second restart overwrote the handle to the first. Both look like a
        working server from either window.

        Serialized rather than refused with a 409: the second window asked for
        something legitimate, and after the first switch finishes the second is
        re-validated against the root that actually won and applied on top of it.
        Last one wins, both windows are told by the bus, and no state exists in
        between. A second switch to the root the first one just arrived at
        collapses into the no-op above, which is the common case when two windows
        are driven from the same picker.
        """
        async with self._switching:
            # Inside the lock, both of them: validating and comparing against a
            # root another switch is in the middle of moving is how a "no-op"
            # concludes it has nothing to do and skips a re-root it owed.
            root = validate_root(Path(path))
            if root == self._workspace.root:
                self._recents.record(root)
                return self.state()

            previous = self._workspace.root
            # The jail first: from here on, every path from the wire is judged
            # against the new root, so nothing in flight can be answered half-way
            # between the two.
            self._workspace.set_root(root)
            for service in self._sync:
                service.set_workspace_root(root)
            # The watcher (and the pool) last, and awaited: a restart that
            # overlapped with the old watch would put two projects' paths on one
            # bus.
            for awaitable in self._async:
                await awaitable.set_workspace_root(root)

            self._explicit = True
            self._recents.record(root)
            log.info("workspace.switched", root=str(root), previous=str(previous))
            self._bus.publish(WorkspaceChangedEvent(root=str(root), name=_name_of(root)))
            return self.state()
