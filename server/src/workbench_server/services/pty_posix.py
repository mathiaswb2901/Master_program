"""PTY sessions on POSIX via the stdlib `pty` module.

The Windows path (`pty_manager._spawn_backend`) is pywinpty and stays exactly as
it shipped. This module is its POSIX twin: same `PtyLike` surface, same blocking
`read(length) -> str` contract the coalescing pump in `terminal_stream` drives
from a worker thread, so nothing above the seam changes shape.

`pty.fork()` is what makes it a *terminal* rather than a pipe — it forks, calls
`setsid()` and hands the child a controlling tty — which is why the interactive
shell behaves like one (job control, `isatty()`, line editing) and why xterm.js
gets the escape sequences it expects. `TERM` is set for the child for the same
reason: an unset `TERM` makes readline and every curses program degrade.

**Two things here are logic, not syscalls, and both are load-bearing:**

* **A read never returns `""` unless the stream really ended.** `PtySession`
  reads `""` as end-of-stream and closes the WebSocket, so a UTF-8 character
  split across two `os.read` calls — routine at 4 KB boundaries — must not
  surface as an empty decode. `read` keeps reading until the incremental decoder
  produces at least one character or the fd hits EOF.
* **EOF has two spellings.** Linux raises `OSError(EIO)` when the last slave
  handle closes; macOS returns `b""`. Both mean the same thing here.

* **No wait on a child is ever unbounded.** `PtySession.terminate()` runs on the
  asyncio event loop thread (`routers/terminal.py` calls it from a `finally:`),
  and uvicorn's loop is single-threaded, so one blocking `waitpid` freezes every
  other websocket and request on the server. SIGKILL is uncatchable, but a child
  wedged in uninterruptible kernel I/O (a hung network mount, an `ssh` waiting on
  a dead peer) does not die until that I/O returns. So every reap here is
  `WNOHANG`, and the force path polls with backoff up to `REAP_TIMEOUT_S` and
  then gives up — the Windows path cannot hang the loop this way either
  (`winpty.PtyProcess.terminate` is signals plus bounded sleeps), and this keeps
  the two backends honest about the same worst case.

* **The master fd has exactly one owner, and it is claimed under a lock.** This
  class is driven from *two* threads: `read` blocks in a worker thread
  (`PtySession.read` -> `asyncio.to_thread`) while `terminate`, `write` and
  `setwinsize` run on the event loop thread. Both close paths converge on
  `_close_fd` — the reader when the fd hits EOF/EIO, the loop when the session is
  released — and on POSIX an fd number freed by the first `close` is handed
  straight back out by the *next* `open` anywhere in the process (the kernel
  gives out the lowest free descriptor). A second `close` of that number, or any
  syscall still holding it, therefore does not fail: it lands on whatever the fd
  has since become — another terminal's master, a WebSocket, the workspace lock.
  Cross-session corruption, not a crash, and invisible where it happens.

  So the number is never touched by anyone who has not claimed it:

  - `_close_fd` **swaps `_fd` to -1 under `_fd_lock`** and only the thread that
    took the real number closes it; every later caller claims -1 and is a no-op.
    (Same idiom as the pool lock in `services/worktrees.py`, which needs no lock
    because only the loop thread touches it.)
  - Every syscall borrows the fd through `_borrow_fd`, which counts users while
    they are inside the call. A close that arrives with a borrower in flight
    publishes -1 (so no *new* borrower can get the number) and hands the actual
    `close` to the last one out. That is what keeps the reader from being blocked
    in `os.read` on a number that has already been recycled onto another session.

  The failure mode of the deferral is one fd held open until the process exits,
  if a reader never wakes at all — which is the safe direction, because an fd
  that is still open cannot be recycled onto anything.

Every kernel call goes through `PosixSyscalls`, which is what lets the whole
class be exercised on the Windows box this is developed on (the stdlib `pty`,
`fcntl` and `termios` modules do not exist there) — the same stand-in shape
`test_office_host_shell.py` uses for win32. The real implementation is thin
enough that a fake proves the interesting half; the *actual* fork is proven by
the terminal suite running on an ubuntu runner (the 3-OS matrix, M7 §C2).
"""

