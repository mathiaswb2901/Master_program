"""The managed worktree pool, driven against a **real** git repository.

Nothing here fakes git. Every test below builds an actual repository in
``tmp_path``, runs the real ``git worktree add``, and asserts on what is on
disk — because every interesting failure in this service is a filesystem
failure, and a mocked ``git`` would pass while the product broke.

The themes, in the order the pool meets them:

* a slot is **detached**, so two lanes can never collide on a branch;
* a slot is **reused**, not recreated, so a warm ``node_modules`` survives;
* **dirty is sacred** — a slot with uncommitted work is never handed out and
  never reclaimed without an explicit override;
* **two idle signals** — an expired lease alone does not reclaim a slot while
  its owner process is alive, and a live owner alone does not hold one past a
  lease nobody renewed;
* **corrupt state fails safe** — a lost ``pool.json`` comes back with every slot
  held, never free;
* the pool root is **outside the workspace**, so the tree and the watcher never
  see it.

The Windows file-lock path gets two tests on purpose. One takes a *real*
exclusive handle on a file in a slot and watches the reset fail — the
reproduction — and the other injects a transient failure to pin the retry
arithmetic, which a real lock cannot do deterministically.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.worktrees import (
    AcquireWorktreeRequest,
    WorktreeInfo,
    WorktreePool,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.workspace import PathOutsideWorkspaceError, Workspace
from workbench_server.services.worktrees import (
    POOL_STATE_FILE,
    RESET_ATTEMPTS,
    AliveProbe,
    Clock,
    GitResult,
    GitRunner,
    LeaseError,
    PoolExhaustedError,
    PoolUnavailableError,
    SlotNotFoundError,
    WorktreeService,
    app_data_dir,
    default_pool_root,
    dirty_detail,
    process_alive,
    run_git,
)

pytestmark = pytest.mark.timeout(180)


# ---- a real repository ------------------------------------------------------


def _try_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Git, allowed to fail. Sync on purpose: the async rules ruff enforces are
    about production code, and a fixture that shells out is the point here."""
    return subprocess.run(  # noqa: S603 - the arguments are this file's own literals
        ["git", *args],  # noqa: S607 - git is on PATH wherever this suite runs
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(cwd: Path, *args: str) -> str:
    """Synchronous git, for building fixtures. The service's own git is async."""
    done = _try_git(cwd, *args)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr or done.stdout}"
    return done.stdout.strip()


def _is_dir(path: str) -> bool:
    """A sync wrapper so an async test may ask the filesystem a question."""
    return Path(path).is_dir()


def _relative_to(path: str, root: Path) -> str:
    return os.path.relpath(path, root)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with two commits, so a reset has something to do."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@workbench.invalid")
    _git(root, "config", "user.name", "Workbench Test")
    # Per-repo, because a developer machine may well have core.autocrlf=true
    # globally (this one does) and every byte assertion below would then compare
    # a CRLF checkout against an LF literal. The pool does not care either way;
    # the fixture does.
    _git(root, "config", "core.autocrlf", "false")
    (root / "model.py").write_bytes(b"VERSION = 1\n")
    (root / ".gitignore").write_bytes(b"node_modules/\n")
    _commit(root, "first")
    return root


@pytest.fixture
def pool_root(tmp_path: Path) -> Path:
    """Stands in for the app data dir: outside the workspace, like the real one."""
    return tmp_path / "pool"


async def _started(
    repo: Path,
    pool_root: Path,
    *,
    capacity: int = 4,
    lease_seconds: float = 3600.0,
    runner: GitRunner = run_git,
    clock: Clock = time.time,
    alive: AliveProbe = process_alive,
) -> WorktreeService:
    service = WorktreeService(
        repo,
        EventBus(),
        pool_root=pool_root,
        capacity=capacity,
        lease_seconds=lease_seconds,
        runner=runner,
        clock=clock,
        alive=alive,
    )
    await service.start()
    return service


def _acquire(
    holder: str = "lane-a",
    *,
    base: str | None = None,
    owner_pid: int | None = None,
    ttl_seconds: float | None = None,
) -> AcquireWorktreeRequest:
    return AcquireWorktreeRequest(
        holder=holder, base=base, owner_pid=owner_pid, ttl_seconds=ttl_seconds
    )


# ---- decision 1: detached HEAD ----------------------------------------------


