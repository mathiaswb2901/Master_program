"""The managed worktree pool: parallel work that cannot step on itself.

``CLAUDE.md`` has required "one writer per checkout, always" since M4, enforced
by discipline. This service makes it a feature: a small pool of git worktrees a
caller **borrows** a slot from, works in, and gives back. It is also the
substrate Mission Control's workers need.

Four decisions, taken in the ROADMAP and implemented here rather than described:

**1. Detached HEAD.** Every slot is checked out with ``git worktree add
--detach``, so a pooled worktree carries no branch and git's *"fatal: 'x' is
already checked out at ..."* cannot happen. This is not a nicety — it is the
wall every fix-stage agent in this repo's own workflow hits when two lanes want
the same branch. What a holder does inside its slot (create a branch, commit,
push) is the holder's business; what the pool hands out is a commit.

**2. Pool, never destroy.** There is no ``git worktree remove`` in this module,
and no ``shutil.rmtree``. A finished slot is reset and returned, so
``node_modules``, ``.venv`` and build caches stay with it — they are *ignored*
files, and the only cleaning done here is ``git clean -fd``, never ``-x``. A
cold install is therefore paid once per slot rather than once per task. It also
avoids a hazard measured on this machine: ``git worktree remove`` recurses
through a Windows junction, so any design that links dependencies into a slot
can empty the checkout they point at.

**3. Two idle signals.** A lease carries an ``owner_pid`` *and* an
``expires_at``, and :meth:`WorktreeService.prune` reclaims a slot only when
**both** say idle. The deadline holds a slot for an agent working in it
unattended with nothing of ours running; the pid holds it past the deadline for
an owner that is demonstrably still there. Either alone reaps a working slot.

**4. Fail safe on corrupt state.** If ``pool.json`` is truncated, unreadable or
written by a version this one does not understand, the pool is rebuilt from what
is on disk and **every** slot comes back ``leased`` under a ``recovered`` lease.
Assume in use; never assume free.

**Dirty is sacred**, and it outranks all four. A slot whose ``git status
--porcelain`` is non-empty is never handed out and never reclaimed without an
explicit ``force``. Losing a user's uncommitted work is the one unrecoverable
failure here, so every path that could reach a working tree asks first — and a
``git status`` that *fails* is read as dirty, never as clean.

**Where the pool lives, and why it is not in the workspace.** The pool root is
under the machine's app data directory, keyed by the workspace it serves. Not
under ``.workbench/``: a worktree inside the workspace would be walked by
``services/workspace.py``'s tree, watched by ``services/watcher.py``, and
indexed as N more copies of the project — every file in the repo would appear
``pool_size`` times in the file tree and every checkout would arrive as a
watcher storm. Outside the root, ``safe_path`` refuses it and the watcher never
sees it, which is the property ``test_worktrees.py`` asserts rather than assumes.

**When the reset happens.** A clean slot is returned *as it is*; the reset that
repurposes it runs at **acquire** time. Two reasons, and the second is the
load-bearing one: acquire is the only moment the pool knows which commit to
reset *to*, and it is the moment nothing is running in the slot — whereas a
release fires exactly as the holder's own processes are letting go of their file
handles, which is when a Windows reset is most likely to fail. Commits a holder
made and did not push are not destroyed by that reset: nothing here runs ``gc``,
``worktree remove`` or ``clean -x``, so they stay in the object database,
reachable through the slot's own ``HEAD`` reflog.

**And why that reset is ``--keep``.** A dirty check and the reset behind it are
two separate git processes, so there is a gap between them, and a slot that was
clean when it was asked can be written to before the reset lands — by a build
daemon the previous holder left running, a language server, an indexer: exactly
the class of background writer the Windows lock note above is about. Under
``reset --hard`` that write was overwritten with no state change, no event and
no log line, which is the one failure this service is not allowed to have.
:data:`ResetMode` is the fix: ``--keep`` refuses to overwrite a locally-modified
file, so the decision and the destruction happen inside *one* git process
instead of across a gap, and :meth:`WorktreeService._verify_reset` re-reads
status afterwards to catch the writes ``--keep`` preserves rather than refuses.
``--hard`` survives only where destruction is what the caller asked for by name.

**One writer per pool, enforced by the OS.** The ``asyncio.Lock`` below is
exactly as wide as one interpreter. Two ``workbench-server`` processes on the
same workspace share a pool root and a ``pool.json``, and nothing in Python
stops both from reading one slot as free and leasing it to different holders —
one checkout, two writers, the invariant this feature exists to provide. So
:class:`PoolLock` takes an exclusive byte-range lock on a file in the pool root
for the life of the process; a server that cannot take it serves no pool and
says so, and everything else in the app starts normally.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import structlog
from pydantic import ValidationError

from workbench_server.models.worktrees import (
    MAX_DETAIL_CHARS,
    MAX_POOL_SIZE,
    POOL_STATE_VERSION,
    AcquireWorktreeRequest,
    KeptSlot,
    PruneResult,
    WorktreeChangedEvent,
    WorktreeInfo,
    WorktreeLease,
    WorktreePool,
    WorktreePoolState,
    WorktreeState,
)
from workbench_server.services.event_bus import EventBus

log = structlog.get_logger()

#: The state document, next to the slots it describes.
POOL_STATE_FILE = "pool.json"

#: The cross-process guard, next to the state it protects. See :class:`PoolLock`.
POOL_LOCK_FILE = "pool.lock"

#: One sentence for a slot git would not talk about, from one place — the same
#: reason :func:`dirty_detail` exists: ``_transition`` publishes only when
#: something changed, so two wordings for one situation *look* like a change.
STATUS_UNREADABLE = "git could not report this slot's status"

#: Ceilings on one git call. ``worktree add`` copies a whole checkout out, so it
#: gets its own; everything else is metadata work that either lands promptly or
#: is not going to. Nothing here waits forever — a git that hangs would hang the
#: request that started it and, through it, the lifespan shutdown.
GIT_TIMEOUT_S = 60.0
GIT_CHECKOUT_TIMEOUT_S = 600.0

#: Windows again, and the same shape as ``services/layouts.py``'s ``os.replace``
#: retry: a live process holding a handle on a file in the slot makes
#: ``git reset --hard`` fail with "Permission denied" rather than wait. The
#: holder is usually transient — the previous owner's interpreter shutting down,
#: Defender, the search indexer — so a short bounded retry turns a failed reset
#: into a marginally slower one.
#:
#: What we deliberately do **not** do is reach for ``--force``: git's reset is
#: already forceful, the failure is the filesystem's, and a slot the pool cannot
#: put back honestly becomes ``needs_review`` instead of a lie.
RESET_ATTEMPTS = 5
RESET_BACKOFF_S = 0.2

#: The two resets this service runs, and the difference between them is the
#: whole safety story of :meth:`WorktreeService._prepare`.
#:
#: ``--keep`` repurposes a slot the pool believes is *free*. It updates the files
#: that differ between ``HEAD`` and the target and **aborts** rather than
#: overwrite one that has local changes — which is what folds the dirty check
#: into the destructive call itself. A separate ``git status`` followed by
#: ``reset --hard`` cannot do that: they are two processes, and a writer that
#: lands between them loses its work silently.
#:
#: ``--hard`` is kept for exactly the two paths where destroying uncommitted
#: work is what the caller asked for out loud — ``release(discard_changes=True)``
#: and ``prune(force=True)`` — and nowhere else. Neither is ``--force``: git's
#: reset is already forceful and the flag this service refuses to reach for is
#: still refused.
ResetMode = Literal["--keep", "--hard"]

#: A discard that could not leave the slot empty. One sentence, because both
#: causes (a handle held open, a process still writing) have the same answer.
DISCARD_FAILED_DETAIL = (
    f"could not empty this slot after {RESET_ATTEMPTS} attempts — something is "
    "holding a file open in it, or still writing to it"
)

#: ``os.replace`` onto the state file, same story as ``services/layouts.py``.
REPLACE_ATTEMPTS = 10
REPLACE_BACKOFF_S = 0.02

#: Exit code Windows reports for a process that is still running, which is also
#: a legal exit code — see :func:`process_alive`.
_STILL_ACTIVE = 259


def dirty_detail(count: int) -> str:
    """One sentence for a parked slot, from one place.

    Two wordings for one situation is not a style problem here: ``_transition``
    publishes only when something changed, and a slot re-described in different
    words on the next sweep *looks* changed. So the sentence is a function of
    the count and nothing else. It names ``force`` rather than the release
    flag, because a parked slot has no lease left to release with.
    """
    return f"{count} uncommitted change(s) kept — prune with force to reset this slot"


class PoolUnavailableError(Exception):
    """There is no pool to serve from: no git, or the workspace is not a repository."""


class PoolExhaustedError(Exception):
    """Every slot is held, dirty or under review, and the pool is at capacity."""


class SlotNotFoundError(Exception):
    """No slot by that name."""


class LeaseError(Exception):
    """The lease presented does not hold this slot.

    Deliberately the same answer for "wrong id", "already released" and "this
    slot is not leased at all": a caller holding a stale lease must not be able
    to release or renew a slot somebody else now owns, and telling it *which*
    of the three is true tells it nothing it can act on.
    """


class GitError(Exception):
    """A git command failed or timed out. Carries the first line of its stderr."""


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def first_error_line(self) -> str:
        for line in (self.err or self.out).splitlines():
            if line.strip():
                return line.strip()[:MAX_DETAIL_CHARS]
        return f"git exited {self.code}"


class GitRunner(Protocol):
    """The seam. Real git by default; tests wrap it to inject a Windows lock."""

    async def __call__(self, cwd: Path, args: Sequence[str], timeout_s: float) -> GitResult: ...


#: Injected so a test can drive lease expiry without sleeping through it.
Clock = Callable[[], float]
#: Injected so a test can say "that pid is gone" without ending a real process.
AliveProbe = Callable[[int], bool]


async def run_git(cwd: Path, args: Sequence[str], timeout_s: float) -> GitResult:
    """One git invocation, bounded, with no terminal it could stop and ask.

    ``GIT_TERMINAL_PROMPT=0`` matters more than it looks: a repository with a
    credential helper misconfigured turns any command into a process waiting on
    a prompt that no one will ever see, inside a request nobody can cancel.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as err:  # git is not installed, or cwd vanished
        raise GitError(f"could not run git: {err}") from err
    try:
        out, err_bytes = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError as err:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        await proc.wait()
        raise GitError(f"git {' '.join(args[:2])} timed out after {timeout_s:.0f}s") from err
    return GitResult(
        proc.returncode or 0,
        out.decode("utf-8", errors="replace").strip(),
        err_bytes.decode("utf-8", errors="replace").strip(),
    )


