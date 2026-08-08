"""The PTY seam: the factory's platform choice, and the POSIX backend's logic.

`test_terminal.py` is the Windows proof and is deliberately untouched by this
change — it drives the real pywinpty path end to end through the router.

What is *here* is the part that cannot be proven that way. The POSIX backend
forks a real process on a real pty, which this Windows box cannot do at all, so
its kernel calls go through `PosixSyscalls` and are driven here by `FakePosix`
— the same stand-in shape `test_office_host_shell.py` uses for win32. That
proves the logic (UTF-8 reassembly across reads, the two spellings of EOF,
reaping, terminate) on any OS; it does **not** prove `pty.fork()` itself. That
is what the ubuntu leg of the 3-OS matrix (M7 §C2) is for, where these same
`test_terminal.py` integration tests run against the POSIX backend for real.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from workbench_server.services import pty_posix
from workbench_server.services.pty_manager import PtyLike, PtyManager, _spawn_backend
from workbench_server.services.pty_posix import PosixPty, PosixSyscalls, _StdlibSyscalls


class FakePosix:
    """A `PosixSyscalls` over an in-memory script — no fork, no fd, no OS."""

    def __init__(self, reads: list[bytes | OSError] | None = None, pid: int = 4242) -> None:
        self._reads = list(reads or [])
        self.pid = pid
        self.written: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.signals: list[bool] = []
        self.closed = 0
        self.blocking_reaps = 0
        self.exit_status: int | None = None
        self.write_limit: int | None = None
        self.forked: tuple[list[str], Path, dict[str, str]] | None = None

    # -- the protocol ---------------------------------------------------------
    def fork_pty(self, argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, int]:
        self.forked = (argv, cwd, env)
        return self.pid, 7

    def read(self, fd: int, length: int) -> bytes:
        if not self._reads:
            return b""
        item = self._reads.pop(0)
        if isinstance(item, OSError):
            raise item
        return item[:length]

    def write(self, fd: int, data: bytes) -> int:
        chunk = data if self.write_limit is None else data[: self.write_limit]
        self.written.append(chunk)
        return len(chunk)

    def set_winsize(self, fd: int, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def close(self, fd: int) -> None:
        self.closed += 1

    def kill(self, pid: int, *, force: bool) -> None:
        self.signals.append(force)
        self.exit_status = 9 if force else None

    def reap(self, pid: int, *, block: bool) -> int | None:
        if block:
            self.blocking_reaps += 1
            self.exit_status = 9 if self.exit_status is None else self.exit_status
        return self.exit_status


def posix_pty(fake: FakePosix) -> PosixPty:
    return PosixPty(fake.pid, 7, fake)


class TestTheSeam:
    def test_both_backends_satisfy_the_protocol(self) -> None:
        """Static conformance is mypy's job; this pins it at runtime too."""
        backend: PtyLike = posix_pty(FakePosix())
        assert all(
            callable(getattr(backend, name))
            for name in ("read", "write", "setwinsize", "isalive", "terminate")
        )
        syscalls: PosixSyscalls = pty_posix._StdlibSyscalls()
        assert callable(syscalls.fork_pty)

    def test_the_factory_picks_pywinpty_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spawned: dict[str, Any] = {}

        def fake_spawn(cmd: str, cwd: str, dimensions: tuple[int, int]) -> object:
            spawned.update(cmd=cmd, cwd=cwd, dimensions=dimensions)
            return object()

        monkeypatch.setitem(
            sys.modules, "winpty", SimpleNamespace(PtyProcess=SimpleNamespace(spawn=fake_spawn))
        )
        monkeypatch.setattr(sys, "platform", "win32")
        _spawn_backend(Path.cwd(), 30, 120)
        assert spawned["cmd"].startswith("powershell")
        assert spawned["dimensions"] == (30, 120)

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_the_factory_picks_the_posix_backend_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        fake = FakePosix()
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(pty_posix, "spawn", lambda cwd, rows, cols: posix_pty(fake))
        backend = _spawn_backend(Path.cwd(), 24, 80)
        assert isinstance(backend, PosixPty)

    def test_the_manager_tracks_and_releases_whatever_the_factory_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manager is platform-blind — the seam's whole point."""
        fake = FakePosix()
        monkeypatch.setattr(
            "workbench_server.services.pty_manager._spawn_backend",
            lambda cwd, rows, cols: posix_pty(fake),
        )
        manager = PtyManager()
        session = manager.spawn(Path.cwd())
        assert manager._sessions == {session.session_id: session}
        manager.shutdown()
        assert manager._sessions == {}
        assert fake.signals == [True]  # force-killed, as on Windows


class TestPosixSpawn:
    def test_spawn_uses_the_login_shell_and_declares_a_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakePosix()
        monkeypatch.setenv("SHELL", "/bin/zsh")
        proc = pty_posix.spawn(Path.cwd(), rows=40, cols=100, syscalls=fake)
        assert proc.pid == fake.pid
        assert fake.forked is not None
        argv, cwd, env = fake.forked
        assert argv == ["/bin/zsh"]
        assert cwd == Path.cwd()
        assert env["TERM"] == pty_posix.TERM  # unset TERM degrades every curses app
        assert fake.sizes == [(40, 100)]  # the size is applied before the first read

    def test_spawn_falls_back_when_no_shell_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakePosix()
        monkeypatch.delenv("SHELL", raising=False)
        pty_posix.spawn(Path.cwd(), syscalls=fake)
        assert fake.forked is not None
        assert fake.forked[0] == [pty_posix.FALLBACK_SHELL]


