"""Filesystem watcher: watchfiles -> FileChangedEvent on the bus.

Disk is the single source of truth; this is the mechanism that makes every
view (editors, file tree, agents) converge on it.
"""

import asyncio
import contextlib
from pathlib import Path
from typing import Literal

import structlog
from watchfiles import Change, awatch

from workbench_server.models.files import (
    MAX_TEXT_FILE_BYTES,
    FileChangedEvent,
    TreeInvalidatedEvent,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.ignore import CACHEDIR_TAG, IgnoreIndex, is_ignored_dir
from workbench_server.services.workspace import content_hash

log = structlog.get_logger()

_CHANGE_NAMES: dict[Change, Literal["added", "modified", "deleted"]] = {
    Change.added: "added",
    Change.modified: "modified",
    Change.deleted: "deleted",
}


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` behind a plain function.

    The watcher loop is async and the lint rule banning blocking pathlib calls
    inside one is right in general; a single ``stat`` on a path the OS has just
    named is the exception, and keeping it here says so once.
    """
    try:
        return path.is_dir()
    except OSError:  # vanished between the event and the question
        return False


def _resolved(path: Path) -> Path:
    """``Path.resolve`` behind a sync helper, for the same reason ``_is_dir`` is
    one: it is a blocking filesystem call and the lint rule that keeps those out
    of coroutines is right — `set_workspace_root` calls this before it awaits."""
    return path.resolve()


def _hash_of(path: Path) -> str | None:
    try:
        if path.is_file() and path.stat().st_size <= MAX_TEXT_FILE_BYTES:
            return content_hash(path.read_bytes())
    except OSError:  # deleted/locked between event and read
        return None
    return None


class Watcher:
    def __init__(self, root: Path, bus: EventBus) -> None:
        self._root = root.resolve()
        self._bus = bus
        self._ignore = IgnoreIndex(self._root)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _skip(self, path: Path, kind: str) -> bool:
        if self._ignore.ignored(path):
            return True
        # our own atomic-write temp files (".name.random.tmp") must never surface as events
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            return True
        # A directory is reported when it *appears* — the tree has a row for it,
        # and a folder created by `mkdir`, an agent or a build used to stay
        # invisible until something forced a full refetch (as did every file
        # created inside it, since the client had no row to hang them on).
        # `modified` on a directory is Windows narrating its children, which
        # arrive as their own events; suppressing it is what keeps a patched
        # tree quiet. A *deleted* path is no longer a directory to ask about
        # (`is_dir()` is False the moment it is gone), so it falls through here
        # and the row goes by path.
        if not _is_dir(path):
            return False
        if kind != "added":
            return True
        # `IgnoreIndex.ignored` tests a path's *ancestors* — right for a file,
        # one short for a directory that is itself the build cache.
        return is_ignored_dir(path)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="workspace-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def set_workspace_root(self, root: Path) -> None:
        """Watch a different tree: stop, re-root, start again.

        A watch cannot be re-pointed in place — ``awatch`` is an async generator
        bound to the directory it was opened on — so this is a real restart, and
        it is awaited so the old watch is *gone* before the new one publishes.
        Overlapping them would emit paths relative to two different roots on one
        bus, and a client patching its tree from those would be building a
        directory listing out of two projects.

        The stop event and the ignore index are rebuilt too. ``self._stop`` has
        been set and cannot be un-set (``asyncio.Event`` has ``clear()``, but a
        subscriber of the old generator could still be inside it), and
        ``IgnoreIndex`` caches decisions about paths under the old root.
        """
        resolved = _resolved(root)
        await self.stop()
        self._root = resolved
        self._ignore = IgnoreIndex(self._root)
        self._stop = asyncio.Event()
        self.start()

    async def _run(self) -> None:
        log.info("watcher.started", root=str(self._root))
        async for changes in awatch(
            self._root, stop_event=self._stop, debounce=200, step=50, recursive=True
        ):
            # Before the batch, not during it: `changes` is a set, so a build
            # that creates its directory and writes the tag inside one debounce
            # window could otherwise have its artifacts judged — and remembered
            # as visible — before the tag in the same batch was ever noticed.
            if any(Path(raw_path).name == CACHEDIR_TAG for _, raw_path in changes):
                self._ignore.invalidate()
                # And say so on the wire. A tag appearing takes a whole subtree
                # out of the tree's answer, and the events that would have
                # described it are exactly the ones now being suppressed — so a
                # client patching its own tree from these events has no other
                # way to learn. Rare enough to be free, and it is what lets the
                # tree be incremental without ever drifting from a fresh listing.
                self._bus.publish(TreeInvalidatedEvent())
            for change, raw_path in changes:
                path = Path(raw_path)
                kind = _CHANGE_NAMES.get(change)
                if kind is None:
                    continue
                if self._skip(path, kind):
                    continue
                is_dir = kind == "added" and _is_dir(path)
                event = FileChangedEvent(
                    path=path.relative_to(self._root).as_posix(),
                    change=kind,
                    hash=None if kind == "deleted" or is_dir else _hash_of(path),
                    is_dir=is_dir,
                )
                self._bus.publish(event)
        log.info("watcher.stopped")