async def test_a_pooled_slot_carries_no_branch(repo: Path, pool_root: Path) -> None:
    """Decision 1, and the reason for it: no branch means no collision.

    The proof is the one that matters in practice — the *main* checkout is on
    ``main``, and a slot checked out at the same commit does not make git say
    "already checked out in another worktree".
    """
    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire())
    slot = Path(info.path)

    assert _git(slot, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached
    assert _git(slot, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    # The collision that decision 1 exists to prevent: `git checkout main` in
    # the slot is exactly what a branch-carrying pool would have done for us.
    refused = _try_git(slot, "checkout", "main")
    assert refused.returncode != 0
    assert "already" in (refused.stderr + refused.stdout).lower()


async def test_a_second_slot_sits_on_the_same_commit(repo: Path, pool_root: Path) -> None:
    """Two lanes, one commit, no argument — the whole point of the pool."""
    service = await _started(repo, pool_root)
    first = await service.acquire(_acquire("lane-a"))
    second = await service.acquire(_acquire("lane-b"))

    assert first.slot != second.slot
    assert first.head == second.head
    assert _is_dir(first.path) and _is_dir(second.path)


async def test_acquire_can_name_any_commit(repo: Path, pool_root: Path) -> None:
    (repo / "model.py").write_bytes(b"VERSION = 2\n")
    second = _commit(repo, "second")
    first = _git(repo, "rev-parse", "HEAD~1")

    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire(base=first))

    assert info.head == first != second
    assert (Path(info.path) / "model.py").read_bytes() == b"VERSION = 1\n"


# ---- decision 2: pool, never destroy ----------------------------------------


async def test_a_released_slot_is_reused_rather_than_recreated(repo: Path, pool_root: Path) -> None:
    """The pool's whole economic argument, asserted as an identity."""
    service = await _started(repo, pool_root)
    first = await service.acquire(_acquire())
    assert first.lease is not None
    await service.release(first.slot, first.lease.lease_id)

    second = await service.acquire(_acquire("lane-b"))
    assert second.slot == first.slot
    assert second.path == first.path
    assert len(service.snapshot().slots) == 1


async def test_reuse_keeps_the_dependency_cache_that_makes_it_worth_pooling(
    repo: Path, pool_root: Path
) -> None:
    """``node_modules`` is *ignored*, so ``git clean -fd`` leaves it alone.

    This is the difference between paying a cold install once per slot and once
    per task, and it is the reason the pool never removes a worktree.
    """
    service = await _started(repo, pool_root)
    first = await service.acquire(_acquire())
    assert first.lease is not None
    warm = Path(first.path) / "node_modules" / "left-pad"
    warm.mkdir(parents=True)
    (warm / "index.js").write_bytes(b"module.exports = 1\n")
    # An ignored file does not make a slot dirty, which is what lets it be
    # released clean and handed straight back out.
    await service.release(first.slot, first.lease.lease_id)
    assert service.snapshot().slots[0].state == "free"

    second = await service.acquire(_acquire("lane-b", base="HEAD"))
    assert second.slot == first.slot
    assert (warm / "index.js").read_bytes() == b"module.exports = 1\n"


async def test_the_service_never_removes_a_worktree(repo: Path, pool_root: Path) -> None:
    """Decision 2, enforced on the commands rather than on the prose.

    ``git worktree remove`` recurses through a Windows junction, so a design
    that links dependencies into a slot can empty the checkout they point at.
    The defence is that this service has no removal path at all — asserted by
    watching every git command a full acquire/release/discard/prune cycle runs.
    """
    seen: list[tuple[str, ...]] = []

    async def spy(cwd: Path, args: Sequence[str], timeout_s: float) -> GitResult:
        seen.append(tuple(args))
        return await run_git(cwd, args, timeout_s)

    service = await _started(repo, pool_root, runner=spy)
    info = await service.acquire(_acquire())
    assert info.lease is not None
    (Path(info.path) / "scratch.txt").write_bytes(b"work\n")
    await service.release(info.slot, info.lease.lease_id, discard_changes=True)
    await service.prune(force=True)

    assert seen, "the spy saw no git at all — the test proves nothing"
    for args in seen:
        assert args[:2] != ("worktree", "remove"), args
        if args[0] == "clean":
            # -x would delete the ignored dependency caches the pool exists to keep.
            assert "x" not in args[1].lstrip("-"), args
        assert "--force" not in args, args


# ---- dirty is sacred --------------------------------------------------------


async def test_a_dirty_slot_is_parked_not_reset(repo: Path, pool_root: Path) -> None:
    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire())
    assert info.lease is not None
    (Path(info.path) / "model.py").write_bytes(b"VERSION = 99\n")
    (Path(info.path) / "notes.md").write_bytes(b"half an idea\n")

    released = await service.release(info.slot, info.lease.lease_id)

    assert released.state == "dirty"
    assert released.dirty_files == 2
    assert released.lease is None
    assert released.detail == dirty_detail(2)
    # The work is still there. That is the assertion this whole file exists for.
    assert (Path(info.path) / "model.py").read_bytes() == b"VERSION = 99\n"
    assert (Path(info.path) / "notes.md").is_file()


