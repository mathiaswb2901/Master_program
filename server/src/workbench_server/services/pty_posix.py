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
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()

#: Fallback when the environment names no login shell.
FALLBACK_SHELL = "/bin/sh"
#: What the child is told it is talking to. xterm.js is an xterm.
TERM = "xterm-256color"


class PosixSyscalls(Protocol):
    """Every kernel call the POSIX backend makes, in one substitutable surface."""

    def fork_pty(self, argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, int]:
        """Fork a child with a new controlling tty; returns `(pid, master_fd)`."""

    def read(self, fd: int, length: int) -> bytes: ...
    def write(self, fd: int, data: bytes) -> int: ...
    def set_winsize(self, fd: int, rows: int, cols: int) -> None: ...
    def close(self, fd: int) -> None: ...
    def kill(self, pid: int, *, force: bool) -> None: ...

    def reap(self, pid: int, *, block: bool) -> int | None:
        """Exit status, or None while the child is still running."""


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

    def reap(self, pid: int, *, block: bool) -> int | None:
        if sys.platform == "win32":
            raise RuntimeError("the POSIX PTY backend is not available on Windows")
        else:
            try:
                done, status = os.waitpid(pid, 0 if block else os.WNOHANG)
            except ChildProcessError:  # already reaped
                return 0
            return None if done == 0 else status


class PosixPty:
    """A forked shell on a pty master fd, shaped like `winpty.PtyProcess`."""

    def __init__(self, pid: int, fd: int, syscalls: PosixSyscalls) -> None:
        self.pid = pid
        self._fd = fd
        self._sys = syscalls
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exit_status: int | None = None
        self._fd_open = True

    @property
    def exit_status(self) -> int | None:
        """The child's wait status once reaped, else None."""
        return self._exit_status

    def read(self, length: int) -> str:
        """Block for the next chunk of output; `""` only at end of stream."""
        while self._fd_open:
            try:
                raw = self._sys.read(self._fd, length)
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
        return ""

    def write(self, data: str) -> int:
        payload = data.encode("utf-8")
        sent = 0
        while sent < len(payload):
            written = self._sys.write(self._fd, payload[sent:])
            if written <= 0:  # pragma: no cover - os.write raises instead
                break
            sent += written
        return len(data)

    def setwinsize(self, rows: int, cols: int) -> None:
        if not self._fd_open:
            return
        try:
            self._sys.set_winsize(self._fd, rows, cols)
        except OSError:
            log.debug("pty.posix_resize_race", pid=self.pid)

    def isalive(self) -> bool:
        if self._exit_status is not None:
            return False
        status = self._sys.reap(self.pid, block=False)
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
            log.debug("pty.posix_terminate_race", pid=self.pid)
        else:
            # SIGKILL cannot be caught, so this wait is bounded by the kernel.
            # A caught SIGHUP could be ignored, so that one is not waited on.
            status = self._sys.reap(self.pid, block=force)
            if status is not None:
                self._exit_status = status
        self._close_fd()
        return True

    def _close_fd(self) -> None:
        if not self._fd_open:
            return
        self._fd_open = False
        try:
            self._sys.close(self._fd)
        except OSError:  # pragma: no cover - already closed
            log.debug("pty.posix_close_race", pid=self.pid)


def spawn(
    cwd: Path, rows: int = 24, cols: int = 80, syscalls: PosixSyscalls | None = None
) -> PosixPty:
    """Start the user's login shell on a new pty, rooted at `cwd`."""
    calls = syscalls if syscalls is not None else _StdlibSyscalls()
    shell = os.environ.get("SHELL") or FALLBACK_SHELL
    env = dict(os.environ, TERM=TERM)
    pid, fd = calls.fork_pty([shell], cwd, env)
    proc = PosixPty(pid, fd, calls)
    proc.setwinsize(rows, cols)
    log.info("pty.posix_spawned", pid=pid, shell=shell, platform=sys.platform)
    return proc