import codecs
import os
import struct
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()

#: Fallback when the environment names no login shell.
FALLBACK_SHELL = "/bin/sh"
#: What the child is told it is talking to. xterm.js is an xterm.
TERM = "xterm-256color"
#: Longest a force-terminate will wait for the SIGKILLed child to be reapable.
#: A healthy child is gone in well under a millisecond; this ceiling only binds
#: when the child is stuck in uninterruptible kernel I/O, and it is what keeps
#: that case from freezing the event loop for the whole server.
REAP_TIMEOUT_S = 1.0
#: Poll backoff bounds for that wait: start tight so the common case costs one
#: syscall, widen so a doomed child does not spin a core.
REAP_POLL_MIN_S = 0.001
REAP_POLL_MAX_S = 0.05

#: Children that outlived `REAP_TIMEOUT_S`. Nothing else will ever wait on them
#: — their session is gone — so they would stay zombies for the life of the
#: server. Swept (non-blocking) whenever a new terminal is spawned.
_UNREAPED: list[tuple[int, "PosixSyscalls"]] = []


class PosixSyscalls(Protocol):
    """Every kernel call the POSIX backend makes, in one substitutable surface."""

    def fork_pty(self, argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, int]:
        """Fork a child with a new controlling tty; returns `(pid, master_fd)`."""

    def read(self, fd: int, length: int) -> bytes: ...
    def write(self, fd: int, data: bytes) -> int: ...
    def set_winsize(self, fd: int, rows: int, cols: int) -> None: ...
    def close(self, fd: int) -> None: ...
    def kill(self, pid: int, *, force: bool) -> None: ...

    def reap(self, pid: int) -> int | None:
        """Exit status, or None while the child is still running.

        Never blocks: see the module docstring on why waiting on the event loop
        thread is not an option here.
        """

    def sleep(self, seconds: float) -> None:
        """Pause between reap polls."""