#: Same import-time platform split ``services/office_host/office_com.py`` uses.
#: Through a constant rather than as a bare ``sys.platform`` comparison, because
#: ``warn_unreachable`` would otherwise call the POSIX half of every branch below
#: dead code on the Windows-only type-check this project runs.
WINDOWS = sys.platform == "win32"


def app_data_dir() -> Path:
    """Machine-local Workbench state that is *not* the user's project.

    ``%LOCALAPPDATA%`` on Windows, which is where a Windows-first app puts
    working data it does not want roamed or backed up. Elsewhere it follows the
    ``~/.workbench`` convention ``services/shortcuts.py`` already established.
    """
    local = os.environ.get("LOCALAPPDATA")
    if WINDOWS and local:
        return Path(local) / "Workbench"
    return Path.home() / ".workbench"


def normalized_path(text: str) -> Path:
    """A path out of git's output: separators normalized, no filesystem touched.

    A sync helper on purpose. ``os.path.normpath`` is pure string work, but
    ``ASYNC240`` cannot know that and flags every ``os.path`` call inside an
    async function — rightly, since almost all of them do block. Keeping the two
    string helpers here is cheaper than teaching the linter the difference.
    """
    return Path(os.path.normpath(text.strip()))


def same_dir(left: Path, right: Path) -> bool:
    """Do two path *strings* name the same directory?

    Pure string work — no ``resolve()``, no ``stat()``: it is called from async
    code, where a blocking filesystem call is the thing ``ASYNC240`` exists to
    stop. ``normcase`` is what makes ``C:/Pool/slot-01`` and
    ``C:\\pool\\slot-01`` the same directory on Windows and different ones
    everywhere else, which is exactly the platform rule.
    """
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def workspace_key(workspace_root: Path) -> str:
    """A readable, stable, collision-free directory name for one workspace.

    The name is for the human who opens the pool root in Explorer; the digest is
    what makes it unique. ``normcase`` because two spellings of one Windows path
    must not get two pools.
    """
    normalized = os.path.normcase(str(workspace_root))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    stem = workspace_root.name or "workspace"
    return f"{stem}-{digest}"


