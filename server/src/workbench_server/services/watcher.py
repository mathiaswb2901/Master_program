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

from workbench_server.models.files import MAX_TEXT_FILE_BYTES, FileChangedEvent
from workbench_server.services.event_bus import EventBus
from workbench_server.services.ignore import CACHEDIR_TAG, IgnoreIndex
from workbench_server.services.workspace import content_hash

log = structlog.get_logger()

_CHANGE_NAMES: dict[Change, Literal["added", "modified", "deleted"]] = {
    Change.added: "added",
    Change.modified: "modified",
    Change.deleted: "deleted",
}


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

    def _skip(self, path: Path) -> bool:
        if self._ignore.ignored(path):
            return True
        # our own atomic-write temp files (".name.random.tmp") must never surface as events
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            return True
        return path.is_dir()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="workspace-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

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
            for change, raw_path in changes:
                path = Path(raw_path)
                if self._skip(path):
                    continue
                kind = _CHANGE_NAMES.get(change)
                if kind is None:
                    continue
                event = FileChangedEvent(
                    path=path.relative_to(self._root).as_posix(),
                    change=kind,
                    hash=None if kind == "deleted" else _hash_of(path),
                )
                self._bus.publish(event)
        log.info("watcher.stopped")