async def test_a_dirty_slot_is_never_handed_out(repo: Path, pool_root: Path) -> None:
    service = await _started(repo, pool_root, capacity=2)
    first = await service.acquire(_acquire())
    assert first.lease is not None
    (Path(first.path) / "model.py").write_bytes(b"VERSION = 99\n")
    await service.release(first.slot, first.lease.lease_id)

    second = await service.acquire(_acquire("lane-b"))

    assert second.slot != first.slot
    assert (Path(first.path) / "model.py").read_bytes() == b"VERSION = 99\n"


async def test_a_dirty_slot_survives_a_prune(repo: Path, pool_root: Path) -> None:
    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire())
    assert info.lease is not None
    (Path(info.path) / "model.py").write_bytes(b"VERSION = 99\n")
    await service.release(info.slot, info.lease.lease_id)

    result = await service.prune()

    assert result.reclaimed == []
    assert [kept.reason for kept in result.kept] == ["dirty"]
    assert (Path(info.path) / "model.py").read_bytes() == b"VERSION = 99\n"


async def test_a_dirty_slot_that_became_clean_is_re_checked_and_freed(
    repo: Path, pool_root: Path
) -> None:
    """The other half of dirty protection: it must not be a one-way door.

    A slot parked as dirty is re-asked on every sweep, and a slot git now
    reports as clean is freed — which discards nothing, because there is
    nothing left to discard. Without this, a transient Windows lock (which
    makes ``git status`` call a file it cannot open *modified*) would retire a
    perfectly good slot for the life of the server.
    """
    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire())
    assert info.lease is not None
    (Path(info.path) / "model.py").write_bytes(b"VERSION = 99\n")
    await service.release(info.slot, info.lease.lease_id)
    assert service.snapshot().slots[0].state == "dirty"

    # The holder came back and committed their work — the ordinary happy ending.
    _git(Path(info.path), "add", "-A")
    _git(Path(info.path), "-c", "commit.gpgsign=false", "commit", "-m", "rescued")

    result = await service.prune()

    assert result.reclaimed == [info.slot]
    assert service.snapshot().slots[0].state == "free"


async def test_force_is_what_discards_and_only_force(repo: Path, pool_root: Path) -> None:
    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire())
    assert info.lease is not None
    (Path(info.path) / "model.py").write_bytes(b"VERSION = 99\n")
    (Path(info.path) / "untracked.txt").write_bytes(b"gone\n")
    await service.release(info.slot, info.lease.lease_id)

    result = await service.prune(force=True)

    assert result.reclaimed == [info.slot]
    assert (Path(info.path) / "model.py").read_bytes() == b"VERSION = 1\n"
    assert not (Path(info.path) / "untracked.txt").exists()
    assert result.pool.slots[0].state == "free"


async def test_a_slot_recorded_free_but_dirty_on_disk_loses_the_argument(
    repo: Path, pool_root: Path
) -> None:
    """The disk wins over the memo of the disk, always.

    A crash between "reset" and "save state" leaves exactly this: a slot the
    file calls free with somebody's work in it. Handing it out would be the
    unrecoverable failure.
    """
    service = await _started(repo, pool_root, capacity=2)
    first = await service.acquire(_acquire())
    assert first.lease is not None
    await service.release(first.slot, first.lease.lease_id)
    assert service.snapshot().slots[0].state == "free"
    (Path(first.path) / "rescued.txt").write_bytes(b"do not lose me\n")

    second = await service.acquire(_acquire("lane-b"))

    assert second.slot != first.slot
    states = {slot.slot: slot.state for slot in service.snapshot().slots}
    assert states[first.slot] == "dirty"
    assert (Path(first.path) / "rescued.txt").is_file()


# ---- decision 3: two idle signals -------------------------------------------