def default_pool_root(workspace_root: Path) -> Path:
    return app_data_dir() / "worktrees" / workspace_key(workspace_root)


def process_alive(pid: int) -> bool:
    """Idle signal one, and it errs towards "still working" on purpose.

    **Never** ``os.kill(pid, 0)``: on Windows CPython maps that to
    ``TerminateProcess``, so the liveness probe would kill the process it is
    asking about. This opens a query-only handle instead.

    Two known imprecisions, both in the safe direction — they hold a slot
    *longer*, and a slot held too long costs a wait while a slot reclaimed too
    early costs an agent's work: a process that exited with code 259 is
    indistinguishable from a running one (``STILL_ACTIVE`` is also a legal exit
    code), and a recycled pid reads as alive. The lease deadline is the other
    signal precisely because this one cannot be exact.
    """
    if pid <= 0:
        return False
    if WINDOWS:
        return _alive_windows(pid)
    return _alive_posix(pid)


def _alive_windows(pid: int) -> bool:
    """``OpenProcess`` + ``GetExitCodeProcess``: ask, never touch."""
    import ctypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False  # gone, or a process this account may not ask about
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by somebody else
    return True


def _lock_exclusively(fd: int) -> None:
    """Take a non-blocking exclusive lock on one byte, or raise ``OSError``.

    One byte is enough: nothing reads the file's contents, and a byte-range lock
    is the cheapest thing both platforms agree on. The platform test is written
    as a literal ``sys.platform`` comparison rather than through the
    :data:`WINDOWS` constant used elsewhere in this module, because here it has
    to narrow the *import* — ``fcntl`` does not exist on Windows and the
    Windows-only type-check this project runs must skip that branch rather than
    fail to resolve it.
    """
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    """Drop the lock. Best effort: the close behind it releases it regardless."""
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


class PoolLock:
    """One writer per *pool*, enforced by the operating system.

    :class:`WorktreeService` serialises its own callers with an
    ``asyncio.Lock``, which is exactly as wide as one interpreter — it prevents
    two concurrent requests to one server from racing, and nothing else. Two
    ``workbench-server`` processes pointed at the same workspace (a crashed
    server whose old process has not fully exited, two dev instances started
    against the same folder by mistake) share a pool root and a ``pool.json``.
    Both can read the same slot as ``free``, both can prepare it, both can lease
    it to a different holder, and the last one to save wins the state file: one
    checkout handed to two writers, which is precisely the invariant this whole
    feature exists to provide.

    So the guard is the OS's rather than the language's, and it is held by a
    **handle** on purpose. Windows and POSIX both drop a file lock when the
    holding process dies, so a server that is killed cannot leave a pool
    permanently unopenable — a lock file whose mere *existence* meant "held"
    would need a liveness story of its own, and this module already knows how
    unreliable those are (see :func:`process_alive`).
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def acquire(self, root: Path) -> str | None:
        """Take it. ``None`` on success, else the sentence to report."""
        path = root / POOL_LOCK_FILE
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as err:
            return f"the pool lock {path} could not be opened: {err.strerror or err}"
        try:
            _lock_exclusively(fd)
        except OSError:
            os.close(fd)
            return (
                f"another process is already serving the worktree pool at {root} — "
                "one server per workspace, so this one will not hand out slots"
            )
        self._fd = fd
        return None

    def release(self) -> None:
        """Give it back. Safe to call when it was never taken."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        _unlock(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


