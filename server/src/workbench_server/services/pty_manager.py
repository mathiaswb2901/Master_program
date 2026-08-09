"""PTY sessions, one backend per platform behind one protocol.

A PTY does blocking reads whatever the OS, so reads are pushed onto worker
threads with ``asyncio.to_thread``. The manager tracks live sessions so the
app can terminate them all on shutdown.

`PtyLike` is the seam (ARCHITECTURE.md, "platform code is quarantined"): the
shape of `winpty.PtyProcess`, which is what the rest of the server was already
written against. `_spawn_backend` picks the implementation by `sys.platform` —
pywinpty/ConPTY on Windows, `pty_posix.PosixPty` (stdlib `pty`) elsewhere — and
both imports are function-local, because neither module can be imported on the
other's platform. Nothing above this line knows which one it got.
"""

import asyncio
import itertools
import sys
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()

_READ_CHUNK = 4096
_WINDOWS_SHELL = "powershell.exe -NoLogo"


class PtyLike(Protocol):
    """The slice of winpty.PtyProcess we use — lets tests substitute a fake."""

    def read(self, length: int) -> str: ...
    def write(self, data: str) -> int: ...
    def setwinsize(self, rows: int, cols: int) -> None: ...
    def isalive(self) -> bool: ...
    def terminate(self, force: bool = ...) -> bool: ...


def _spawn_backend(cwd: Path, rows: int, cols: int) -> PtyLike:
    """The platform's PTY, started on the user's shell in `cwd`."""
    if sys.platform == "win32":
        from winpty import PtyProcess  # Windows-only import, deferred

        spawned: PtyLike = PtyProcess.spawn(_WINDOWS_SHELL, cwd=str(cwd), dimensions=(rows, cols))
        return spawned
    else:
        from workbench_server.services import pty_posix  # POSIX-only stdlib imports

        return pty_posix.spawn(cwd, rows, cols)


class PtySession:
    def __init__(self, session_id: int, proc: PtyLike) -> None:
        self.session_id = session_id
        self._proc = proc

    async def read(self) -> str | None:
        """Next chunk of output, or None when the process has exited."""
        try:
            data = await asyncio.to_thread(self._proc.read, _READ_CHUNK)
        except (EOFError, ConnectionAbortedError, OSError):
            return None
        return data if data else None

    def write(self, data: str) -> None:
        self._proc.write(data)

    def resize(self, rows: int, cols: int) -> None:
        self._proc.setwinsize(rows, cols)

    def alive(self) -> bool:
        return self._proc.isalive()

    def terminate(self) -> None:
        try:
            if self._proc.isalive():
                self._proc.terminate(force=True)
        except OSError:  # already gone
            log.debug("pty.terminate_race", session_id=self.session_id)


class PtyManager:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._sessions: dict[int, PtySession] = {}

    def spawn(self, cwd: Path, rows: int = 24, cols: int = 80) -> PtySession:
        session = PtySession(next(self._ids), _spawn_backend(cwd, rows, cols))
        self._sessions[session.session_id] = session
        log.info("pty.spawned", session_id=session.session_id, cwd=str(cwd))
        return session

    def release(self, session: PtySession) -> None:
        session.terminate()
        self._sessions.pop(session.session_id, None)
        log.info("pty.released", session_id=session.session_id)

    def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            self.release(session)