class _Clock:
    """A hand-wound clock, so lease expiry is tested rather than waited for."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def test_an_unexpired_lease_holds_a_slot_with_nothing_running(
    repo: Path, pool_root: Path
) -> None:
    """Signal two on its own: the agent works unattended and keeps its slot."""
    clock = _Clock()
    service = await _started(
        repo, pool_root, clock=clock, alive=lambda _pid: False, lease_seconds=600.0
    )
    info = await service.acquire(_acquire(owner_pid=999_999))
    clock.now += 300.0

    result = await service.prune()

    assert result.reclaimed == []
    assert [kept.reason for kept in result.kept] == ["leased"]
    assert service.snapshot().slots[0].state == "leased"
    assert info.lease is not None


async def test_a_live_owner_holds_a_slot_past_an_expired_lease(repo: Path, pool_root: Path) -> None:
    """Signal one on its own: the deadline lapsed, the worker did not."""
    clock = _Clock()
    service = await _started(
        repo, pool_root, clock=clock, alive=lambda _pid: True, lease_seconds=600.0
    )
    await service.acquire(_acquire(owner_pid=4242))
    clock.now += 10_000.0

    result = await service.prune()

    assert result.reclaimed == []
    assert [kept.reason for kept in result.kept] == ["owner_alive"]
    assert result.kept[0].detail is not None and "4242" in result.kept[0].detail


async def test_both_signals_idle_is_what_reclaims_a_slot(repo: Path, pool_root: Path) -> None:
    clock = _Clock()
    service = await _started(
        repo, pool_root, clock=clock, alive=lambda _pid: False, lease_seconds=600.0
    )
    info = await service.acquire(_acquire(owner_pid=4242))
    clock.now += 10_000.0

    result = await service.prune()

    assert result.reclaimed == [info.slot]
    assert service.snapshot().slots[0].state == "free"


async def test_renewing_pushes_the_deadline_out(repo: Path, pool_root: Path) -> None:
    """A lease that cannot be renewed is a timeout, not a lease."""
    clock = _Clock()
    service = await _started(
        repo, pool_root, clock=clock, alive=lambda _pid: False, lease_seconds=600.0
    )
    info = await service.acquire(_acquire())
    assert info.lease is not None
    clock.now += 500.0
    renewed = await service.renew(info.slot, info.lease.lease_id)
    clock.now += 500.0  # past the original deadline, inside the renewed one

    result = await service.prune()

    assert renewed.lease is not None
    assert renewed.lease.expires_at > info.lease.expires_at
    assert result.reclaimed == []


async def test_a_stale_lease_cannot_release_or_renew_a_slot(repo: Path, pool_root: Path) -> None:
    """A zombie owner must not be able to take a slot from whoever holds it now."""
    clock = _Clock()
    service = await _started(repo, pool_root, clock=clock, alive=lambda _pid: False)
    first = await service.acquire(_acquire("lane-a"))
    assert first.lease is not None
    stale = first.lease.lease_id
    clock.now += 100_000.0
    await service.prune()
    second = await service.acquire(_acquire("lane-b"))
    assert second.lease is not None and second.lease.lease_id != stale

    with pytest.raises(LeaseError):
        await service.release(second.slot, stale)
    with pytest.raises(LeaseError):
        await service.renew(second.slot, stale)
    assert service.snapshot().slots[0].state == "leased"


# ---- decision 4: fail safe on corrupt state ---------------------------------


@pytest.mark.parametrize(
    "corruption",
    ['{"version": 1, "slots": [{"slot": "slot-01"', "", "not json", '{"version": 99, "slots": []}'],
    ids=["truncated", "empty", "garbage", "future-version"],
)
async def test_unusable_state_comes_back_with_every_slot_held(
    repo: Path, pool_root: Path, corruption: str
) -> None:
    """Assume in use. Never assume free."""
    first = await _started(repo, pool_root)
    info = await first.acquire(_acquire())
    assert info.lease is not None
    await first.release(info.slot, info.lease.lease_id)
    assert first.snapshot().slots[0].state == "free"

    (pool_root / POOL_STATE_FILE).write_text(corruption, encoding="utf-8")
    rebuilt = await _started(repo, pool_root)
    pool = rebuilt.snapshot()

    assert pool.problem is not None and "held until verified" in pool.problem
    assert [slot.state for slot in pool.slots] == ["leased"]
    recovered = pool.slots[0].lease
    assert recovered is not None and recovered.recovered is True
    assert recovered.holder == "recovered"


async def test_a_recovered_slot_is_not_handed_out(repo: Path, pool_root: Path) -> None:
    """The whole point: a rebuild must not give an agent's checkout away."""
    first = await _started(repo, pool_root, capacity=1)
    info = await first.acquire(_acquire())
    assert info.lease is not None
    await first.release(info.slot, info.lease.lease_id)
    (pool_root / POOL_STATE_FILE).unlink()

    rebuilt = await _started(repo, pool_root, capacity=1)

    with pytest.raises(PoolExhaustedError):
        await rebuilt.acquire(_acquire("lane-b"))


