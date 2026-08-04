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
from workbench_server.services.workspace import IGNORED_DIRS, content_hash

log = structlog.get_logger()

_CHANGE_NAMES: dict[Change, Literal["added", "modified", "deleted"]] = {
    Change.added: "added",
    Change.modified: "modified",
    Change.deleted: "deleted",
}


def _ignored(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    if any(part in IGNORED_DIRS for part in parts):
        return True
    # our own atomic-write temp files (".name.random.tmp") must never surface as events
    if path.name.startswith(".") and path.name.endswith(".tmp"):
        return True
    return path.is_dir()


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
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

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
            for change, raw_path in changes:
                path = Path(raw_path)
                if _ignored(self._root, path):
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