class _StdlibSyscalls:
    """The real thing.

    Imports *and* calls are guarded by `sys.platform`: `pty`, `fcntl` and
    `termios` do not exist on Windows, and neither do `os.fork`, `os.WNOHANG` or
    `signal.SIGKILL`. The guard is what lets `mypy --strict` check this file on
    both platforms — on Windows the POSIX branches are skipped as unreachable,
    on Linux/macOS (the C2 matrix legs) they are the ones that get checked.
    """

    def fork_pty(self, argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, int]:
        if sys.platform == "win32":
            raise RuntimeError("the POSIX PTY backend is not available on Windows")
        else:
            import pty

            pid, fd = pty.fork()
            if pid == 0:  # pragma: no cover - the child never returns to the tests
                # `finally` runs only if the exec *failed* — on success this
                # process image is gone. 127 is the shell's own "not found".
                try:
                    os.chdir(cwd)
                    os.execvpe(argv[0], argv, env)  # noqa: S606 - a shell IS the payload
                finally:
                    os._exit(127)
            return pid, fd

    def read(self, fd: int, length: int) -> bytes:
        return os.read(fd, length)

    def write(self, fd: int, data: bytes) -> int:
        return os.write(fd, data)

    def set_winsize(self, fd: int, rows: int, cols: int) -> None:
        if sys.platform == "win32":
            raise RuntimeError("the POSIX PTY backend is not available on Windows")
        else:
            import fcntl
            import termios

            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self, fd: int) -> None:
        os.close(fd)

    def kill(self, pid: int, *, force: bool) -> None:
        if sys.platform == "win32":
            raise RuntimeError("the POSIX PTY backend is not available on Windows")
        else:
            import signal

            os.kill(pid, signal.SIGKILL if force else signal.SIGHUP)

    def reap(self, pid: int) -> int | None:
        if sys.platform == "win32":
            raise RuntimeError("the POSIX PTY backend is not available on Windows")
        else:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:  # already reaped
                return 0
            return None if done == 0 else status

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class PosixPty:
    """A forked shell on a pty master fd, shaped like `winpty.PtyProcess`."""

    def __init__(self, pid: int, fd: int, syscalls: PosixSyscalls) -> None:
        self.pid = pid
        self._sys = syscalls
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exit_status: int | None = None
        #: Guards the fd's *lifetime* — the three fields below are read and
        #: written from the reader thread and the event loop thread both. It is
        #: never held across a syscall on the fd itself.
        self._fd_lock = threading.Lock()
        #: The master fd, or -1 once a close has claimed it. -1 is the published
        #: "there is no fd" and is what every borrower checks; it is deliberately
        #: not a valid descriptor, so a caller that ignores the check gets EBADF
        #: rather than somebody else's session.
        self._fd = fd
        #: How many threads are inside a syscall on `_fd` right now.
        self._fd_users = 0
        #: An fd whose close is waiting for the last borrower to come out, else -1.
        self._pending_close = -1

    @property
    def exit_status(self) -> int | None:
        """The child's wait status once reaped, else None."""
        return self._exit_status

    @contextmanager
    def _borrow_fd(self) -> Iterator[int]:
        """Hold the master fd open for the duration of one syscall.

        Yields the descriptor, or -1 once it has been released — which every
        caller treats as "this terminal is over". While a borrow is out the
        number cannot be closed, so it cannot be recycled onto another session
        underneath a call that is already in flight.
        """
        with self._fd_lock:
            fd = self._fd
            if fd == -1:
                borrowed = False
            else:
                self._fd_users += 1
                borrowed = True
        if not borrowed:
            yield -1
            return
        try:
            yield fd
        finally:
            self._release_borrow()

    def _release_borrow(self) -> None:
        """Give the fd back, and close it if this was the last user of a doomed one."""
        with self._fd_lock:
            self._fd_users -= 1
            if self._fd_users or self._pending_close == -1:
                return
            fd, self._pending_close = self._pending_close, -1
        self._close_now(fd)

    def read(self, length: int) -> str:
        """Block for the next chunk of output; `""` only at end of stream."""
        while True:
            with self._borrow_fd() as fd:
                if fd == -1:  # terminated from the loop thread while we were away
                    return ""
                try:
                    raw = self._sys.read(fd, length)
                except OSError:  # EIO on Linux once the slave side is gone
                    raw = b""
            if not raw:
                self._close_fd()
                return self._decoder.decode(b"", final=True)
            text = self._decoder.decode(raw)
            if text:
                return text
            # Decoded to nothing: a multibyte character straddles this read and
            # the next. Returning "" here would be read as EOF — keep reading.

    def write(self, data: str) -> int:
        """Send keystrokes; a write to an ended terminal is dropped, not an error.

        The fd is borrowed for the whole short-write loop: a partial write must
        finish on the descriptor it started on, not on whatever the number became
        halfway through.
        """
        payload = data.encode("utf-8")
        sent = 0
        with self._borrow_fd() as fd:
            if fd == -1:
                log.debug("pty.posix_write_after_close", pid=self.pid)
                return len(data)
            while sent < len(payload):
                written = self._sys.write(fd, payload[sent:])
                if written <= 0:  # pragma: no cover - os.write raises instead
                    break
                sent += written
        return len(data)

    def setwinsize(self, rows: int, cols: int) -> None:
        with self._borrow_fd() as fd:
            if fd == -1:
                return
            try:
                self._sys.set_winsize(fd, rows, cols)
            except OSError:
                log.debug("pty.posix_resize_race", pid=self.pid)

    def isalive(self) -> bool:
        if self._exit_status is not None:
            return False
        status = self._sys.reap(self.pid)
        if status is None:
            return True
        self._exit_status = status
        return False

    def terminate(self, force: bool = False) -> bool:
        """Signal the child and reap it; `force` is SIGKILL, else SIGHUP."""
        if not self.isalive():
            self._close_fd()
            return True
        try:
            self._sys.kill(self.pid, force=force)
        except OSError:  # exited between the check and the signal
            # It is gone and nothing will signal it again, so it has to be
            # waited on *here* — the manager has already dropped this session,
            # so a skipped reap is a zombie for the life of the server.
            log.debug("pty.posix_terminate_race", pid=self.pid)
            status = self._sys.reap(self.pid)
        else:
            # SIGKILL is uncatchable, so the child is doomed and worth waiting
            # for — but only for `REAP_TIMEOUT_S`, and never with a blocking
            # waitpid. A caught SIGHUP could be ignored: never wait on that one.
            status = self._reap_within_timeout() if force else self._sys.reap(self.pid)
        if status is not None:
            self._exit_status = status
        self._close_fd()
        return True

    def _reap_within_timeout(self) -> int | None:
        """Poll `WNOHANG` with backoff; hand the pid to the sweep if it outlasts us."""
        waited = 0.0
        delay = REAP_POLL_MIN_S
        while True:
            status = self._sys.reap(self.pid)
            if status is not None:
                return status
            if waited >= REAP_TIMEOUT_S:
                log.warning("pty.posix_reap_timeout", pid=self.pid, waited=waited)
                _UNREAPED.append((self.pid, self._sys))
                return None
            self._sys.sleep(delay)
            waited += delay
            delay = min(delay * 2, REAP_POLL_MAX_S)

    def _close_fd(self) -> None:
        """Release the master fd, exactly once, from whichever thread gets here.

        Both callers race for real: the reader thread arrives via EOF/EIO, the
        event loop thread via `terminate`. The swap under the lock is the claim —
        one thread leaves with the descriptor and every other leaves with -1 — so
        the number is closed once and cannot be closed again after the OS has
        handed it to somebody else.
        """
        with self._fd_lock:
            fd, self._fd = self._fd, -1
            if fd == -1:
                return  # another thread already claimed it
            # Somebody inside a syscall on this number would have it recycled
            # under them by a close now; the last one out does it instead.
            users = self._fd_users
            if users:
                self._pending_close = fd
        if users:
            log.debug("pty.posix_close_deferred", pid=self.pid, users=users)
            return
        self._close_now(fd)

    def _close_now(self, fd: int) -> None:
        """The actual syscall — never under `_fd_lock`, and never twice on one fd."""
        try:
            self._sys.close(fd)
        except OSError:  # pragma: no cover - the OS refused a descriptor we owned
            log.debug("pty.posix_close_race", pid=self.pid)