async def test_a_recovered_slot_is_released_by_the_reaper_once_it_verifies(
    repo: Path, pool_root: Path
) -> None:
    """Held until verified, not held forever — the lease still has a deadline."""
    clock = _Clock()
    first = await _started(repo, pool_root, clock=clock)
    info = await first.acquire(_acquire())
    assert info.lease is not None
    await first.release(info.slot, info.lease.lease_id)
    (pool_root / POOL_STATE_FILE).unlink()

    rebuilt = await _started(
        repo, pool_root, clock=clock, alive=lambda _pid: False, lease_seconds=600.0
    )
    held = await rebuilt.prune()
    clock.now += 10_000.0
    freed = await rebuilt.prune()

    assert held.reclaimed == []
    assert held.kept[0].detail is not None and "held until verified" in held.kept[0].detail
    assert freed.reclaimed == [info.slot]


async def test_a_good_state_file_is_believed(repo: Path, pool_root: Path) -> None:
    """The negative control: recovery must not fire on a healthy restart."""
    first = await _started(repo, pool_root)
    info = await first.acquire(_acquire("lane-a", owner_pid=4242))
    assert info.lease is not None

    second = await _started(repo, pool_root)
    pool = second.snapshot()

    assert pool.problem is None
    assert [slot.state for slot in pool.slots] == ["leased"]
    restored = pool.slots[0].lease
    assert restored is not None
    assert restored.lease_id == info.lease.lease_id
    assert restored.holder == "lane-a"
    assert restored.owner_pid == 4242
    assert restored.recovered is False


async def test_a_directory_git_does_not_know_is_flagged_never_deleted(
    repo: Path, pool_root: Path
) -> None:
    """A half-created slot is not a slot — and it is still somebody's bytes."""
    await _started(repo, pool_root)
    stray = pool_root / "slot-99"
    stray.mkdir()
    (stray / "something.txt").write_bytes(b"who put this here\n")

    rebuilt = await _started(repo, pool_root)
    states = {slot.slot: slot for slot in rebuilt.snapshot().slots}

    assert states["slot-99"].state == "needs_review"
    assert states["slot-99"].detail is not None
    assert (stray / "something.txt").is_file()


# ---- Windows file locking ---------------------------------------------------


def _lock_exclusively(path: Path) -> "object":
    """A genuinely exclusive Win32 handle: share mode 0, nobody else may open it.

    Python's own ``open()`` will not do — CPython opens with all share flags set,
    so a file a Python process is reading is still perfectly writable by git.
    This is what a build tool, an editor or a dying interpreter actually holds.
    """
    import win32con
    import win32file

    return win32file.CreateFile(
        str(path), win32con.GENERIC_READ, 0, None, win32con.OPEN_EXISTING, 0, None
    )


@pytest.mark.skipif(sys.platform != "win32", reason="an exclusive share mode is a Windows thing")
async def test_a_real_exclusive_handle_is_caught_by_the_dirty_guard_first(
    repo: Path, pool_root: Path
) -> None:
    """The reproduction, and a measured fact worth writing down.

    A live process holding a file open is the failure this pool meets in the
    field. Take a genuinely exclusive handle on a file in a free slot and two
    things happen, in this order, **both protective**:

    1. ``git status --porcelain`` reports the file as ``M`` even though its bytes
       never changed — git could not open it to compare, so it says "changed".
       The pool's dirty guard therefore fires *before* any reset is attempted,
       which is the safest possible place for it to fire.
    2. ``git reset --hard`` against that file fails outright
       (``unable to unlink old 'model.py'``) — asserted directly here so the
       causal chain is documented rather than assumed.

    Neither costs a byte, and the slot heals by itself once the handle is gone.
    """
    (repo / "model.py").write_bytes(b"VERSION = 2\n")
    second = _commit(repo, "second")
    first_commit = _git(repo, "rev-parse", "HEAD~1")

    service = await _started(repo, pool_root, capacity=1, alive=lambda _pid: False)
    info = await service.acquire(_acquire(base=first_commit))
    assert info.lease is not None
    await service.release(info.slot, info.lease.lease_id)
    assert service.snapshot().slots[0].state == "free"

    handle = _lock_exclusively(Path(info.path) / "model.py")
    try:
        # (2), stated as the mechanism rather than inferred from the outcome.
        refused = _try_git(Path(info.path), "reset", "--hard", second)
        assert refused.returncode != 0
        assert "unable to unlink" in (refused.stderr + refused.stdout)

        # (1): the pool will not hand the slot out, and at capacity 1 that is
        # the whole pool — so the caller is told, rather than given a checkout
        # somebody else is holding files open in.
        with pytest.raises(PoolExhaustedError):
            await service.acquire(_acquire("lane-b", base=second))
        held = service.snapshot().slots[0]
        assert held.state == "dirty"
    finally:
        handle.Close()  # type: ignore[attr-defined]

    # Only now can anything read it — including this test, which is the clearest
    # possible statement of what an exclusive handle means.
    assert (Path(info.path) / "model.py").read_bytes() == b"VERSION = 1\n"
    # The lock is gone. The pool puts the slot back by itself — no --force, no
    # human, and nothing was destroyed on the way.
    healed = await service.prune()
    assert healed.reclaimed == [info.slot]
    assert service.snapshot().slots[0].state == "free"


