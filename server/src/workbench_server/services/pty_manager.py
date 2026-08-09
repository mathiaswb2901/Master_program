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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()

_READ_CHUNK = 4096
_WINDOWS_SHELL = "powershell.exe -NoLogo"
#: Threads reserved for teardown, and why teardown gets its own pool at all.
#:
#: `release` blocks: force-terminating a child that is wedged in uninterruptible
#: kernel I/O (an `ssh` to a dead peer, a hung network mount) polls for up to
#: `pty_posix.REAP_TIMEOUT_S`, and `winpty.PtyProcess.terminate` sleeps too. That
#: may not happen on the event-loop thread — uvicorn serves every other socket
#: and request from it — and it may not happen on the loop's *default* executor
#: either: that is where every live terminal parks a thread inside
#: `PtySession.read`, so a release scheduled there would queue behind the very
#: sessions it is closing (a box with 8 cores gives that pool 12 workers).
#: Hence a private pool, wide enough that a windowful of wedged terminals costs
#: one `REAP_TIMEOUT_S` in total rather than one apiece.
_TEARDOWN_THREADS = 32


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
        # Threads are created on demand, so a manager nobody releases through
        # costs nothing. Deliberately never shut down: a WebSocket handler's
        # own `finally:` can still run after the lifespan's, and a closed pool
        # would turn that release into a RuntimeError and orphan the child.
        # The workers are idle and are joined at interpreter exit.
        self._teardown = ThreadPoolExecutor(_TEARDOWN_THREADS, thread_name_prefix="pty-teardown")

    def spawn(self, cwd: Path, rows: int = 24, cols: int = 80) -> PtySession:
        session = PtySession(next(self._ids), _spawn_backend(cwd, rows, cols))
        self._sessions[session.session_id] = session
        log.info("pty.spawned", session_id=session.session_id, cwd=str(cwd))
        return session

    def release(self, session: PtySession) -> None:
        """Terminate the child and forget the session. **Blocks** — see
        `_TEARDOWN_THREADS`; callers on the event loop want `release_async`."""
        session.terminate()
        self._sessions.pop(session.session_id, None)
        log.info("pty.released", session_id=session.session_id)

    async def release_async(self, session: PtySession) -> None:
        """`release`, off the event-loop thread."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._teardown, self.release, session)

    async def shutdown(self) -> None:
        """Release every live session — off the loop thread, and all at once.

        Concurrently rather than in a loop: serially, N wedged children cost N x
        `REAP_TIMEOUT_S` of dead server before the process can exit, and a user
        who left four terminals open on hung SSH sessions would feel every one
        of them. Gathered, the whole shutdown is bounded by the slowest child.
        """
        sessions = list(self._sessions.values())
        if not sessions:
            return
        await asyncio.gather(*(self.release_async(session) for session in sessions))
