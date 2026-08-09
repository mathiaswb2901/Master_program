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

import asyncio
import contextlib
import errno
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Any, cast

import pytest

from workbench_server.services import pty_posix
from workbench_server.services.pty_manager import PtyLike, PtyManager, PtySession, _spawn_backend
from workbench_server.services.pty_posix import PosixPty, PosixSyscalls, _StdlibSyscalls


class FakePosix:
    """A `PosixSyscalls` over an in-memory script — no fork, no fd, no OS."""

    #: What `getpgid(0)` answers: the server's own group, which nothing here may
    #: ever signal. Deliberately unlike any pid a test uses.
    OUR_GROUP = 111

    def __init__(self, reads: list[bytes | OSError] | None = None, pid: int = 4242) -> None:
        self._reads = list(reads or [])
        self.pid = pid
        self.written: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.signals: list[bool] = []
        self.groups: list[tuple[int, bool]] = []
        self.closed = 0
        self.reaps = 0
        self.slept: list[float] = []
        #: Which thread each reap-poll sleep ran on. A blocking wait belongs on
        #: a worker; on the event-loop thread it is the whole server stalling.
        self.slept_on: list[int] = []
        self.exit_status: int | None = None
        self.write_limit: int | None = None
        self.forked: tuple[list[str], Path, dict[str, str]] | None = None
        #: The child's own group. `pty.fork()` calls setsid(), so a healthy child
        #: is its own session and group leader — hence pgid == pid.
        self.child_group: int | None = pid
        #: What the tty says is in the foreground. `None` is a kernel that
        #: refuses the ioctl; the default is "the shell itself", i.e. no job.
        self.foreground: int | None = pid

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

    def killpg(self, pgid: int, *, force: bool) -> None:
        self.groups.append((pgid, force))
        # Same effect on the child as the pid path: it is inside the group.
        self.exit_status = 9 if force else None

    def getpgid(self, pid: int) -> int:
        if pid == 0:
            return self.OUR_GROUP
        if self.child_group is None:
            raise ProcessLookupError(errno.ESRCH, "No such process")
        return self.child_group

    def foreground_pgid(self, fd: int) -> int:
        if self.foreground is None:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        return self.foreground

    def reap(self, pid: int) -> int | None:
        self.reaps += 1
        return self.exit_status

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.slept_on.append(threading.get_ident())


class WedgedChild(FakePosix):
    """SIGKILLed but stuck in uninterruptible I/O — never becomes reapable.

    The one case `REAP_TIMEOUT_S` exists for, and the only one in which a
    release blocks long enough for *where* it blocks to matter.
    """

    def reap(self, pid: int) -> int | None:
        self.reaps += 1
        return None


class InLockstep(WedgedChild):
    """A wedged child whose first reap-poll waits for its siblings' at a barrier.

    `met_the_others` is False when it was the only one there — which is exactly
    what a serial shutdown looks like from inside one child's reap.
    """

    def __init__(self, barrier: threading.Barrier, pid: int) -> None:
        super().__init__(pid=pid)
        self._barrier = barrier
        self.met_the_others: bool | None = None

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        if self.met_the_others is None:
            try:
                self._barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                self.met_the_others = False
            else:
                self.met_the_others = True


def posix_pty(fake: FakePosix) -> PosixPty:
    return PosixPty(fake.pid, 7, fake)