@pytest.mark.skipif(sys.platform != "win32", reason="an exclusive share mode is a Windows thing")
async def test_a_real_lock_under_force_is_needs_review_never_a_lie(
    repo: Path, pool_root: Path
) -> None:
    """``force`` past a real lock reaches the reset, and the reset really fails.

    This is the honest fallback the task named: the pool does not escalate to a
    harder flag, it records what happened and stops handing the slot out.
    """
    service = await _started(repo, pool_root, capacity=1, alive=lambda _pid: False)
    info = await service.acquire(_acquire())
    assert info.lease is not None
    (Path(info.path) / "model.py").write_bytes(b"VERSION = 99\n")
    await service.release(info.slot, info.lease.lease_id)
    assert service.snapshot().slots[0].state == "dirty"

    handle = _lock_exclusively(Path(info.path) / "model.py")
    try:
        result = await service.prune(force=True)
    finally:
        handle.Close()  # type: ignore[attr-defined]

    assert result.reclaimed == []
    assert [kept.reason for kept in result.kept] == ["reset_failed"]
    held = service.snapshot().slots[0]
    assert held.state == "needs_review"
    assert held.detail is not None and str(RESET_ATTEMPTS) in held.detail
    # And the bytes the reset could not touch are still the bytes on disk.
    assert (Path(info.path) / "model.py").read_bytes() == b"VERSION = 99\n"


async def test_a_transient_lock_is_retried_rather_than_failed(repo: Path, pool_root: Path) -> None:
    """The retry arithmetic, pinned. A real lock cannot be released on a schedule.

    The first two resets fail exactly as Windows fails them; the third is let
    through, and the acquire that would otherwise have lost a warm slot to a
    Defender scan simply takes a moment longer.
    """
    failures = 2
    attempts = 0

    async def flaky(cwd: Path, args: Sequence[str], timeout_s: float) -> GitResult:
        nonlocal attempts
        if tuple(args[:2]) == ("reset", "--hard"):
            attempts += 1
            if attempts <= failures:
                return GitResult(1, "", "error: unable to unlink old 'model.py': Permission denied")
        return await run_git(cwd, args, timeout_s)

    service = await _started(repo, pool_root, runner=flaky)
    first = await service.acquire(_acquire())
    assert first.lease is not None
    await service.release(first.slot, first.lease.lease_id)

    second = await service.acquire(_acquire("lane-b"))

    assert attempts == failures + 1
    assert second.slot == first.slot
    assert second.state == "leased"


async def test_a_lock_that_outlasts_the_budget_is_reported_honestly(
    repo: Path, pool_root: Path
) -> None:
    """No ``--force``, no pretending. The slot says what happened to it."""

    async def stuck(cwd: Path, args: Sequence[str], timeout_s: float) -> GitResult:
        if tuple(args[:2]) == ("reset", "--hard"):
            return GitResult(1, "", "error: unable to unlink old 'model.py': Permission denied")
        return await run_git(cwd, args, timeout_s)

    service = await _started(repo, pool_root, capacity=1, runner=stuck)
    first = await service.acquire(_acquire())
    assert first.lease is not None
    await service.release(first.slot, first.lease.lease_id)

    with pytest.raises(PoolExhaustedError):
        await service.acquire(_acquire("lane-b"))

    held = service.snapshot().slots[0]
    assert held.state == "needs_review"
    assert held.detail is not None and "holding a file open" in held.detail


