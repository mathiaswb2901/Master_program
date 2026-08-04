"""PTY sessions on Windows via pywinpty (ConPTY).

``winpty.PtyProcess`` does blocking reads, so reads are pushed onto worker
threads with ``asyncio.to_thread``. The manager tracks live sessions so the
app can terminate them all on shutdown.
"""

import asyncio
import itertools
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()

_READ_CHUNK = 4096
_SHELL = "powershell.exe -NoLogo"


class PtyLike(Protocol):
    """The slice of winpty.PtyProcess we use — lets tests substitute a fake."""

    def read(self, length: int) -> str: ...
    def write(self, data: str) -> int: ...
    def setwinsize(self, rows: int, cols: int) -> None: ...
    def isalive(self) -> bool: ...
    def terminate(self, force: bool = ...) -> bool: ...


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
        from winpty import PtyProcess  # Windows-only import, deferred

        proc: PtyProcess = PtyProcess.spawn(_SHELL, cwd=str(cwd), dimensions=(rows, cols))
        session = PtySession(next(self._ids), proc)
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
