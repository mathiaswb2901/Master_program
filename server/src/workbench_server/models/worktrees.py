"""Managed worktree pool schemas: parallel work that cannot step on itself.

``CLAUDE.md`` has said "one writer per checkout, always" since M4, and until now
that was enforced by discipline rather than by the product. This is the wire
vocabulary for enforcing it: a **pool of git worktrees** a caller borrows a slot
from, works in, and gives back.

Four decisions from the ROADMAP are encoded in these types rather than left to a
comment in the service:

1. **Detached HEAD.** A pooled worktree carries no branch, so git's
   "already checked out in another worktree" refusal cannot happen. There is no
   ``branch`` field here on purpose — :attr:`WorktreeInfo.head` is a commit and
   nothing else. What a caller does with the slot (create a branch, commit,
   push) is the caller's business; what the *pool* hands out is a detached
   checkout.
2. **Pool, never destroy.** :data:`WorktreeState` has no ``removed``. A finished
   slot is reset and returned to ``free``, keeping ``node_modules``, ``.venv``
   and build caches — which are ignored files and therefore survive
   ``git clean -fd`` — so a cold install is paid once per slot rather than once
   per task. It also side-steps a hazard measured on this machine:
   ``git worktree remove`` recurses through a Windows junction, so any design
   that *links* dependencies into a slot can empty the checkout they point at.
3. **Two idle signals.** A lease carries both an :attr:`~WorktreeLease.owner_pid`
   and an :attr:`~WorktreeLease.expires_at`, and a slot is reclaimable only when
   **both** say idle. The deadline is what holds a slot for an agent working in
   it unattended with nothing of ours running; the pid is what holds it past the
   deadline for an owner that is demonstrably still there.
4. **Fail safe on corrupt state.** A pool rebuilt from disk (state file lost or
   truncated) comes back with every slot ``leased`` under a
   :attr:`~WorktreeLease.recovered` lease. Assume in use; never assume free.

And the rule that outranks all four: **dirty is sacred**. A slot holding tracked
changes or untracked files is never handed out and never reclaimed without an
explicit override, because losing a user's uncommitted work is the one
unrecoverable failure here.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: What a slot is doing.
#:
#: * ``free``          — reset, clean, detached: available to acquire.
#: * ``leased``        — somebody holds it. Not available, whatever the clock says.
#: * ``dirty``         — holds uncommitted work. Never handed out, never reclaimed
#:                       without ``force``. The state exists so the protection is
#:                       *visible* rather than an internal branch.
#: * ``needs_review``  — the pool could not put the slot back into a state it can
#:                       vouch for (a reset that kept failing under a file lock, a
#:                       directory git does not know as a worktree). A human looks;
#:                       the pool does not reach for ``--force`` on its own.
WorktreeState = Literal["free", "leased", "dirty", "needs_review"]

#: Ceilings. The whole pool rides every response and every event, so a runaway
#: holder string or a pool nobody bounded must not become a runaway payload.
MAX_HOLDER_CHARS = 120
MAX_DETAIL_CHARS = 300
MAX_POOL_SIZE = 32
#: Lease bounds. The floor keeps a caller from asking for a lease so short the
#: next sweep reclaims a slot out from under it; the ceiling keeps a typo from
#: parking a slot for a year.
MIN_LEASE_SECONDS = 30.0
MAX_LEASE_SECONDS = 7 * 24 * 3600.0


class WorktreeLease(BaseModel):
    """Who holds a slot, and the two independent reasons they still do."""

    lease_id: str
    #: Free text naming the holder for a human reading the pool — an agent
    #: session id, a task name, "mission-control worker 3". The pool never
    #: interprets it.
    holder: str = Field(min_length=1, max_length=MAX_HOLDER_CHARS)
    #: Idle signal one: the process working in the slot. ``None`` when the caller
    #: did not name one (or after a rebuild from disk, where it is unknowable) —
    #: which makes the deadline the only signal, deliberately.
    owner_pid: int | None = None
    #: Idle signal two: unix seconds after which the lease no longer holds the
    #: slot *by itself*. Renewable; see ``RenewWorktreeRequest``.
    expires_at: float
    acquired_at: float
    #: This lease was invented while rebuilding the pool from disk because the
    #: state file could not be read. Nothing is known about who was working here,
    #: so the slot is held rather than offered — decision 4.
    recovered: bool = False


class WorktreeInfo(BaseModel):
    """One slot in the pool, as the UI and the API see it."""

    #: Stable, human-readable, and the directory name under the pool root
    #: (``slot-01``). It is the id every endpoint takes.
    slot: str
    #: **Absolute** path. Unlike everything else on this wire, a pooled worktree
    #: is deliberately *outside* the workspace (see the service), so a
    #: workspace-relative path could not name it.
    path: str
    state: WorktreeState
    #: The detached commit the slot is sitting on. ``None`` only before the first
    #: successful checkout, or when git could not be asked.
    head: str | None = None
    #: Present exactly when ``state == "leased"``.
    lease: WorktreeLease | None = None
    #: How many paths ``git status --porcelain`` reported. Ignored files (the
    #: dependency caches decision 2 keeps) are not among them, so a slot with a
    #: warm ``node_modules`` and nothing else reads as clean.
    dirty_files: int = 0
    created_at: float
    updated_at: float
    #: Why the slot is where it is, for ``dirty`` and ``needs_review``. One line,
    #: aimed at a human; never a stack trace.
    detail: str | None = Field(default=None, max_length=MAX_DETAIL_CHARS)


class WorktreePool(BaseModel):
    """GET /api/worktrees — the whole pool in one payload.

    Small on purpose: the pool is capped at :data:`MAX_POOL_SIZE`, so listing it
    whole is cheaper than an endpoint per slot and there is no pagination to get
    wrong.
    """

    #: The pool root, absolute. Outside the workspace, so the file tree and the
    #: watcher never see it.
    root: str
    #: The repository the slots are worktrees of, absolute. ``None`` when the
    #: workspace is not inside a git repository — which is not an error, it is
    #: the honest "there is nothing to pool here".
    repo: str | None = None
    #: How many slots this pool may grow to.
    capacity: int
    slots: list[WorktreeInfo] = Field(default_factory=list)
    #: Non-null when the pool could not be used at all (no git, not a
    #: repository), or when the state file had to be rebuilt from disk. The UI
    #: shows it; nothing here raises.
    problem: str | None = None


class WorktreeChangedEvent(BaseModel):
    """Broadcast on /ws/events whenever a slot changes state.

    Rides the existing bus, so a window that never issued the acquire still
    tracks the pool — and a reconnecting client re-reads the same truth from
    ``GET /api/worktrees``.
    """

    type: Literal["worktree_changed"] = "worktree_changed"
    worktree: WorktreeInfo


class AcquireWorktreeRequest(BaseModel):
    """POST /api/worktrees/acquire."""

    holder: str = Field(min_length=1, max_length=MAX_HOLDER_CHARS)
    #: What to check the slot out at. Any commit-ish git accepts; defaults to the
    #: repository's current ``HEAD``. Resolved to a commit *before* the checkout,
    #: so the slot is detached at a sha and never at a branch tip that moves.
    base: str | None = Field(default=None, max_length=200)
    #: Idle signal one. A caller that names its pid gets its slot held past the
    #: deadline for as long as it is alive.
    owner_pid: int | None = None
    #: Idle signal two, in seconds. Defaults to the server's configured lease.
    ttl_seconds: float | None = Field(default=None, ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)


class ReleaseWorktreeRequest(BaseModel):
    """POST /api/worktrees/{slot}/release."""

    lease_id: str
    #: Throw away uncommitted work in the slot instead of parking it as
    #: ``dirty``. Never the default: this is the one irreversible thing the pool
    #: can do to a user's files.
    discard_changes: bool = False


class RenewWorktreeRequest(BaseModel):
    """POST /api/worktrees/{slot}/renew — push the deadline out.

    A lease that cannot be renewed is a timeout, not a lease: an agent working
    for longer than one TTL would otherwise have its slot reclaimed underneath
    it the moment its owner process happened not to be running.
    """

    lease_id: str
    ttl_seconds: float | None = Field(default=None, ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)


class PruneRequest(BaseModel):
    """POST /api/worktrees/prune — the reaper, run on demand."""

    #: Also reclaim slots holding uncommitted work. Off by default and never
    #: implied by anything else, because it destroys files nobody can get back.
    force: bool = False


class KeptSlot(BaseModel):
    """A slot a sweep deliberately left alone."""

    slot: str
    reason: Literal["leased", "owner_alive", "dirty", "needs_review", "reset_failed"]
    detail: str | None = Field(default=None, max_length=MAX_DETAIL_CHARS)


class PruneResult(BaseModel):
    """What a sweep did, named slot by slot rather than counted.

    ``kept`` is the interesting half: it is the list of slots the pool refused to
    reclaim, each with the reason, so "why is the pool full" is answerable
    without reading a log.
    """

    reclaimed: list[str] = Field(default_factory=list)
    kept: list[KeptSlot] = Field(default_factory=list)
    pool: WorktreePool


#: Bump when a field the reader cannot ignore changes shape. A document from a
#: version this one does not understand is treated exactly like a truncated one:
#: rebuild from disk, every slot leased until verified.
POOL_STATE_VERSION = 1


class WorktreePoolState(BaseModel):
    """The persisted document, ``<pool root>/pool.json``.

    Not a wire type — it never leaves the machine — but a Pydantic model all the
    same, because the recovery path in the service is defined as "what
    ``model_validate`` refuses", and that is a much sharper line than "what
    ``json.loads`` refuses".
    """

    version: int = POOL_STATE_VERSION
    slots: list[WorktreeInfo] = Field(default_factory=list, max_length=MAX_POOL_SIZE)