async def test_a_status_that_cannot_be_read_is_never_read_as_clean(
    repo: Path, pool_root: Path
) -> None:
    """The fail-safe direction, stated as a test so it cannot be edited away."""

    async def mute(cwd: Path, args: Sequence[str], timeout_s: float) -> GitResult:
        if "status" in args:
            return GitResult(128, "", "fatal: not a git repository")
        return await run_git(cwd, args, timeout_s)

    service = await _started(repo, pool_root, capacity=1, runner=mute)
    info = await service.acquire(_acquire())
    assert info.lease is not None

    released = await service.release(info.slot, info.lease.lease_id)

    assert released.state == "needs_review"
    with pytest.raises(PoolExhaustedError):
        await service.acquire(_acquire("lane-b"))


# ---- the pool root is not the workspace -------------------------------------


def test_the_default_pool_root_is_outside_any_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    root = default_pool_root(workspace)

    assert workspace not in root.parents and root != workspace
    assert app_data_dir() in root.parents
    # Two spellings of one Windows path must not get two pools.
    assert default_pool_root(Path(str(workspace).upper())) == root or sys.platform != "win32"


async def test_the_workspace_jail_refuses_the_pool_root(repo: Path, pool_root: Path) -> None:
    """``safe_path`` is the jail; a slot is simply not reachable through it."""
    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire())
    workspace = Workspace(repo)

    with pytest.raises(PathOutsideWorkspaceError):
        workspace.safe_path(_relative_to(info.path, repo))


async def test_slots_do_not_appear_in_the_file_tree(repo: Path, pool_root: Path) -> None:
    service = await _started(repo, pool_root)
    info = await service.acquire(_acquire())
    names = {child.name for child in Workspace(repo).tree().children or []}

    assert info.slot not in names
    assert "pool" not in names
    assert "model.py" in names  # the control: the tree does work