@pytest.fixture(autouse=True)
def _no_unreaped_leak_between_tests() -> Iterator[None]:
    pty_posix._UNREAPED.clear()
    yield
    pty_posix._UNREAPED.clear()


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

    async def test_the_manager_tracks_and_releases_whatever_the_factory_returned(
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
        await manager.shutdown()
        assert manager._sessions == {}
        assert fake.groups == [(fake.pid, True)]  # force-killed, as on Windows


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

    def killpg(self, pgid: int, *, force: bool) -> None: ...

    def getpgid(self, pid: int) -> int:
        """A group that is emphatically not the test runner's.

        These tests pass `os.getpid()` as the child, so the real `getpgid` would
        answer with *our* group — which `terminate` refuses to signal, sending
        every one of them down the fallback path instead of the one under test.
        """
        return 4242 if pid else 111

    def foreground_pgid(self, fd: int) -> int:
        raise OSError(errno.ENOTTY, "a pipe has no foreground process group")

    def reap(self, pid: int) -> int | None:
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
        assert fake.groups == [(fake.pid, True)]  # the group, not the bare pid
        assert fake.signals == []
        assert fake.slept == []  # a dead child is reapable on the first poll
        assert fake.closed == 1
        assert proc.exit_status == 9

    def test_a_gentle_terminate_does_not_wait_on_the_child(self) -> None:
        fake = FakePosix()
        proc = posix_pty(fake)
        proc.terminate()
        assert fake.groups == [(fake.pid, False)]
        assert fake.slept == []  # SIGHUP can be ignored — never wait on it

    def test_terminating_an_exited_child_signals_nothing(self) -> None:
        fake = FakePosix()
        fake.exit_status = 0
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.signals == []
        assert fake.groups == []
        assert fake.closed == 1

    def test_a_terminate_race_still_reaps_the_child(self) -> None:
        """The child exits between `isalive()` and the signal.

        Nothing will ever signal it again and the manager has already dropped
        the session, so if `terminate` skips the reap here the child stays a
        zombie for the life of the server. Both signalling paths have to fail
        for that to be the diagnosis: a group that is gone is exactly the case
        that falls back to the pid, and it is the pid's `ESRCH` that means
        "already exited" rather than "wrong target".
        """

        class Racing(FakePosix):
            def killpg(self, pgid: int, *, force: bool) -> None:
                raise ProcessLookupError(errno.ESRCH, "No such process")

            def kill(self, pid: int, *, force: bool) -> None:
                self.exit_status = 0  # it is gone; that is why the signal failed
                raise ProcessLookupError(errno.ESRCH, "No such process")

        fake = Racing()
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.reaps >= 2  # the isalive() probe, then the one that waits on it
        assert proc.exit_status == 0
        assert fake.closed == 1


class TestTerminateSignalsTheGroupNotThePid:
    """Who gets the signal — the half a fake can prove on any OS.

    `TestAJobDoesNotOutliveItsTerminal` (bottom of this file) proves the kernel
    end of it on the POSIX legs. These pin the decisions: which groups are
    chosen, which are refused, and what happens when the kernel says no.
    """

    def test_the_running_jobs_group_is_signalled_as_well_as_the_shells(self) -> None:
        """Job control puts the command in a group of its own.

        An interactive shell starts every job in a new process group, so the
        shell's own group holds nothing but the shell: kill it and the build the
        user was running is untouched. The group the *terminal* is pointed at is
        the one that has it.
        """
        fake = FakePosix()
        fake.foreground = 5555  # `make` and its children, in a group of their own
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        # The job first: the shell's death would otherwise be free to release
        # that pgid before we reach it.
        assert fake.groups == [(5555, True), (fake.pid, True)]
        assert fake.signals == []

    def test_one_group_is_signalled_once_when_the_shell_is_the_foreground(self) -> None:
        """At a prompt — or with job control off — both names are one group."""
        fake = FakePosix()
        fake.foreground = fake.pid
        proc = posix_pty(fake)
        proc.terminate(force=True)
        assert fake.groups == [(fake.pid, True)]

    def test_a_kernel_that_will_not_name_the_foreground_group_still_kills_the_shell(
        self,
    ) -> None:
        fake = FakePosix()
        fake.foreground = None  # the ioctl raised; nothing to fall back *to*
        proc = posix_pty(fake)
        proc.terminate(force=True)
        assert fake.groups == [(fake.pid, True)]

    def test_our_own_process_group_is_never_signalled(self) -> None:
        """The one mistake that would kill the server instead of the terminal.

        A child sharing our group means `pty.fork()` did not `setsid()` — the
        group SIGKILL would land on uvicorn, this process and every other
        terminal. Refuse the group, signal the pid, and say so in the log.
        """
        fake = FakePosix()
        fake.child_group = FakePosix.OUR_GROUP
        fake.foreground = FakePosix.OUR_GROUP
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.groups == []
        assert fake.signals == [True]  # the pid path, which can only hit the child

    def test_a_group_that_cannot_be_looked_up_falls_back_to_the_pid(self) -> None:
        fake = FakePosix()
        fake.child_group = None  # getpgid raised ESRCH
        fake.foreground = None
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.groups == []
        assert fake.signals == [True]

    @pytest.mark.parametrize("refusal", [errno.ESRCH, errno.EPERM])
    def test_a_refused_group_signal_falls_back_to_the_pid_and_never_raises(
        self, refusal: int
    ) -> None:
        """ESRCH: the group is already gone. EPERM: it is not ours to signal.

        Either way the child itself may still be alive, and `terminate` owes the
        caller a dead child — so the pid path runs. Neither error escapes:
        `routers/terminal.py` calls this from a `finally:`.
        """

        class Refusing(FakePosix):
            def killpg(self, pgid: int, *, force: bool) -> None:
                self.groups.append((pgid, force))
                raise OSError(refusal, os.strerror(refusal))

        fake = Refusing()
        fake.foreground = 5555
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.groups == [(5555, True), (fake.pid, True)]  # both were tried
        assert fake.signals == [True]  # and the child was still signalled
        assert fake.closed == 1

    def test_a_refused_job_group_does_not_cost_the_shell_its_group_kill(self) -> None:
        """Only the job's group is gone (it finished); the shell's is intact."""

        class JobGone(FakePosix):
            def killpg(self, pgid: int, *, force: bool) -> None:
                if pgid != self.pid:
                    raise ProcessLookupError(errno.ESRCH, "No such process")
                super().killpg(pgid, force=force)

        fake = JobGone()
        fake.foreground = 5555
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert fake.groups == [(fake.pid, True)]  # the shell's, delivered
        assert fake.signals == []  # so no pid fallback was needed

    def test_the_foreground_group_is_not_read_from_a_released_fd(self) -> None:
        """The ioctl needs the master, and the master has one owner (#110).

        A terminate that arrives after the reader hit EOF has no fd to ask, and
        asking anyway would put a `TIOCGPGRP` on whatever the number has since
        become. The shell's own group does not depend on the fd, so that half
        still runs.
        """
        fake = FakePosix([b""])
        fake.foreground = 5555
        proc = posix_pty(fake)
        assert proc.read(4096) == ""  # EOF: the reader closed the master
        assert fake.closed == 1
        proc.terminate(force=True)
        assert fake.groups == [(fake.pid, True)]

    def test_the_reap_ladder_is_unchanged_by_the_group_signal(self) -> None:
        """A group SIGKILL is still bounded, and still never blocks the loop."""

        class Wedged(FakePosix):
            def reap(self, pid: int) -> int | None:
                self.reaps += 1
                return None

        fake = Wedged()
        fake.foreground = 5555
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert sum(fake.slept) <= pty_posix.REAP_TIMEOUT_S + pty_posix.REAP_POLL_MAX_S
        assert [(fake.pid, fake)] == pty_posix._UNREAPED
        assert fake.closed == 1


#: What `sys.settrace` takes: a hook that returns the hook for the next event.
TraceFn = Callable[[FrameType, str, Any], "TraceFn | None"]


class Lockstep:
    """A deterministic two-thread scheduler over one function.

    The bug below lives in a window two bytecodes wide, between reading a flag
    and writing it. Measured on this machine: a stress loop of 3,000 trials with
    `sys.setswitchinterval(1e-6)`, both racers released from a barrier and again
    from a spin-wait, hit that window **zero** times — the GIL simply does not
    preempt there. A race that cannot be reproduced cannot be regression-tested,
    so the interleaving is *driven* rather than hoped for.

    `sys.settrace` gives a line-level hook, which is a scheduler: every party is
    held at the door until all of them are inside `target`, and from then on each
    line runs in turn. A party that does not reach its next line within
    `blocked_after` is inside a C-level acquire rather than running, so the turn
    moves on — which is exactly what keeps a *correctly locked* implementation
    from deadlocking the interleaver instead of passing.

    Consequences worth stating, because they are what makes this safe to keep:
    a looser interleaving (a loaded CI runner exercising the timeout more often)
    can only make the test weaker, never red — the assertions are invariants that
    hold under every schedule. And `entered` is asserted by every user, so a
    renamed target turns the test red rather than quietly vacuous.
    """

    def __init__(self, parties: int, blocked_after: float = 0.1) -> None:
        self.entered = 0
        self._target = "_close_fd"
        self._parties = parties
        self._blocked_after = blocked_after
        self._cv = threading.Condition()
        self._inside: list[int] = []
        self._turn: int | None = None

    def run(self, body: Callable[[], object]) -> Callable[[], None]:
        """Wrap a thread body so its trip through the target is scheduled."""

        def traced() -> None:
            sys.settrace(self._trace)
            try:
                body()
            finally:
                sys.settrace(None)

        return traced

    def _trace(self, frame: FrameType, event: str, arg: Any) -> "TraceFn | None":
        if event == "call" and frame.f_code.co_name == self._target:
            self._enter()
            return self._line
        return None

    def _enter(self) -> None:
        with self._cv:
            self.entered += 1
            self._inside.append(threading.get_ident())
            if len(self._inside) >= self._parties:
                self._turn = self._inside[0]
                self._cv.notify_all()
            else:
                self._cv.wait_for(lambda: len(self._inside) >= self._parties, timeout=10)

    def _line(self, frame: FrameType, event: str, arg: Any) -> "TraceFn | None":
        if event == "line":
            self._take_turn()
        elif event == "return":
            self._leave()
        return self._line

    def _take_turn(self) -> None:
        me = threading.get_ident()
        with self._cv:
            if not self._cv.wait_for(lambda: self._turn == me, timeout=self._blocked_after):
                self._turn = me  # whoever is scheduled is blocked; take over
            others = [t for t in self._inside if t != me]
            self._turn = others[0] if others else me  # one line each, in turn
            self._cv.notify_all()

    def _leave(self) -> None:
        me = threading.get_ident()
        with self._cv:
            if me in self._inside:
                self._inside.remove(me)
            self._parties = max(1, self._parties - 1)
            self._turn = self._inside[0] if self._inside else None
            self._cv.notify_all()


class RecyclingFds(RealFdSyscalls):
    """Real fds, and a freed number is handed straight back out — as POSIX does.

    `open` returns the *lowest* free descriptor, so the number a PTY master had a
    microsecond ago is routinely somebody else's the moment it is closed. That is
    what makes a double close a data-corruption bug rather than an `EBADF` — the
    second one lands on whatever the number has become.

    Modelled with `dup2` onto that exact number rather than by opening and hoping
    it comes back lowest: inside a pytest process there are lower free
    descriptors, so "open something and see" would prove nothing on some runs and
    the point of the test on others.
    """

    def __init__(self, victim_source: int) -> None:
        self._victim_source = victim_source
        self.closed: list[int] = []
        self.recycled = -1
        self.reaps = 0
        self.reading = threading.Event()
        self.may_finish_read = threading.Event()
        self.may_finish_read.set()
        self._lock = threading.Lock()

    def read(self, fd: int, length: int) -> bytes:
        self.reading.set()
        self.may_finish_read.wait(10)
        raise OSError(errno.EIO, "Input/output error")  # the Linux spelling of EOF

    def close(self, fd: int) -> None:
        with self._lock:
            first = not self.closed
            self.closed.append(fd)
        os.close(fd)
        if first:  # the number is live again, and no longer ours
            os.dup2(self._victim_source, fd)
            self.recycled = fd

    def reap(self, pid: int) -> int | None:
        with self._lock:
            self.reaps += 1
            first = self.reaps == 1
        return None if first else 0  # alive for isalive(), reapable right after


def read_once(proc: PosixPty) -> None:
    """A worker thread's whole job, as `PtySession.read` gives it to one."""
    proc.read(4096)


def release(proc: PosixPty) -> None:
    """What the loop thread does when the WebSocket goes away."""
    proc.terminate(force=True)


def victim_file(tmp_path: Path) -> tuple[int, Path]:
    """An unrelated open file, standing in for the next session's descriptor."""
    path = tmp_path / "someone_elses_fd"
    path.write_bytes(b"")
    return os.open(path, os.O_RDWR), path


class TestTheMasterFdHasOneOwner:
    """The fd lifecycle across the two threads that really drive this class.

    `read` blocks in a worker thread (`PtySession.read` -> `asyncio.to_thread`)
    while `terminate`, `write` and `resize` run on the event loop thread. Every
    test here is about the same defect: a descriptor *number* used by one thread
    after another has released it. On POSIX that number is not dead, it is
    reassigned, so the bug reads and writes another session's terminal instead of
    raising — which is why these are proven against real fds and real closes.
    """

    def test_a_reader_at_eof_and_a_terminate_cannot_close_the_same_fd_twice(
        self, tmp_path: Path
    ) -> None:
        """The C1 follow-up bug, driven through both close paths at once.

        Reader-EIO and `terminate()` both reach `_close_fd`. With the two gated
        on an unsynchronised flag, both pass the check before either sets it and
        the fd is closed twice — the second close destroying whatever the OS
        handed the number to in between, which `RecyclingFds` makes concrete.
        """
        source, victim_path = victim_file(tmp_path)
        read_fd, write_fd = os.pipe()
        calls = RecyclingFds(source)
        proc = PosixPty(os.getpid(), read_fd, calls)
        step = Lockstep(parties=2)
        try:
            threads = [
                threading.Thread(target=step.run(lambda: proc.read(4096)), daemon=True),
                threading.Thread(target=step.run(lambda: proc.terminate(force=True)), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
                assert not thread.is_alive()

            assert step.entered == 2, "both paths must have reached _close_fd"
            assert calls.closed == [read_fd], f"closed {len(calls.closed)}x: {calls.closed}"
            # The number the OS reissued is still alive and still someone else's.
            assert calls.recycled == read_fd
            assert os.fstat(read_fd).st_size == 0
        finally:
            for fd in (read_fd, write_fd, source):
                with contextlib.suppress(OSError):
                    os.close(fd)
        assert victim_path.exists()

    def test_a_close_waits_for_a_read_that_is_still_inside_the_syscall(
        self, tmp_path: Path
    ) -> None:
        """Closing under an in-flight read recycles the number beneath it.

        The reader is parked *inside* `os.read` — where a PTY reader spends
        essentially all of its time — when the loop thread terminates the
        session. Releasing the number there means the next `open` anywhere in the
        process takes it, and the read that was already dispatched belongs to
        another session. So the close is deferred to the last thread out.
        """
        source, _ = victim_file(tmp_path)
        read_fd, write_fd = os.pipe()
        calls = RecyclingFds(source)
        calls.may_finish_read.clear()
        proc = PosixPty(os.getpid(), read_fd, calls)
        reader = threading.Thread(target=lambda: proc.read(4096), daemon=True)
        try:
            reader.start()
            assert calls.reading.wait(10), "the reader never entered the syscall"

            proc.terminate(force=True)
            assert calls.closed == [], "the fd was released under a read in flight"

            calls.may_finish_read.set()
            reader.join(30)
            assert not reader.is_alive()
            assert calls.closed == [read_fd]  # the last one out closed it, once
            assert proc.read(4096) == ""  # and the stream stays ended
            assert calls.closed == [read_fd]
        finally:
            calls.may_finish_read.set()
            for fd in (read_fd, write_fd, source):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def test_a_late_write_never_lands_in_the_recycled_descriptor(self, tmp_path: Path) -> None:
        """The same defect where it is deterministic: use *after* the release.

        `write` took no part in the flag at all, so a keystroke arriving after
        the fd was closed went to the raw number — by then an unrelated file.
        This is the corruption spelled out: the victim's bytes, or not.
        """
        source, victim_path = victim_file(tmp_path)
        read_fd, write_fd = os.pipe()
        calls = RecyclingFds(source)
        proc = PosixPty(os.getpid(), write_fd, calls)
        try:
            proc.terminate(force=True)
            assert calls.recycled == write_fd  # the number is someone else's now

            assert proc.write("rm -rf /\r") == 9  # accepted and dropped, never raised
            proc.setwinsize(30, 120)
            os.fsync(source)
        finally:
            for fd in (read_fd, write_fd, source):
                with contextlib.suppress(OSError):
                    os.close(fd)
        assert victim_path.read_bytes() == b"", "keystrokes reached an unrelated descriptor"

    async def test_the_threads_the_router_really_uses_release_the_fd_once(
        self, tmp_path: Path
    ) -> None:
        """The same defect at the topology that produces it, one layer up.

        Nothing below `PtySession` invents these two threads: `read` goes to a
        worker via `asyncio.to_thread` and `terminate` is called from the
        `finally:` in `routers/terminal.py`, on the loop. That is the whole race,
        assembled the way a user closing a terminal tab assembles it — the pump
        blocked in a read on the master while the socket teardown releases the
        session.
        """
        source, _ = victim_file(tmp_path)
        read_fd, write_fd = os.pipe()
        calls = RecyclingFds(source)
        calls.may_finish_read.clear()
        session = PtySession(1, PosixPty(os.getpid(), read_fd, calls))
        try:
            pump = asyncio.create_task(session.read())
            await asyncio.to_thread(calls.reading.wait, 10)
            assert calls.reading.is_set(), "the pump never reached the master fd"

            session.terminate()  # the loop thread, mid-read
            assert calls.closed == [], "released under the pump's read"

            calls.may_finish_read.set()
            async with asyncio.timeout(30):
                assert await pump is None  # end of stream, as the router reads it
            assert calls.closed == [read_fd]
        finally:
            calls.may_finish_read.set()
            for fd in (read_fd, write_fd, source):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def test_the_close_is_claimed_once_however_many_threads_race_for_it(self) -> None:
        """Undriven concurrency: no deadlock, no leak, still exactly one close.

        Stated honestly, this one is **not** a reproduction — it passes against
        the unfixed code too, because the window is the two bytecodes only
        `Lockstep` can land in. What it is for is the other half: the three tests
        above each pin one schedule, and this runs the real one, so the borrow
        counting and the deferred close are exercised by threads that actually
        collide (a terminate landing while readers sit inside `read`) rather than
        only where the interleaver puts them.
        """
        for _ in range(50):
            fake = FakePosix([b""] * 4)
            proc = posix_pty(fake)
            threads = [
                threading.Thread(target=read_once, args=(proc,), daemon=True) for _ in range(4)
            ] + [threading.Thread(target=release, args=(proc,), daemon=True) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
                assert not thread.is_alive()
            assert fake.closed == 1, f"{fake.closed} closes of one fd"


class TestAWedgedChildCannotFreezeTheServer:
    """`PtySession.terminate()` runs on the single asyncio loop thread.

    Uvicorn serves every other websocket and request from that same thread, so
    a wait that is not bounded here is a server-wide stall, not a slow tab.
    """

    def test_the_real_syscall_wrapper_never_issues_a_blocking_waitpid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug, at the only layer that can actually hang: `os.waitpid`."""
        flags: list[int] = []

        def spy(pid: int, options: int) -> tuple[int, int]:
            flags.append(options)
            return 0, 0

        wnohang = 0x2A  # a sentinel, since Windows has no os.WNOHANG to compare to
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "WNOHANG", wnohang, raising=False)
        monkeypatch.setattr(os, "waitpid", spy, raising=False)
        assert _StdlibSyscalls().reap(4242) is None
        assert flags == [wnohang]  # a 0 here is the server-wide freeze

    def test_a_child_that_never_dies_gives_up_inside_the_timeout(self) -> None:
        class Wedged(FakePosix):
            """SIGKILLed, but stuck in uninterruptible I/O — never reapable."""

            def reap(self, pid: int) -> int | None:
                self.reaps += 1
                return None

        fake = Wedged()
        proc = posix_pty(fake)
        assert proc.terminate(force=True)
        assert sum(fake.slept) <= pty_posix.REAP_TIMEOUT_S + pty_posix.REAP_POLL_MAX_S
        assert max(fake.slept) <= pty_posix.REAP_POLL_MAX_S  # no single long stall
        assert fake.closed == 1  # the fd is released even though the child is not
        assert proc.exit_status is None

    async def test_a_wedged_child_reaps_on_a_worker_thread_not_the_caller_s(self) -> None:
        """`release_async` is the only release an event loop may call.

        The assertion is which thread the poll sleeps on: `release` blocks for
        `REAP_TIMEOUT_S`, and uvicorn serves every other socket from the thread
        that would otherwise be doing the waiting.
        """
        fake = WedgedChild()
        manager = PtyManager()
        session = PtySession(1, posix_pty(fake))
        manager._sessions[session.session_id] = session

        await manager.release_async(session)

        assert fake.slept_on, "the wedged child never reached the polling reap"
        assert threading.get_ident() not in fake.slept_on
        assert manager._sessions == {}

    async def test_shutdown_reaps_every_wedged_terminal_at_once_not_one_by_one(self) -> None:
        """N wedged children must cost one `REAP_TIMEOUT_S`, not N of them.

        The barrier *is* the assertion, and it is what makes this deterministic
        rather than a wall-clock guess: three reaps can only get through a
        3-party barrier if all three are in flight at the same moment. A
        shutdown that releases sessions in a `for` loop leaves the first one
        waiting there alone until the barrier times out and breaks.
        """
        parties = 3
        barrier = threading.Barrier(parties)
        fakes = [InLockstep(barrier, pid=100 + i) for i in range(parties)]
        manager = PtyManager()
        for fake in fakes:
            session = PtySession(fake.pid, posix_pty(fake))
            manager._sessions[session.session_id] = session

        await manager.shutdown()

        assert all(f.met_the_others for f in fakes), "the reaps ran one after another"
        # Every child was force-killed — via its group, which is the delivery a
        # healthy fake takes; the pid path is the fallback and is not wanted here.
        assert all(f.groups == [(f.pid, True)] for f in fakes)
        assert all(f.signals == [] for f in fakes)
        assert threading.get_ident() not in [t for f in fakes for t in f.slept_on]
        assert manager._sessions == {}

    def test_a_child_that_dies_late_is_reaped_by_the_next_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Giving up on the wait must not mean giving up on the zombie."""

        class Wedged(FakePosix):
            reapable = False

            def reap(self, pid: int) -> int | None:
                self.reaps += 1
                return 9 if self.reapable else None

        fake = Wedged()
        posix_pty(fake).terminate(force=True)
        assert [(fake.pid, fake)] == pty_posix._UNREAPED

        assert pty_posix.sweep_unreaped() == 1  # still wedged: still tracked
        fake.reapable = True
        monkeypatch.setenv("SHELL", "/bin/sh")
        pty_posix.spawn(Path.cwd(), syscalls=FakePosix())
        assert pty_posix._UNREAPED == []


class HeldInTerminate(FakePosix):
    """A child whose kill parks until the test lets go, counting its company.

    The window every test below needs open: while one release is inside the
    signal, a second one has every chance to arrive. `most_at_once` above 1
    means two OS threads were signalling and reap-polling one pid together.

    The park is on `killpg` rather than `kill` because that is where a healthy
    child's signal is actually delivered: `_signal` reaches the bare pid only
    when it has no group it may signal, which a default `FakePosix` — its own
    session and group leader, like anything out of `pty.fork()` — never hits.
    Parking on `kill` here would hold a call nothing makes, and every test below
    would sail past a window that was never open.
    """

    def __init__(self, pid: int = 4242) -> None:
        super().__init__(pid=pid)
        #: Set once some thread is inside the signal — what a test waits on
        #: instead of sleeping and hoping.
        self.entered = threading.Event()
        self.may_finish = threading.Event()
        self.most_at_once = 0
        self._inside = 0
        self._tally = threading.Lock()

    def killpg(self, pgid: int, *, force: bool) -> None:
        with self._tally:
            self._inside += 1
            self.most_at_once = max(self.most_at_once, self._inside)
        self.entered.set()
        # Longer than any `wait_for` below, so a test fails on its own assertion
        # rather than on this timeout quietly rescuing the racer.
        self.may_finish.wait(timeout=5)
        with self._tally:
            self._inside -= 1
        super().killpg(pgid, force=force)


class TestOneChildIsHandedBackOnce:
    """Two callers race to release every session, and they always did.

    The socket handler's `finally:` releases the session it owns; `shutdown()`
    snapshots `_sessions` and releases everything still in it. A drop landing as
    the server is asked to stop — a deploy with terminals open — is both of them
    on the same session. What changed is that `release` used to be synchronous
    and awaited nothing, so the one loop thread serialized any two calls to it
    for free; on a real teardown pool that is gone, and the dict has to be the
    claim instead.
    """

    async def test_a_second_concurrent_release_signals_nothing(self) -> None:
        fake = HeldInTerminate()
        manager = PtyManager()
        session = PtySession(fake.pid, posix_pty(fake))
        manager._sessions[session.session_id] = session

        winner = asyncio.create_task(manager.release_async(session))
        assert await asyncio.to_thread(fake.entered.wait, 5), "no release reached the child"
        # Created while the winner is parked in the signal, so it cannot help
        # but overlap. It must come straight back having touched no process;
        # unguarded it queued behind `may_finish` inside the same kill and this
        # is the await that hung.
        loser = asyncio.create_task(manager.release_async(session))
        await asyncio.wait_for(loser, timeout=2)

        fake.may_finish.set()
        await winner

        assert fake.most_at_once == 1, "two threads signalled the same pid"
        assert fake.groups == [(fake.pid, True)]  # ...and it was killed exactly once
        assert manager._sessions == {}

    async def test_shutdown_leaves_a_release_already_in_flight_alone(self) -> None:
        """The finding's own sequence: the socket drops, then the server stops."""
        fake = HeldInTerminate()
        manager = PtyManager()
        session = PtySession(fake.pid, posix_pty(fake))
        manager._sessions[session.session_id] = session

        handler = asyncio.create_task(manager.release_async(session))  # the peer vanished
        assert await asyncio.to_thread(fake.entered.wait, 5)

        # While that is still running, the lifespan's teardown arrives. It used
        # to find the session still listed — nothing was popped until after the
        # terminate returned — and queue a second independent release for the
        # same pid, so this await sat behind the parked child instead of
        # finding nothing left to do.
        await asyncio.wait_for(manager.shutdown(), timeout=2)

        fake.may_finish.set()
        await handler

        assert fake.most_at_once == 1
        assert fake.groups == [(fake.pid, True)]
        assert manager._sessions == {}

    async def test_releasing_the_same_session_twice_in_a_row_is_a_no_op(self) -> None:
        """The uncontended half of the same rule, and the cheap one to keep."""
        fake = FakePosix()
        manager = PtyManager()
        session = PtySession(fake.pid, posix_pty(fake))
        manager._sessions[session.session_id] = session

        await manager.release_async(session)
        await manager.release_async(session)

        assert fake.groups == [(fake.pid, True)]

    async def test_one_release_that_raises_does_not_abandon_the_others(self) -> None:
        """`shutdown` is awaited from the lifespan, and it is not the last line.

        A bare `gather` propagates the first failure and drops the rest, which
        here meant a sibling terminal never signalled *and* everything the
        lifespan still owed after this call — closing the shared HTTP client,
        logging that the server stopped — silently skipped.
        """

        class Exploding(FakePosix):
            """Failing some way `PtySession.terminate`'s `except OSError` misses.

            On `killpg` because that is the call a healthy child's signal goes
            through, and `RuntimeError` because `_killpg` swallows `OSError` by
            design — a kernel refusing one group is a degradation it recovers
            from, not the unhandled backend failure this test is about.
            """

            def killpg(self, pgid: int, *, force: bool) -> None:
                raise RuntimeError("the backend came apart")

        bad, good = Exploding(pid=101), FakePosix(pid=102)
        manager = PtyManager()
        for fake in (bad, good):
            manager._sessions[fake.pid] = PtySession(fake.pid, posix_pty(fake))

        await manager.shutdown()  # must not raise

        assert good.groups == [(good.pid, True)], "a sibling's failure took this release with it"
        assert bad.groups == []  # it did fail — that is the point of the fixture
        assert manager._sessions == {}  # and a failed release still clears the registry


# --- the real fork, on the legs that have one ---------------------------------
#
# Everything above drives `PosixSyscalls` through a fake, which proves what
# `terminate` *decides* and nothing about what the kernel does with it. Process
# groups are exactly where those two part company: whether the command the user
# was running is inside the group we signal is a question about `setsid`, job
# control and the controlling tty, and a fake has none of those. So these fork a
# real shell on a real pty and kill it for real. Windows has no `pty` and
# `emscripten`/`wasi` have no `fork`, so the guard is the same "a POSIX backend
# exists" shape `test_terminal.py` uses.
posix_only = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="pty.fork/killpg/tcgetpgrp are POSIX; a Windows terminal is pywinpty",
)

#: What the user leaves running when they close the pane. Three real processes
#: deep — the shell we forked, the command it runs, and *that* command's own
#: child — and the innermost one ignores SIGHUP.
#:
#: The ignore is the whole point, not a trick to make the test red. When a
#: session leader dies the kernel's only cleanup is a SIGHUP to the terminal's
#: foreground group, so SIGHUP is the one signal a long-running job is likely to
#: have opted out of: anything under `nohup`, anything with its own handler, any
#: script that traps it to keep working over a dropped connection. `trap "" HUP`
#: survives the `exec` because POSIX inherits an *ignored* disposition across
#: `execve` — which is precisely how `nohup` itself works — so the `sleep`
#: holding the pid at the end is HUP-proof the way a real one is.
JOB_SCRIPT = """\
#!/bin/sh
# $1: the file the innermost process announces its pid in, written atomically so
# the test never reads half a number.
sh -c 'trap "" HUP; echo $$ > "$1.tmp"; mv "$1.tmp" "$1"; exec sleep 30' sh "$1"
"""


def _pgid(pid: int) -> int:
    """`os.getpgid` behind the `sys.platform` guard mypy needs on Windows.

    Same `if/else` shape as `_StdlibSyscalls`: the branch is what lets a strict
    check run on both platforms, since these names do not exist on win32.
    """
    if sys.platform == "win32":  # pragma: no cover - every caller is posix_only
        raise NotImplementedError
    else:
        return os.getpgid(pid)


def _sid(pid: int) -> int:
    """`os.getsid`, same guard."""
    if sys.platform == "win32":  # pragma: no cover - every caller is posix_only
        raise NotImplementedError
    else:
        return os.getsid(pid)


def _alive(pid: int) -> bool:
    """Does this pid still name a process? Signal 0 is the probe that asks."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - it exists; we just may not signal it
        return True
    return True


def _gone_within(pid: int, timeout: float = 15.0) -> bool:
    """Bounded wait for a pid to disappear. Not our child, so there is no wait()."""
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def _announced_pid(path: Path, timeout: float = 30.0) -> int | None:
    """The pid the job wrote, once it has written one."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        time.sleep(0.02)
    return None


@posix_only
@pytest.mark.timeout(120)
class TestAJobDoesNotOutliveItsTerminal:
    """A closed pane must take the command that was running in it.

    These run on the ubuntu and macos legs of the 3-OS matrix (M7 §C2) and skip
    on Windows, where the backend is pywinpty and ConPTY owns the lifetime.
    """

    def test_the_forked_shell_is_a_session_and_group_leader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`pty.fork()` calls `setsid()` — which is what makes a group kill safe.

        Signalling a process *group* is only ever correct because the child has
        one of its own. If the fork path ever stopped calling `setsid` the child
        would share **our** group, and a group SIGKILL would take the whole
        server down with one terminal. `terminate` guards against that at
        runtime; this is the proof that the guard is the backstop rather than
        the thing the normal case rests on.
        """
        monkeypatch.setenv("SHELL", "/bin/sh")
        proc = pty_posix.spawn(tmp_path)
        try:
            assert _sid(proc.pid) == proc.pid  # a session of its own
            assert _pgid(proc.pid) == proc.pid  # and a group of its own
            assert _pgid(proc.pid) != _pgid(0)  # which is never ours
        finally:
            proc.terminate(force=True)

    def test_the_master_side_can_read_the_terminals_foreground_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The ioctl the job kill leans on, asked of the master fd directly.

        `TIOCGPGRP` on the *master* is what names the group the terminal is
        currently giving the keyboard to — the running job's own group, once an
        interactive shell has done job control. Linux answers it for a master
        even though the pty is not this process's controlling terminal. If a
        kernel refuses, `terminate` degrades to the child's own group rather
        than failing, so this test says so out loud (CI runs `pytest -rs`, which
        prints the reason) instead of narrowing an assertion until it passes.
        """
        monkeypatch.setenv("SHELL", "/bin/sh")
        proc = pty_posix.spawn(tmp_path)
        calls = _StdlibSyscalls()
        try:
            pgid = 0
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    pgid = calls.foreground_pgid(proc._fd)
                except OSError as exc:
                    pytest.skip(f"this kernel will not read TIOCGPGRP from a master: {exc}")
                if pgid > 0:  # 0 until the child has finished claiming the tty
                    break
                time.sleep(0.01)
            assert pgid == proc.pid  # nothing running yet, so: the shell itself
        finally:
            proc.terminate(force=True)

    async def test_a_running_job_does_not_outlive_the_terminal_that_started_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The bug, assembled the way a user assembles it.

        A shell, a command running under it, that command's own child — then the
        pane closes. Signalling only the shell's pid kills the shell; the kernel
        sends SIGHUP to the terminal's foreground group on the way out, and the
        one process that ignores SIGHUP is left running with no terminal and no
        parent. An orphaned build, `ssh` or dev server, for the life of the box.

        The teardown is the real one, and it is the *whole* real one:
        `release_async`, which is what `routers/terminal.py` awaits in its
        `finally:` when the WebSocket goes, and which runs the group-signalling
        `terminate` on the teardown pool rather than on the loop thread. Driving
        it from here is what proves the two halves compose — a job kill that
        reads `TIOCGPGRP` through the borrow-guarded fd is doing that from a
        pool thread, and the fd guard, not the caller's thread, is what makes
        that safe.
        """
        monkeypatch.setenv("SHELL", "/bin/sh")
        script = tmp_path / "job.sh"
        script.write_text(JOB_SCRIPT, encoding="utf-8")
        pid_file = tmp_path / "grandchild.pid"

        manager = PtyManager()
        session = manager.spawn(tmp_path)
        shell = cast(PosixPty, session._proc)
        grandchild = 0
        try:
            session.write(f"sh '{script}' '{pid_file}'\r")
            found = _announced_pid(pid_file)
            assert found is not None, "the job never started under the shell"
            grandchild = found

            # The tree really is the one the bug is about: a live process in the
            # shell's session that is not the shell.
            assert grandchild != shell.pid
            assert _sid(grandchild) == shell.pid
            job_group, shell_group = _pgid(grandchild), _pgid(shell.pid)

            await manager.release_async(session)

            assert _gone_within(grandchild), (
                f"pid {grandchild} outlived its terminal: job group {job_group}, "
                f"shell group {shell_group} — "
                + (
                    "job control put the job in a group of its own"
                    if job_group != shell_group
                    else "the job shared the shell's group"
                )
            )
        finally:
            await manager.shutdown()
            if grandchild:
                with contextlib.suppress(OSError):
                    os.kill(grandchild, 9)  # SIGKILL, spelled without the posix-only name