class TestPosixReads:
    def test_a_multibyte_character_split_across_reads_is_not_end_of_stream(self) -> None:
        """The bug this guards: `""` means EOF to `PtySession`, and a torn
        UTF-8 sequence decodes to nothing. Returning it would close the
        terminal mid-stream at a 4 KB boundary."""
        head, tail = "é".encode()[:1], "é".encode()[1:]
        proc = posix_pty(FakePosix([head, tail]))
        assert proc.read(4096) == "é"

    def test_eof_by_empty_read_ends_the_stream(self) -> None:
        proc = posix_pty(FakePosix([b"hi", b""]))
        assert proc.read(4096) == "hi"
        assert proc.read(4096) == ""

    def test_eof_by_eio_ends_the_stream(self) -> None:
        """Linux raises EIO where macOS returns b"" — same event."""
        fake = FakePosix([b"hi", OSError(5, "Input/output error")])
        proc = posix_pty(fake)
        assert proc.read(4096) == "hi"
        assert proc.read(4096) == ""
        assert fake.closed == 1
        assert proc.read(4096) == ""  # and stays ended, without touching the fd
        assert fake.closed == 1

    def test_invalid_bytes_are_replaced_rather_than_raised(self) -> None:
        proc = posix_pty(FakePosix([b"\xff\xfe"]))
        assert proc.read(4096) == "��"


class RealFdSyscalls(_StdlibSyscalls):
    """The real `os.read`/`os.write`/`os.close`, over an ordinary pipe.

    A pipe is not a pty — there is no fork, no ioctl and no child to reap, so
    those three are stubbed — but the byte path *is* the real one: real fds,
    real kernel reads, a real EOF when the writer closes. That is the part of
    `_StdlibSyscalls` that is identical on every OS, so it can be proven here
    instead of only on the ubuntu runner.
    """

    def fork_pty(self, argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, int]:
        raise NotImplementedError

    def set_winsize(self, fd: int, rows: int, cols: int) -> None: ...

    def kill(self, pid: int, *, force: bool) -> None: ...

    def reap(self, pid: int, *, block: bool) -> int | None:
        return None


class TestPosixOverRealFds:
    def test_real_reads_reassemble_and_end_at_a_real_eof(self) -> None:
        read_fd, write_fd = os.pipe()
        payload = "naïve ✓\n".encode()
        proc = PosixPty(os.getpid(), read_fd, RealFdSyscalls())
        try:
            os.write(write_fd, payload[:4])
            assert proc.read(4096) == payload[:4].decode()
            os.write(write_fd, payload[4:])
            rest = proc.read(4096)
            os.close(write_fd)
            write_fd = -1
            assert (payload[:4].decode() + rest) == payload.decode()
            assert proc.read(4096) == ""  # EOF, and the fd is closed with it
        finally:
            if write_fd != -1:
                os.close(write_fd)

    def test_a_real_write_reaches_the_fd(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            assert PosixPty(os.getpid(), write_fd, RealFdSyscalls()).write("ls\r") == 3
            assert os.read(read_fd, 64) == b"ls\r"
        finally:
            os.close(read_fd)
            os.close(write_fd)


class TestPosixWrites:
    def test_a_short_write_is_finished(self) -> None:
        fake = FakePosix()
        fake.write_limit = 2
        proc = posix_pty(fake)
        assert proc.write("abcde") == 5
        assert b"".join(fake.written) == b"abcde"

    def test_text_goes_out_as_utf8(self) -> None:
        fake = FakePosix()
        posix_pty(fake).write("é\r")
        assert b"".join(fake.written) == "é\r".encode()

    def test_resize_reaches_the_ioctl(self) -> None:
        fake = FakePosix()
        posix_pty(fake).setwinsize(30, 120)
        assert fake.sizes == [(30, 120)]


class TestPosixLifecycle:
    def test_a_running_child_is_alive_and_a_reaped_one_is_not(self) -> None:
        fake = FakePosix()
        proc = posix_pty(fake)
        assert proc.isalive()
        fake.exit_status = 0
        assert not proc.isalive()
        assert proc.exit_status == 0

    def test_terminate_kills_reaps_and_closes(self) -> None:
        fake = FakePosix()
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.signals == [True]
        assert fake.blocking_reaps == 1  # SIGKILL cannot be caught: waiting is safe
        assert fake.closed == 1
        assert proc.exit_status == 9

    def test_a_gentle_terminate_does_not_block_on_the_child(self) -> None:
        fake = FakePosix()
        proc = posix_pty(fake)
        proc.terminate()
        assert fake.signals == [False]
        assert fake.blocking_reaps == 0  # SIGHUP can be ignored — never wait on it

    def test_terminating_an_exited_child_signals_nothing(self) -> None:
        fake = FakePosix()
        fake.exit_status = 0
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.signals == []
        assert fake.closed == 1

    def test_a_terminate_race_is_survived(self) -> None:
        class Racing(FakePosix):
            def kill(self, pid: int, *, force: bool) -> None:
                raise ProcessLookupError(3, "No such process")

        fake = Racing()
        assert posix_pty(fake).terminate(force=True)
        assert fake.closed == 1