@pytest.mark.timeout(120)
def test_the_watcher_never_sees_a_checkout_land_in_a_slot(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree inside the workspace would arrive as a storm of file events.

    Drives the real app: acquire a slot through the API (which runs a real
    ``git worktree add``, writing every file in the repository into it), then
    write one file in the workspace and assert that the **first file event on
    the socket** is that one. A file event from the slot would arrive before it,
    since the watcher preserves order.

    The pool's own ``worktree_changed`` frames ride the same socket and are
    skipped: they are the pool reporting itself, not the watcher seeing a slot.
    """
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    settings = Settings(workspace_root=repo, worktree_root=tmp_path / "pool")
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        acquired = client.post("/api/worktrees/acquire", json={"holder": "watcher-test"})
        assert acquired.status_code == 200, acquired.text
        assert acquired.json()["state"] == "leased"
        (repo / "after.py").write_bytes(b"VERSION = 3\n")

        event = json.loads(ws.receive_text())
        while event["type"] == "worktree_changed":
            event = json.loads(ws.receive_text())
        assert event["type"] == "file_changed"
        assert event["path"] == "after.py"


# ---- the API ----------------------------------------------------------------


async def test_the_endpoints_round_trip_a_slot(repo: Path, tmp_path: Path) -> None:
    """Every endpoint, over the real app, including the two refusals a caller
    has to be able to tell apart: a lease that does not hold the slot (409) and
    a slot that does not exist (404)."""
    settings = Settings(workspace_root=repo, worktree_root=tmp_path / "pool")
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        empty = await client.get("/api/worktrees")
        assert empty.status_code == 200
        pool = WorktreePool.model_validate(empty.json())
        assert pool.repo is not None and pool.problem is None and pool.slots == []

        acquired = await client.post(
            "/api/worktrees/acquire", json={"holder": "lane-a", "ttl_seconds": 60}
        )
        assert acquired.status_code == 200, acquired.text
        info = WorktreeInfo.model_validate(acquired.json())
        assert info.state == "leased" and info.lease is not None

        renewed = await client.post(
            f"/api/worktrees/{info.slot}/renew", json={"lease_id": info.lease.lease_id}
        )
        assert renewed.status_code == 200

        wrong = await client.post(
            f"/api/worktrees/{info.slot}/release", json={"lease_id": "not-the-lease"}
        )
        assert wrong.status_code == 409

        missing = await client.post(
            "/api/worktrees/slot-99/release", json={"lease_id": info.lease.lease_id}
        )
        assert missing.status_code == 404

        released = await client.post(
            f"/api/worktrees/{info.slot}/release", json={"lease_id": info.lease.lease_id}
        )
        assert released.status_code == 200
        assert WorktreeInfo.model_validate(released.json()).state == "free"

        pruned = await client.post("/api/worktrees/prune", json={"force": False})
        assert pruned.status_code == 200
        assert pruned.json()["reclaimed"] == []


async def test_a_workspace_that_is_not_a_repository_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    """No git repository is an honest empty pool, never a 500 on startup."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    service = await _started(plain, tmp_path / "pool")
    pool = service.snapshot()

    assert pool.repo is None
    assert pool.problem is not None and "not inside a git repository" in pool.problem
    with pytest.raises(PoolUnavailableError):
        await service.acquire(_acquire())


async def test_an_unknown_slot_is_a_not_found(repo: Path, pool_root: Path) -> None:
    service = await _started(repo, pool_root)
    with pytest.raises(SlotNotFoundError):
        await service.release("slot-99", "whatever")


# ---- events, budget and the liveness probe ----------------------------------


async def test_every_state_change_reaches_the_bus(repo: Path, pool_root: Path) -> None:
    bus = EventBus()
    queue = bus.subscribe()
    service = WorktreeService(repo, bus, pool_root=pool_root)
    await service.start()

    info = await service.acquire(_acquire())
    assert info.lease is not None
    await service.release(info.slot, info.lease.lease_id)

    frames = [queue.get_nowait().model_dump() for _ in range(queue.qsize())]
    assert [frame["type"] for frame in frames] == ["worktree_changed", "worktree_changed"]
    assert [frame["worktree"]["state"] for frame in frames] == ["leased", "free"]


async def test_a_sweep_that_changes_nothing_says_nothing(repo: Path, pool_root: Path) -> None:
    """The shared bus is not a heartbeat.

    Every sweep re-asks about every non-free slot, so without this a pool with
    one parked dirty slot would emit a frame per prune saying it is still
    dirty. Caught on the live socket while driving the real server, not by
    reading the code.
    """
    bus = EventBus()
    queue = bus.subscribe()
    service = WorktreeService(repo, bus, pool_root=pool_root)
    await service.start()
    info = await service.acquire(_acquire())
    assert info.lease is not None
    (Path(info.path) / "model.py").write_bytes(b"VERSION = 99\n")
    await service.release(info.slot, info.lease.lease_id)
    while not queue.empty():
        queue.get_nowait()

    for _ in range(3):
        await service.prune()

    assert queue.qsize() == 0
    assert service.snapshot().slots[0].state == "dirty"


async def test_the_whole_pool_stays_a_small_payload(repo: Path, pool_root: Path) -> None:
    """The pool rides every response *and* every reconnect, so it carries a
    budget like every other agent-visible surface here.

    Measured at four leased slots with the longest holder a request may send:
    the ceiling below is that payload plus room for the ``detail`` sentences a
    dirty or under-review slot adds.
    """
    service = await _started(repo, pool_root, capacity=4)
    holder = "h" * 120
    for _ in range(4):
        await service.acquire(_acquire(holder, owner_pid=4242))

    payload = service.snapshot().model_dump_json()

    assert len(service.snapshot().slots) == 4
    assert len(payload) < 4096, f"pool payload grew to {len(payload)} bytes"


async def test_the_liveness_probe_never_touches_the_process_it_asks_about() -> None:
    """``os.kill(pid, 0)`` on Windows is ``TerminateProcess``. This is not that.

    The proof is a real child process: it is alive before the probe and still
    alive after it, and reads as gone once it has really exited. Spawned the
    way the service spawns git — a blocking ``Popen`` here would only be a
    second mechanism to keep two ruff versions happy about.
    """
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    try:
        assert process_alive(child.pid) is True
        assert child.returncode is None, "the probe ended the process it was asked about"
    finally:
        child.terminate()
        await child.wait()
    assert process_alive(child.pid) is False
    assert process_alive(0) is False


async def test_two_acquires_at_once_get_two_different_slots(repo: Path, pool_root: Path) -> None:
    """The lock, asserted. Two callers racing must never share one checkout —
    that is the entire premise of "one writer per checkout, always"."""
    service = await _started(repo, pool_root, capacity=2)

    first, second = await asyncio.gather(
        service.acquire(_acquire("lane-a")), service.acquire(_acquire("lane-b"))
    )

    assert first.slot != second.slot
    assert first.path != second.path