def sweep_unreaped() -> int:
    """Wait (non-blocking) on children a force-terminate could not reap in time.

    Returns how many are still outstanding. Called on every spawn: a terminal
    the user opens later is the only recurring event with the right cadence,
    and the list is empty in every normal run.
    """
    for entry in list(_UNREAPED):
        pid, calls = entry
        try:
            status = calls.reap(pid)
        except OSError:  # pragma: no cover - nothing left to wait on
            status = 0
        if status is not None:
            _UNREAPED.remove(entry)
            log.info("pty.posix_reaped_late", pid=pid, status=status)
    return len(_UNREAPED)


def spawn(
    cwd: Path, rows: int = 24, cols: int = 80, syscalls: PosixSyscalls | None = None
) -> PosixPty:
    """Start the user's login shell on a new pty, rooted at `cwd`."""
    sweep_unreaped()
    calls = syscalls if syscalls is not None else _StdlibSyscalls()
    shell = os.environ.get("SHELL") or FALLBACK_SHELL
    env = dict(os.environ, TERM=TERM)
    pid, fd = calls.fork_pty([shell], cwd, env)
    proc = PosixPty(pid, fd, calls)
    proc.setwinsize(rows, cols)
    log.info("pty.posix_spawned", pid=pid, shell=shell, platform=sys.platform)
    return proc