class WorktreeService:
    """The pool: slots by name, the git behind them, and the events."""

    def __init__(
        self,
        workspace_root: Path,
        bus: EventBus,
        *,
        pool_root: Path | None = None,
        capacity: int = 4,
        lease_seconds: float = 3600.0,
        runner: GitRunner = run_git,
        clock: "Clock" = time.time,
        alive: "AliveProbe" = process_alive,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._bus = bus
        self._root = (pool_root or default_pool_root(self._workspace_root)).resolve()
        self._capacity = max(1, min(capacity, MAX_POOL_SIZE))
        self._lease_seconds = lease_seconds
        self._git = runner
        self._clock = clock
        self._alive = alive
        self._repo: Path | None = None
        self._slots: dict[str, WorktreeInfo] = {}
        self._problem: str | None = "the worktree pool has not been started"
        #: Serialises this process's own callers.
        self._lock = asyncio.Lock()
        #: Serialises this process against every *other* one. See :class:`PoolLock`.
        self._pool_lock = PoolLock()

    # ---- properties ---------------------------------------------------------

    @property
    def root(self) -> Path:
        """The pool root. Outside the workspace — asserted, not assumed."""
        return self._root

    @property
    def repo(self) -> Path | None:
        return self._repo

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Find the repository, read the state, reconcile it with the disk.

        Never raises. A machine with no git, a workspace that is not a
        repository, or a pool root that cannot be created are all reported
        through :attr:`WorktreePool.problem` — the rest of the app must start.
        """
        async with self._lock:
            try:
                repo = await self._discover_repo()
            except GitError as err:
                self._problem = str(err)
                log.info("worktrees.unavailable", detail=self._problem)
                return
            try:
                self._root.mkdir(parents=True, exist_ok=True)
            except OSError as err:
                self._problem = f"pool root {self._root} is unusable: {err.strerror or err}"
                log.warning("worktrees.root_unusable", root=str(self._root), detail=self._problem)
                return
            # Before anything reads pool.json, and before `self._repo` is set:
            # a process that does not hold the pool lock must not be able to
            # reach `acquire`, and `_require_repo` is the gate that stops it.
            held = self._pool_lock.acquire(self._root)
            if held is not None:
                self._problem = held
                log.warning("worktrees.pool_locked_elsewhere", root=str(self._root), detail=held)
                return
            self._repo = repo
            known, self._problem = self._load_state()
            self._slots = await self._reconcile(known)
            self._save_state()
            log.info(
                "worktrees.ready",
                root=str(self._root),
                repo=str(self._repo),
                slots=len(self._slots),
                capacity=self._capacity,
                problem=self._problem,
            )

    async def stop(self) -> None:
        """Hand the pool back to the next process. Never raises.

        The OS would release the lock when this process exits anyway; releasing
        it here is what lets a *clean* shutdown be followed immediately by
        another server on the same workspace, instead of one that depends on how
        fast the old process is reaped.
        """
        async with self._lock:
            self._pool_lock.release()

    # ---- reading ------------------------------------------------------------

    def snapshot(self) -> WorktreePool:
        """The whole pool. Cheap: it reads memory, never git."""
        return WorktreePool(
            root=str(self._root),
            repo=str(self._repo) if self._repo else None,
            capacity=self._capacity,
            slots=[self._slots[name] for name in sorted(self._slots)],
            problem=self._problem,
        )

    # ---- acquiring ----------------------------------------------------------

    async def acquire(self, request: AcquireWorktreeRequest) -> WorktreeInfo:
        """Borrow a slot, detached at ``base``.

        Prefers a slot the pool already has — that is the whole point of a pool,
        and the reason a warm ``node_modules`` is worth more than a tidy disk —
        and only creates a new one when every existing slot is busy.
        """
        async with self._lock:
            repo = self._require_repo()
            base = await self._resolve_commit(request.base or "HEAD")
            for name in sorted(self._slots):
                if self._slots[name].state != "free":
                    continue
                info = await self._prepare(name, base)
                if info is None:
                    continue  # found dirty, or would not reset: try the next one
                return self._lease(name, request, base)
            if len(self._slots) < self._capacity:
                name = await self._create_slot(repo, base)
                return self._lease(name, request, base)
            raise PoolExhaustedError(self._exhausted_detail())

    async def _prepare(self, name: str, base: str) -> WorktreeInfo | None:
        """Put an existing free slot at ``base``, or take it out of service.

        Returns ``None`` when the slot cannot be handed out — and it has already
        been moved to ``dirty`` or ``needs_review`` and published by then, so
        the caller only has to try the next one.

        Three questions, not one, because the first two are separate git
        processes and a slot can change between them. The check below says the
        slot was clean *when it was asked*; ``--keep`` is what makes the reset
        itself refuse to overwrite a write that landed since; and
        :meth:`_verify_reset` catches the writes ``--keep`` silently *preserves*
        rather than refuses. Only a slot that answers all three is leased out,
        so "reset succeeded, slot leased" is a fact rather than a hope.
        """
        path = Path(self._slots[name].path)
        dirty = await self._dirty_count(path)
        if dirty is None:
            self._transition(name, "needs_review", detail=STATUS_UNREADABLE)
            return None
        if dirty > 0:
            # The state file said free and the disk says otherwise. The disk
            # wins, always: somebody's work is in there.
            log.warning("worktrees.free_slot_was_dirty", slot=name, files=dirty)
            self._transition(name, "dirty", detail=dirty_detail(dirty), dirty_files=dirty)
            return None
        if not await self._reset_to(path, base, mode="--keep"):
            await self._reset_refused(name, path, base)
            return None
        return await self._verify_reset(name, path)

    async def _reset_refused(self, name: str, path: Path, base: str) -> None:
        """A reset that would not run, and *why* — asked of git, not of stderr.

        Two causes with the same exit code and very different meanings: a file
        somebody wrote between the check and the reset (``--keep`` aborts rather
        than destroy it — the slot is dirty and the work is intact), or a handle
        held open so the reset cannot touch the tree at all (the slot is clean
        and unusable). Re-reading status tells them apart without matching a
        substring of git's error text, and the two land in different states
        because they need different answers: one heals when the writer stops,
        the other wants a human.
        """
        raced = await self._dirty_count(path)
        if raced is None:
            self._transition(name, "needs_review", detail=STATUS_UNREADABLE)
        elif raced > 0:
            log.warning("worktrees.acquire_raced_a_writer", slot=name, files=raced, refused=True)
            self._transition(name, "dirty", detail=dirty_detail(raced), dirty_files=raced)
        else:
            self._transition(
                name,
                "needs_review",
                detail=(
                    f"could not reset to {base[:12]} after {RESET_ATTEMPTS} attempts — "
                    "something is holding a file open in this slot"
                ),
            )

    async def _verify_reset(self, name: str, path: Path) -> WorktreeInfo | None:
        """The reset said yes. Make it say so definitively.

        ``--keep`` only aborts for a file that *differs* between ``HEAD`` and
        the target; a write to a file the two commits agree on is kept, which is
        not destruction but is still somebody's work sitting in a slot about to
        be leased to somebody else. This is where it surfaces — and where a
        silent race becomes a ``dirty`` slot with a reason on it.
        """
        after = await self._dirty_count(path)
        if after is None:
            self._transition(name, "needs_review", detail=STATUS_UNREADABLE)
            return None
        if after > 0:
            log.warning("worktrees.acquire_raced_a_writer", slot=name, files=after, refused=False)
            self._transition(name, "dirty", detail=dirty_detail(after), dirty_files=after)
            return None
        log.debug("worktrees.slot_verified_clean", slot=name)
        return self._slots[name]

    async def _create_slot(self, repo: Path, base: str) -> str:
        """``git worktree add --detach``. The ``--detach`` is decision 1."""
        name = self._next_slot_name()
        path = self._root / name
        result = await self._git(
            repo,
            ("worktree", "add", "--detach", str(path), base),
            GIT_CHECKOUT_TIMEOUT_S,
        )
        if not result.ok:
            raise GitError(f"could not create {name}: {result.first_error_line()}")
        now = self._clock()
        self._slots[name] = WorktreeInfo(
            slot=name,
            path=str(path),
            state="free",
            head=base,
            created_at=now,
            updated_at=now,
        )
        log.info("worktrees.slot_created", slot=name, path=str(path), base=base[:12])
        return name

    def _lease(self, name: str, request: AcquireWorktreeRequest, base: str) -> WorktreeInfo:
        now = self._clock()
        ttl = request.ttl_seconds or self._lease_seconds
        lease = WorktreeLease(
            lease_id=uuid.uuid4().hex,
            holder=request.holder,
            owner_pid=request.owner_pid,
            acquired_at=now,
            expires_at=now + ttl,
        )
        info = self._slots[name].model_copy(
            update={
                "state": "leased",
                "lease": lease,
                "head": base,
                "dirty_files": 0,
                "detail": None,
                "updated_at": now,
            }
        )
        self._slots[name] = info
        self._save_state()
        self._publish(info)
        log.info(
            "worktrees.acquired",
            slot=name,
            holder=request.holder,
            owner_pid=request.owner_pid,
            base=base[:12],
            expires_in_s=round(ttl),
        )
        return info

    # ---- releasing and renewing ---------------------------------------------

    async def release(
        self, slot: str, lease_id: str, *, discard_changes: bool = False
    ) -> WorktreeInfo:
        """Give a slot back.

        A slot holding uncommitted work is parked as ``dirty`` rather than
        reset: the holder is finished with it, the work is not, and nothing
        about "I am done" is a licence to delete files. ``discard_changes`` is
        the caller saying otherwise, out loud.
        """
        async with self._lock:
            info = self._checked(slot, lease_id)
            path = Path(info.path)
            dirty = await self._dirty_count(path)
            if dirty is None:
                return self._transition(slot, "needs_review", detail=STATUS_UNREADABLE)
            if dirty > 0 and not discard_changes:
                log.info("worktrees.released_dirty", slot=slot, files=dirty)
                return self._transition(
                    slot, "dirty", detail=dirty_detail(dirty), dirty_files=dirty
                )
            if dirty > 0 and not await self._discard(path):
                return self._transition(
                    slot,
                    "needs_review",
                    detail=DISCARD_FAILED_DETAIL,
                    dirty_files=dirty,
                )
            head = await self._head(path)
            log.info("worktrees.released", slot=slot, discarded=dirty)
            return self._transition(slot, "free", head=head)

    async def renew(
        self, slot: str, lease_id: str, ttl_seconds: float | None = None
    ) -> WorktreeInfo:
        """Push the deadline out. A lease that cannot be renewed is a timeout."""
        async with self._lock:
            info = self._checked(slot, lease_id)
            lease = info.lease
            if lease is None:  # pragma: no cover - _checked guarantees it
                raise LeaseError(slot)
            now = self._clock()
            ttl = ttl_seconds or self._lease_seconds
            renewed = info.model_copy(
                update={
                    "lease": lease.model_copy(update={"expires_at": now + ttl}),
                    "updated_at": now,
                }
            )
            self._slots[slot] = renewed
            self._save_state()
            self._publish(renewed)
            return renewed

    # ---- pruning ------------------------------------------------------------

    async def prune(self, *, force: bool = False) -> PruneResult:
        """One reaper sweep. Both idle signals must agree before a slot moves.

        ``force`` is the only thing in this service that can destroy a file, and
        it applies to exactly the slots holding uncommitted work. Everything
        else it does, it would have done anyway.

        **A sweep re-asks about every slot that is not free**, including the
        ``dirty`` and ``needs_review`` ones, and that is what keeps a transient
        Windows lock from retiring a slot for good: an exclusive handle makes
        ``git status`` report a file it cannot open as *modified*, so a Defender
        scan or a dying interpreter can park a perfectly clean slot as ``dirty``.
        Re-checking discards nothing — a slot git now reports as clean has, by
        definition, nothing left to lose — so it is safe in exactly the way
        forcing is not.
        """
        async with self._lock:
            reclaimed: list[str] = []
            kept: list[KeptSlot] = []
            now = self._clock()
            for name in sorted(self._slots):
                info = self._slots[name]
                if info.state == "free":
                    continue
                if info.state == "leased":
                    held = self._still_held(info, now)
                    if held is not None:
                        kept.append(held)
                        continue
                outcome = await self._reclaim(name, force=force)
                if outcome is None:
                    reclaimed.append(name)
                else:
                    kept.append(outcome)
            if reclaimed:
                log.info("worktrees.pruned", reclaimed=reclaimed, kept=[k.slot for k in kept])
            return PruneResult(reclaimed=reclaimed, kept=kept, pool=self.snapshot())

    def _still_held(self, info: WorktreeInfo, now: float) -> KeptSlot | None:
        """Decision 3, in one place. ``None`` means both signals say idle."""
        lease = info.lease
        if lease is None:  # pragma: no cover - a leased slot always has one
            return None
        if now < lease.expires_at:
            remaining = round(lease.expires_at - now)
            reason = "held by a lease that has not expired"
            if lease.recovered:
                reason = "rebuilt from disk and held until verified"
            return KeptSlot(
                slot=info.slot,
                reason="leased",
                detail=f"{reason} ({remaining}s left, holder {lease.holder!r})",
            )
        if lease.owner_pid is not None and self._alive(lease.owner_pid):
            return KeptSlot(
                slot=info.slot,
                reason="owner_alive",
                detail=f"lease expired but pid {lease.owner_pid} is still running",
            )
        return None

    async def _reclaim(self, name: str, *, force: bool) -> KeptSlot | None:
        """Try to put one slot back. ``None`` on success, else why not."""
        info = self._slots[name]
        path = Path(info.path)
        dirty = await self._dirty_count(path)
        if dirty is None:
            self._transition(name, "needs_review", detail=STATUS_UNREADABLE)
            return KeptSlot(slot=name, reason="needs_review", detail="git could not read the slot")
        if dirty > 0:
            if not force:
                self._transition(
                    name,
                    "dirty",
                    detail=dirty_detail(dirty),
                    dirty_files=dirty,
                )
                return KeptSlot(slot=name, reason="dirty", detail=f"{dirty} uncommitted change(s)")
            if not await self._discard(path):
                self._transition(
                    name,
                    "needs_review",
                    detail=DISCARD_FAILED_DETAIL,
                    dirty_files=dirty,
                )
                return KeptSlot(
                    slot=name, reason="reset_failed", detail="something is holding a file open"
                )
        head = await self._head(path)
        self._transition(name, "free", head=head)
        return None

    # ---- git ----------------------------------------------------------------

    async def _discover_repo(self) -> Path:
        result = await self._git(
            self._workspace_root, ("rev-parse", "--show-toplevel"), GIT_TIMEOUT_S
        )
        if not result.ok or not result.out:
            raise GitError(f"{self._workspace_root} is not inside a git repository")
        # No `.resolve()`: git already prints an absolute, symlink-resolved path,
        # and resolving it here would be a blocking filesystem call in an async
        # function for no answer we do not already have.
        return normalized_path(result.out.splitlines()[0])

    async def _resolve_commit(self, ref: str) -> str:
        repo = self._require_repo()
        result = await self._git(
            repo, ("rev-parse", "--verify", f"{ref}^{{commit}}"), GIT_TIMEOUT_S
        )
        if not result.ok or not result.out:
            raise GitError(f"{ref} is not a commit in this repository")
        return result.out.splitlines()[0].strip()

    async def _dirty_count(self, path: Path) -> int | None:
        """How many paths ``git status`` reports, or ``None`` if it could not say.

        ``None`` is not zero and callers must not treat it as one — an
        unanswerable slot is one whose contents we cannot vouch for, and the
        whole point of this service is never to guess about a working tree.
        Ignored files are absent from ``--porcelain`` output, which is what lets
        a slot keep its dependency caches and still read as clean.
        """
        try:
            # A missing or unreadable directory comes back as a GitError from
            # the spawn itself, which is the same "cannot vouch for it" answer —
            # so there is no `is_dir()` pre-check to go stale (or to block).
            result = await self._git(
                path, ("--no-optional-locks", "status", "--porcelain"), GIT_TIMEOUT_S
            )
        except GitError as err:
            log.warning("worktrees.status_failed", path=str(path), detail=str(err))
            return None
        if not result.ok:
            log.warning("worktrees.status_failed", path=str(path), detail=result.first_error_line())
            return None
        return len([line for line in result.out.splitlines() if line.strip()])

    async def _head(self, path: Path) -> str | None:
        with contextlib.suppress(GitError):
            result = await self._git(path, ("rev-parse", "HEAD"), GIT_TIMEOUT_S)
            if result.ok and result.out:
                return result.out.splitlines()[0].strip()
        return None

    async def _reset_to(self, path: Path, commit: str, *, mode: ResetMode) -> bool:
        """``git reset`` past a transient Windows lock. See :data:`ResetMode`.

        See :data:`RESET_ATTEMPTS`. A lock that outlasts the budget is a real
        one, and the answer to a real one is ``needs_review`` — never a harder
        flag, and never a claim that the slot is clean.
        """
        for attempt in range(RESET_ATTEMPTS):
            try:
                result = await self._git(path, ("reset", mode, commit), GIT_TIMEOUT_S)
            except GitError as err:
                log.warning("worktrees.reset_error", path=str(path), detail=str(err))
                return False
            if result.ok:
                return True
            log.debug(
                "worktrees.reset_retry",
                path=str(path),
                attempt=attempt + 1,
                detail=result.first_error_line(),
            )
            if attempt < RESET_ATTEMPTS - 1:
                await asyncio.sleep(RESET_BACKOFF_S)
        return False

    async def _discard(self, path: Path) -> bool:
        """Reset tracked files and remove untracked ones — ``-fd``, never ``-fdx``.

        The missing ``-x`` is decision 2: ignored files are the dependency and
        build caches that make a warm slot worth pooling, and a "clean" that
        deletes ``node_modules`` turns every reuse back into a cold install.

        ``--hard`` here rather than the ``--keep`` :meth:`_prepare` uses, because
        this path is only ever reached through ``discard_changes`` or ``force``
        — the caller naming destruction out loud is what makes it allowed.

        Verified rather than assumed, all the same. This is the one call that
        *promises* an empty slot, and the clean behind the reset is a second
        process: a writer active across that gap leaves work behind, and a slot
        handed back as ``free`` with somebody's files in it is the same silent
        lie the acquire path guards against. So the promise is re-read from git
        before it is made.
        """
        if not await self._reset_to(path, "HEAD", mode="--hard"):
            return False
        try:
            result = await self._git(path, ("clean", "-fd"), GIT_TIMEOUT_S)
        except GitError as err:
            log.warning("worktrees.clean_error", path=str(path), detail=str(err))
            return False
        if not result.ok:
            return False
        left = await self._dirty_count(path)
        if left != 0:  # None (git would not say) and >0 (work arrived) both fail
            log.warning("worktrees.discard_left_work_behind", path=str(path), files=left)
            return False
        return True

    async def _registered_worktrees(self) -> dict[str, str]:
        """Slot name -> path, for the worktrees git knows about under our root."""
        repo = self._require_repo()
        try:
            result = await self._git(repo, ("worktree", "list", "--porcelain"), GIT_TIMEOUT_S)
        except GitError as err:
            log.warning("worktrees.list_failed", detail=str(err))
            return {}
        if not result.ok:
            return {}
        found: dict[str, str] = {}
        for line in result.out.splitlines():
            if not line.startswith("worktree "):
                continue
            path = normalized_path(line[len("worktree ") :])
            if same_dir(path.parent, self._root):
                found[path.name] = str(path)
        return found

    # ---- state --------------------------------------------------------------

    def _load_state(self) -> tuple[dict[str, WorktreeInfo], str | None]:
        """Read ``pool.json``. Anything unusable comes back as an empty pool
        plus the reason, and :meth:`_reconcile` then rebuilds from disk with
        every slot held — decision 4."""
        path = self._root / POOL_STATE_FILE
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}, None  # a pool that has never run is not a problem
        except OSError as err:
            return {}, self._recovering(f"{POOL_STATE_FILE} is unreadable: {err.strerror or err}")
        try:
            parsed = json.loads(raw)
        except ValueError as err:
            return {}, self._recovering(
                f"{POOL_STATE_FILE} is not valid JSON ({err.args[0] if err.args else err})"
            )
        try:
            state = WorktreePoolState.model_validate(parsed)
        except ValidationError:
            return {}, self._recovering(
                f"{POOL_STATE_FILE} is not a pool document this version reads"
            )
        if state.version != POOL_STATE_VERSION:
            return {}, self._recovering(
                f"{POOL_STATE_FILE} is version {state.version}, this Workbench reads "
                f"{POOL_STATE_VERSION}"
            )
        return {slot.slot: slot for slot in state.slots}, None

    def _recovering(self, reason: str) -> str:
        log.warning("worktrees.state_unusable", root=str(self._root), reason=reason)
        return f"{reason} — the pool was rebuilt from disk and every slot is held until verified"

    async def _reconcile(self, known: dict[str, WorktreeInfo]) -> dict[str, WorktreeInfo]:
        """The disk is the truth; the state file is a memory of it.

        Three cases, and the second is decision 4: a directory git knows as a
        worktree *and* the state file knows is believed; one only git knows is
        adopted **leased**, because something may be working in it right now and
        the pool has no way to ask; one git does not know is ``needs_review``,
        because a half-created slot is not a slot — and it is left on disk,
        because this service does not delete directories.
        """
        registered = await self._registered_worktrees()
        try:
            on_disk = sorted(p.name for p in self._root.iterdir() if p.is_dir())
        except OSError as err:  # pragma: no cover - the root was just created
            log.warning("worktrees.root_unreadable", root=str(self._root), detail=str(err))
            return {}
        now = self._clock()
        slots: dict[str, WorktreeInfo] = {}
        for name in on_disk:
            path = self._root / name
            if name not in registered:
                slots[name] = self._orphan(name, path, now)
                continue
            remembered = known.get(name)
            if remembered is not None:
                slots[name] = remembered.model_copy(update={"path": str(path)})
                continue
            slots[name] = self._recovered(name, path, now)
        for name in known:
            if name not in slots:
                # The directory is gone. Nothing to protect, nothing to delete.
                log.info("worktrees.slot_vanished", slot=name)
        return slots

    def _recovered(self, name: str, path: Path, now: float) -> WorktreeInfo:
        """Assume in use. Never assume free."""
        log.warning("worktrees.slot_recovered", slot=name, path=str(path))
        return WorktreeInfo(
            slot=name,
            path=str(path),
            state="leased",
            lease=WorktreeLease(
                lease_id=uuid.uuid4().hex,
                holder="recovered",
                owner_pid=None,
                acquired_at=now,
                expires_at=now + self._lease_seconds,
                recovered=True,
            ),
            created_at=now,
            updated_at=now,
            detail="rebuilt from disk after the pool state was lost; held until verified",
        )

    def _orphan(self, name: str, path: Path, now: float) -> WorktreeInfo:
        return WorktreeInfo(
            slot=name,
            path=str(path),
            state="needs_review",
            created_at=now,
            updated_at=now,
            detail="a directory in the pool root that git does not know as a worktree",
        )

    def _save_state(self) -> None:
        """Atomic, and past the same transient Windows lock ``layouts.py`` hits.

        A write that will not land is logged and swallowed: the pool in memory
        is still correct, and failing an acquire because a *memo* of it could
        not be saved would trade a working feature for a tidy file.
        """
        state = WorktreePoolState(slots=[self._slots[name] for name in sorted(self._slots)])
        target = self._root / POOL_STATE_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{POOL_STATE_FILE}.", suffix=".tmp"
            )
        except OSError as err:
            log.warning("worktrees.state_write_failed", detail=str(err))
            return
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(state.model_dump_json().encode("utf-8"))
            self._replace(tmp_name, target)
        except OSError as err:
            Path(tmp_name).unlink(missing_ok=True)
            log.warning("worktrees.state_write_failed", path=str(target), detail=str(err))
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _replace(tmp_name: str, target: Path) -> None:
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_name, target)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_BACKOFF_S)

    # ---- internals ----------------------------------------------------------

    def _require_repo(self) -> Path:
        if self._repo is None:
            raise PoolUnavailableError(self._problem or "the worktree pool is not available")
        return self._repo

    def _checked(self, slot: str, lease_id: str) -> WorktreeInfo:
        info = self._slots.get(slot)
        if info is None:
            raise SlotNotFoundError(slot)
        if info.lease is None or info.lease.lease_id != lease_id:
            raise LeaseError(slot)
        return info

    def _transition(
        self,
        slot: str,
        state: WorktreeState,
        *,
        detail: str | None = None,
        head: str | None = None,
        dirty_files: int = 0,
    ) -> WorktreeInfo:
        """Move a slot, persist, publish. The lease is dropped by construction:
        every state but ``leased`` is reached by giving one up.

        **A transition that changes nothing publishes nothing** — the same rule
        ``office_host``'s close-failure flag follows. Every prune re-asks about
        every non-free slot, so a pool with one parked ``dirty`` slot would
        otherwise put a frame on the shared bus for each sweep saying it is
        still dirty. Observed on the live socket before this guard existed.
        """
        current = self._slots[slot]
        info = current.model_copy(
            update={
                "state": state,
                "lease": None,
                "detail": detail,
                "dirty_files": dirty_files,
                "head": head if head is not None else current.head,
                "updated_at": self._clock(),
            }
        )
        if info.model_dump(exclude={"updated_at"}) == current.model_dump(exclude={"updated_at"}):
            return current
        self._slots[slot] = info
        self._save_state()
        self._publish(info)
        return info

    def _next_slot_name(self) -> str:
        for index in range(1, MAX_POOL_SIZE + 1):
            name = f"slot-{index:02d}"
            if name not in self._slots and not (self._root / name).exists():
                return name
        raise PoolExhaustedError("no free slot name is available")  # pragma: no cover

    def _exhausted_detail(self) -> str:
        counts: dict[str, int] = {}
        for info in self._slots.values():
            counts[info.state] = counts.get(info.state, 0) + 1
        summary = ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))
        return (
            f"all {self._capacity} slots are in use ({summary}) — release one, "
            "prune the pool, or raise WORKBENCH_WORKTREE_POOL_SIZE"
        )

    def _publish(self, info: WorktreeInfo) -> None:
        self._bus.publish(WorktreeChangedEvent(worktree=info))
