"""An agent session that runs other agent sessions — Mission Control's crew.

The board renders the fleet; this is what puts agents in it. An *orchestrator*
is an ordinary :class:`~workbench_server.services.agent_sessions.AgentSession`
carrying five extra MCP tools (``services/agent_tools.py``), and a *worker* is an
ordinary session it started, bound to a borrowed worktree slot so two workers
cannot write to one checkout.

Nothing here is a new mechanism. A worker's permission prompts, status changes,
activity rows and turn costs all travel the channels every session already
travels, which is why the board sees a worker without knowing it is one — and
why the four constraints below could be *enforced* rather than described.

**1. Never auto-allow shell.** There is no code path in this module that
resolves a permission. A worker's ``Bash`` request blocks on the same future a
chat session's does, publishes the same fleet-wide
:class:`~workbench_server.models.agents.SessionPermissionEvent`, and is answered
by a human on the board. An orchestrator that could approve its own workers'
shell access is a different product with a different threat model, so the
absence is asserted (``test_orchestrator.py``) rather than left to review.

**2. A budget that binds, checked before spawning.** Per-worker and per-fleet
ceilings on *turns* and *dollars*, read from
:class:`~workbench_server.services.usage.UsageService` — the same numbers the
board renders, because a budget enforced against a second accumulator is a
budget that can disagree with what the user is looking at. Every refusal is a
:class:`~workbench_server.models.orchestrator.SpawnRefusal` carrying the limit,
the observation and **the environment variable that raises it**, so a cap that
is hit renders as a cap with a way out and never as a dead button.

**3. Bounded concurrency, honouring caps this service does not own.**
``WORKBENCH_MAX_CONCURRENT_SESSIONS`` belongs to the session manager and the
pool size to ``services/worktrees.py``; both are asked rather than duplicated,
and either one binding produces its own named refusal.

**4. A worker's death is not the orchestrator's death — and the reverse is not
an orphaning.** A turn that raises inside a worker is that worker's
``failed`` outcome and a tool result the orchestrator can read; it never
propagates. Stopping the orchestrator (:meth:`OrchestratorService.stop`) closes
every worker it owns *and releases their leases*, because a pool slot held by a
process that is no longer working in it is the exact failure the two idle
signals in ``services/worktrees.py`` exist to bound — and the honest way to
avoid needing them is to give the slot back.

**Why the roster lives here and not in the session manager.** ``SessionManager``
knows sessions; it does not know that one of them asked for another, and
teaching it would put a Mission Control concept into the type every other
capability uses. This service is the only thing that holds the parent link, the
lease and the task text, which is also why it is the only thing that can reap.
"""

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from workbench_server.models.agents import SessionInfo
from workbench_server.models.orchestrator import (
    MAX_TASK_CHARS,
    OrchestratorBudget,
    OrchestratorEvent,
    OrchestratorInfo,
    OrchestratorSnapshot,
    SpawnRefusal,
    WorkerInfo,
    WorkerOutcome,
)
from workbench_server.models.worktrees import AcquireWorktreeRequest
from workbench_server.services.agent_sessions import (
    AgentSession,
    EventPublisher,
    SessionManager,
    TooManySessionsError,
)
from workbench_server.services.agent_tools import MAX_READ_WINDOW_CHARS
from workbench_server.services.usage import UsageService
from workbench_server.services.worktrees import (
    LeaseError,
    PoolExhaustedError,
    PoolUnavailableError,
    SlotNotFoundError,
    WorktreeService,
)

log = structlog.get_logger()

#: Settings names quoted back to the user when a ceiling binds. Spelled out
#: rather than derived from the field names, because these are the strings a
#: person types into a shell and a derived one that drifted would be worse than
#: no hint at all. ``test_orchestrator.py`` asserts each names a real setting.
SETTING_MAX_WORKERS = "WORKBENCH_ORCHESTRATOR_MAX_WORKERS"
SETTING_WORKER_TURNS = "WORKBENCH_ORCHESTRATOR_WORKER_TURNS"
SETTING_WORKER_COST = "WORKBENCH_ORCHESTRATOR_WORKER_COST_USD"
SETTING_FLEET_TURNS = "WORKBENCH_ORCHESTRATOR_FLEET_TURNS"
SETTING_FLEET_COST = "WORKBENCH_ORCHESTRATOR_FLEET_COST_USD"
SETTING_MAX_SESSIONS = "WORKBENCH_MAX_CONCURRENT_SESSIONS"
SETTING_POOL_SIZE = "WORKBENCH_WORKTREE_POOL_SIZE"

#: Text kept per worker for :meth:`OrchestratorService.read`. Bounded because
#: this is a live buffer on the server, not a transcript — the transcript is on
#: disk and belongs to the session index.
WORKER_BUFFER_CHARS = 20_000


class OrchestratorUnavailableError(Exception):
    """This session is not an orchestrator, or the service is not wired up."""


@dataclass
class _Worker:
    """One worker's bookkeeping — the parent link, the lease and the task.

    Deliberately does **not** carry state, activity or cost: those live in the
    session, the activity service and the usage service, and a copy here would
    be a fourth number to keep honest.
    """

    worker_id: str
    orchestrator_id: str
    task: str
    path: str
    slot: str | None
    lease_id: str | None
    created_at: float
    updated_at: float
    outcome: WorkerOutcome | None = None
    detail: str | None = None
    #: Rolling tail of the worker's own output, for ``read_worker``.
    buffer: str = ""
    #: Set once the slot has been given back, so a double stop cannot release a
    #: lease somebody else now holds.
    released: bool = False
    #: The task draining this worker's own event queue. See
    #: :meth:`OrchestratorService._pump` — this is how the orchestrator can read
    #: a worker without a WebSocket, and how a worker's death is *noticed*.
    pump: "asyncio.Task[None] | None" = None


@dataclass
class _Roster:
    """One orchestrator's workers, newest last, plus the last cap it met."""

    workers: dict[str, _Worker] = field(default_factory=dict)
    last_refusal: SpawnRefusal | None = None


class OrchestratorService:
    """The roster, the budget, and the reaping.

    Constructed before :class:`SessionManager` exists (the manager needs the
    client factory, and the factory needs this service so an orchestrator
    session can reach its own tools), so the manager arrives through
    :meth:`bind`. Every entry point refuses politely until it does — a service
    that is half-wired must say so rather than raise ``AttributeError`` into a
    tool result.
    """

    def __init__(
        self,
        bus: EventPublisher,
        usage: UsageService,
        budget: OrchestratorBudget,
        worktrees: WorktreeService | None = None,
    ) -> None:
        self._bus = bus
        self._usage = usage
        self._budget = budget
        self._worktrees = worktrees
        self._sessions: SessionManager | None = None
        self._rosters: dict[str, _Roster] = {}
        #: Serialises spawn and stop. Both acquire and release a pool slot, and
        #: two concurrent spawns from one orchestrator racing the worker cap
        #: would each read "one slot left".
        self._lock = asyncio.Lock()

    def bind(self, sessions: SessionManager) -> None:
        """Hand over the session manager, once ``main.py`` has built it."""
        self._sessions = sessions

    # ---- reading ------------------------------------------------------------

    @property
    def budget(self) -> OrchestratorBudget:
        return self._budget

    def snapshot(self) -> OrchestratorSnapshot:
        """Every orchestrator and its workers — load, reconnect and the event."""
        return OrchestratorSnapshot(
            orchestrators=[
                OrchestratorInfo(
                    orchestrator_id=orchestrator_id,
                    workers=[self._info(worker) for worker in roster.workers.values()],
                    last_refusal=roster.last_refusal,
                )
                for orchestrator_id, roster in sorted(self._rosters.items())
            ],
            budget=self._budget,
            max_concurrent_sessions=self._sessions.max_concurrent if self._sessions else 0,
            worktree_capacity=self._pool_capacity(),
        )

    def workers_of(self, orchestrator_id: str) -> list[WorkerInfo]:
        roster = self._rosters.get(orchestrator_id)
        return [] if roster is None else [self._info(w) for w in roster.workers.values()]

    def _info(self, worker: _Worker) -> WorkerInfo:
        entry = self._usage.session_entry(worker.worker_id)
        return WorkerInfo(
            worker_id=worker.worker_id,
            orchestrator_id=worker.orchestrator_id,
            task=worker.task,
            slot=worker.slot,
            path=worker.path,
            # Turns come from the usage service rather than from a counter here:
            # it is the number the budget is enforced against and the number the
            # board renders, and a third one would eventually disagree with both.
            turns=entry.turns if entry else 0,
            outcome=worker.outcome,
            detail=worker.detail,
            created_at=worker.created_at,
            updated_at=worker.updated_at,
        )

    def _pool_capacity(self) -> int | None:
        if self._worktrees is None:
            return None
        pool = self._worktrees.snapshot()
        return None if pool.repo is None else pool.capacity

    # ---- spawning -----------------------------------------------------------

    async def spawn(
        self, orchestrator_id: str, task: str, base: str | None = None
    ) -> WorkerInfo | SpawnRefusal:
        """Start one worker on ``task``, in a slot of its own.

        Returns the worker, or the cap that stopped it — never raises for a
        refusal, because a refusal is an answer the orchestrator can act on and
        an exception is one it can only apologise for.
        """
        async with self._lock:
            sessions = self._sessions
            if sessions is None:
                return SpawnRefusal(
                    reason="unavailable",
                    detail="the orchestrator service is not wired up in this server",
                )
            roster = self._rosters.setdefault(orchestrator_id, _Roster())
            refusal = self._budget_refusal(roster, sessions)
            if refusal is not None:
                roster.last_refusal = refusal
                log.info(
                    "orchestrator.spawn_refused",
                    orchestrator=orchestrator_id,
                    reason=refusal.reason,
                )
                self._publish()
                return refusal
            slot, path, lease_id, pool_refusal = await self._take_slot(orchestrator_id)
            if pool_refusal is not None:
                roster.last_refusal = pool_refusal
                log.info(
                    "orchestrator.spawn_refused",
                    orchestrator=orchestrator_id,
                    reason=pool_refusal.reason,
                )
                self._publish()
                return pool_refusal
            try:
                session = sessions.create_at(path, slot or path.name, kind="worker")
            except TooManySessionsError:
                # The pool said yes and the session manager said no. Give the
                # slot straight back rather than leaking it — this is the one
                # window where we hold a lease with nothing to put in it.
                await self._release(slot, lease_id)
                refused = self._session_limit_refusal(sessions)
                roster.last_refusal = refused
                self._publish()
                return refused
            now = time.time()
            worker = _Worker(
                worker_id=session.local_id,
                orchestrator_id=orchestrator_id,
                task=task[:MAX_TASK_CHARS],
                path=str(path),
                slot=slot,
                lease_id=lease_id,
                created_at=now,
                updated_at=now,
            )
            roster.workers[worker.worker_id] = worker
            roster.last_refusal = None
            # Subscribed *before* the first message, or the opening deltas of
            # the turn we are about to start would be gone before anything was
            # listening — and ``read_worker`` would answer "no output yet" for a
            # worker that had already replied.
            worker.pump = asyncio.create_task(self._pump(worker, session))
            # The task *is* the worker's first message, which is what makes its
            # title, its transcript and its activity row read as the work rather
            # than as "new session".
            session.send_user_message(worker.task)
            log.info(
                "orchestrator.worker_spawned",
                orchestrator=orchestrator_id,
                worker=worker.worker_id,
                slot=slot,
            )
            self._publish()
            return self._info(worker)

    def _budget_refusal(self, roster: _Roster, sessions: SessionManager) -> SpawnRefusal | None:
        """Every ceiling this service owns, checked before anything is created.

        Order matters only in what the user is told first, and it goes from the
        cheapest thing to fix (stop a worker) to the most expensive (raise a
        cost ceiling).
        """
        running = [w for w in roster.workers.values() if w.outcome is None]
        if len(running) >= self._budget.max_workers:
            return SpawnRefusal(
                reason="worker_limit",
                detail=(
                    f"{len(running)} of {self._budget.max_workers} workers are already "
                    f"running — stop one, or raise {SETTING_MAX_WORKERS}"
                ),
                limit=float(self._budget.max_workers),
                observed=float(len(running)),
                setting=SETTING_MAX_WORKERS,
            )
        turns, cost = self._fleet_spend(roster)
        if turns >= self._budget.max_fleet_turns:
            return SpawnRefusal(
                reason="fleet_turns",
                detail=(
                    f"this orchestrator's workers have taken {turns} of "
                    f"{self._budget.max_fleet_turns} turns — raise {SETTING_FLEET_TURNS}"
                ),
                limit=float(self._budget.max_fleet_turns),
                observed=float(turns),
                setting=SETTING_FLEET_TURNS,
            )
        if cost >= self._budget.max_fleet_cost_usd:
            return SpawnRefusal(
                reason="fleet_cost",
                detail=(
                    f"this orchestrator's workers have spent ${cost:.2f} of "
                    f"${self._budget.max_fleet_cost_usd:.2f} — raise {SETTING_FLEET_COST}"
                ),
                limit=self._budget.max_fleet_cost_usd,
                observed=cost,
                setting=SETTING_FLEET_COST,
            )
        if sessions.active_count() >= sessions.max_concurrent:
            return self._session_limit_refusal(sessions)
        return None

    def _session_limit_refusal(self, sessions: SessionManager) -> SpawnRefusal:
        return SpawnRefusal(
            reason="session_limit",
            detail=(
                f"{sessions.active_count()} of {sessions.max_concurrent} sessions are "
                f"working — this cap counts sessions working, not sessions open; "
                f"raise {SETTING_MAX_SESSIONS}"
            ),
            limit=float(sessions.max_concurrent),
            observed=float(sessions.active_count()),
            setting=SETTING_MAX_SESSIONS,
        )

    def _fleet_spend(self, roster: _Roster) -> tuple[int, float]:
        """Turns and dollars every worker of this orchestrator has used.

        Stopped workers count. A budget that forgot what a failed worker cost
        would let an orchestrator spend its ceiling several times over by
        restarting, which is the loop this ceiling exists to bound.
        """
        turns = 0
        cost = 0.0
        for worker in roster.workers.values():
            entry = self._usage.session_entry(worker.worker_id)
            if entry is None:
                continue
            turns += entry.turns
            cost += entry.cost_usd
        return turns, cost

    def _worker_refusal(self, worker: _Worker) -> SpawnRefusal | None:
        """The per-worker half, checked before *more work* rather than before
        creation — a worker's first turn has by definition cost nothing."""
        entry = self._usage.session_entry(worker.worker_id)
        if entry is None:
            return None
        if entry.turns >= self._budget.max_worker_turns:
            return SpawnRefusal(
                reason="worker_turns",
                detail=(
                    f"worker {worker.worker_id} has taken {entry.turns} of "
                    f"{self._budget.max_worker_turns} turns — raise {SETTING_WORKER_TURNS}"
                ),
                limit=float(self._budget.max_worker_turns),
                observed=float(entry.turns),
                setting=SETTING_WORKER_TURNS,
            )
        if entry.cost_usd >= self._budget.max_worker_cost_usd:
            return SpawnRefusal(
                reason="worker_cost",
                detail=(
                    f"worker {worker.worker_id} has spent ${entry.cost_usd:.2f} of "
                    f"${self._budget.max_worker_cost_usd:.2f} — raise {SETTING_WORKER_COST}"
                ),
                limit=self._budget.max_worker_cost_usd,
                observed=entry.cost_usd,
                setting=SETTING_WORKER_COST,
            )
        return None

    async def _take_slot(
        self, orchestrator_id: str
    ) -> tuple[str | None, Path, str | None, SpawnRefusal | None]:
        """Borrow a worktree for one worker.

        The pool is not optional decoration — it is what makes two workers safe
        — so a pool that cannot serve is a **refusal**, not a quiet fallback to
        the workspace. Two workers writing to the user's own checkout is exactly
        the failure ``CLAUDE.md``'s "one writer per checkout" rule names, and
        producing it silently would be worse than not spawning.
        """
        if self._worktrees is None:
            return (
                None,
                Path(),
                None,
                SpawnRefusal(
                    reason="unavailable",
                    detail="no worktree pool is configured, so a worker has nowhere safe to work",
                ),
            )
        try:
            info = await self._worktrees.acquire(
                AcquireWorktreeRequest(
                    holder=f"orchestrator:{orchestrator_id}",
                    # This process. The pool's second idle signal, so a slot is
                    # not reclaimed under a worker that is still running.
                    owner_pid=os.getpid(),
                )
            )
        except PoolExhaustedError as err:
            return (
                None,
                Path(),
                None,
                SpawnRefusal(
                    reason="no_worktree",
                    detail=str(err)[:280],
                    limit=float(self._pool_capacity() or 0),
                    setting=SETTING_POOL_SIZE,
                ),
            )
        except (PoolUnavailableError, OSError, ValueError) as err:
            return (
                None,
                Path(),
                None,
                SpawnRefusal(
                    reason="no_worktree",
                    detail=f"the worktree pool could not hand out a slot: {err}"[:280],
                ),
            )
        lease = info.lease
        return info.slot, Path(info.path), lease.lease_id if lease else None, None

    # ---- talking to a worker ------------------------------------------------

    def send(self, orchestrator_id: str, worker_id: str, text: str) -> str | SpawnRefusal:
        """Give a running worker more work. Returns a sentence, or the cap."""
        worker, session = self._resolve(orchestrator_id, worker_id)
        if worker is None or session is None:
            return f"no worker {worker_id} belongs to this orchestrator"
        if worker.outcome is not None:
            return f"worker {worker_id} has already {worker.outcome}"
        refusal = self._worker_refusal(worker)
        if refusal is None:
            roster = self._rosters.get(orchestrator_id)
            if roster is not None:
                fleet = self._budget_fleet_only(roster)
                if fleet is not None:
                    refusal = fleet
        if refusal is not None:
            roster = self._rosters.setdefault(orchestrator_id, _Roster())
            roster.last_refusal = refusal
            self._publish()
            return refusal
        session.send_user_message(text)
        worker.updated_at = time.time()
        self._publish()
        return f"sent to {worker_id}"

    def _budget_fleet_only(self, roster: _Roster) -> SpawnRefusal | None:
        """The fleet halves of the budget, without the worker-count check.

        Sending to an existing worker must not be refused for "3 of 3 workers
        are running" — that is a cap on *starting* one, and refusing to talk to
        a worker because it exists would be nonsense.
        """
        turns, cost = self._fleet_spend(roster)
        if turns >= self._budget.max_fleet_turns:
            return SpawnRefusal(
                reason="fleet_turns",
                detail=(
                    f"this orchestrator's workers have taken {turns} of "
                    f"{self._budget.max_fleet_turns} turns — raise {SETTING_FLEET_TURNS}"
                ),
                limit=float(self._budget.max_fleet_turns),
                observed=float(turns),
                setting=SETTING_FLEET_TURNS,
            )
        if cost >= self._budget.max_fleet_cost_usd:
            return SpawnRefusal(
                reason="fleet_cost",
                detail=(
                    f"this orchestrator's workers have spent ${cost:.2f} of "
                    f"${self._budget.max_fleet_cost_usd:.2f} — raise {SETTING_FLEET_COST}"
                ),
                limit=self._budget.max_fleet_cost_usd,
                observed=cost,
                setting=SETTING_FLEET_COST,
            )
        return None

    async def _pump(self, worker: _Worker, session: AgentSession) -> None:
        """Drain one worker's own event queue into the tail and the outcome.

        A worker has no WebSocket — nobody opened a chat for it — so this is the
        only reader of its frames, and it is what lets ``read_worker`` answer
        without the orchestrator subscribing to anything. It is also where
        **a worker's death stops being silent**: ``AgentSession._run_turn``
        turns an exception into ``agent_error`` plus ``turn_done(is_error)``
        rather than letting it escape, so from out here a dying worker looks
        exactly like a finished one until these two frames are read.

        The queue is bounded and drops its oldest frame when full
        (``AgentSession._emit``), which is the right trade for a rolling tail:
        a worker that outruns this loop loses the middle of its output, never
        the end, and never wedges the session that is producing it.
        """
        queue = session.subscribe()
        try:
            while True:
                event = await queue.get()
                kind = getattr(event, "type", "")
                if kind == "text_delta":
                    self._append(worker, str(getattr(event, "text", "")))
                elif kind == "tool_use":
                    self._append(worker, f"\n[{getattr(event, 'summary', 'tool')}]\n")
                elif kind == "agent_error":
                    message = str(getattr(event, "message", "the turn failed"))
                    self._append(worker, f"\n[error] {message}\n")
                    self.note_failed(worker.worker_id, message)
                elif kind == "turn_done" and bool(getattr(event, "is_error", False)):
                    self.note_failed(worker.worker_id, "the worker's turn ended in an error")
        except asyncio.CancelledError:
            raise
        except Exception:  # a reader that dies must not take the worker with it
            log.exception("orchestrator.pump_failed", worker=worker.worker_id)
        finally:
            session.unsubscribe(queue)

    def _append(self, worker: _Worker, text: str) -> None:
        worker.buffer = (worker.buffer + text)[-WORKER_BUFFER_CHARS:]
        worker.updated_at = time.time()

    def read(self, orchestrator_id: str, worker_id: str, window: int) -> str:
        """The tail of a worker's output, with what was withheld said out loud.

        The three shapes CLAUDE.md requires of an agent-facing result are all
        here: a stated size and the argument that widens it, an explicit "none"
        rather than blankness, and the obvious next step.
        """
        worker, session = self._resolve(orchestrator_id, worker_id)
        if worker is None or session is None:
            return f"no worker {worker_id} belongs to this orchestrator"
        limit = max(1, min(window, MAX_READ_WINDOW_CHARS))
        text = worker.buffer
        state = session.state if worker.outcome is None else worker.outcome
        head = f"worker {worker_id} [{state}]"
        if not text:
            return f"{head}: no output yet — it has produced nothing since it started"
        shown = text[-limit:]
        withheld = len(text) - len(shown)
        note = (
            ""
            if withheld == 0
            else (
                f" ({withheld} earlier chars withheld — "
                f"raise `chars` up to {MAX_READ_WINDOW_CHARS})"
            )
        )
        return f"{head}{note}:\n{shown}"

    # ---- stopping -----------------------------------------------------------

    async def stop_worker(self, orchestrator_id: str, worker_id: str) -> str:
        """Stop one worker and give its slot back."""
        async with self._lock:
            worker, session = self._resolve(orchestrator_id, worker_id)
            if worker is None:
                return f"no worker {worker_id} belongs to this orchestrator"
            await self._retire(worker, session, "stopped", "stopped by the orchestrator")
            self._publish()
            return f"stopped {worker_id}"

    async def stop(self, orchestrator_id: str) -> int:
        """Stop an orchestrator's whole crew. Returns how many were stopped.

        Called when the user stops the orchestrator from the board.
        **This is the anti-orphaning rule**: a worker outliving the session that
        asked for it is an agent nobody is watching, holding a pool slot nobody
        will release, and the only signal left would be the lease deadline — an
        hour by default.

        The orchestrator's own turn is **interrupted** as part of this, which is
        what makes the gesture mean what its button says. Reaping a crew while
        the session that asked for it is still mid-turn would let it spawn the
        crew straight back; the session itself is left open, because the user
        stopped the work, not the conversation.
        """
        async with self._lock:
            stopped = await self._stop_all(orchestrator_id)
            session = None if self._sessions is None else self._sessions.get(orchestrator_id)
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.interrupt()
            return stopped

    async def stop_all(self) -> None:
        """Every orchestrator, every worker. The lifespan's shutdown hook."""
        async with self._lock:
            for orchestrator_id in list(self._rosters):
                await self._stop_all(orchestrator_id)

    async def _stop_all(self, orchestrator_id: str) -> int:
        roster = self._rosters.get(orchestrator_id)
        if roster is None:
            return 0
        stopped = 0
        for worker in list(roster.workers.values()):
            if worker.outcome is not None and worker.released:
                continue
            _, session = self._resolve(orchestrator_id, worker.worker_id)
            await self._retire(worker, session, "stopped", "its orchestrator stopped")
            stopped += 1
        if stopped:
            log.info("orchestrator.crew_stopped", orchestrator=orchestrator_id, workers=stopped)
        self._publish()
        return stopped

    def note_failed(self, worker_id: str, detail: str) -> None:
        """A worker's turn raised. Recorded, never propagated.

        The slot is deliberately *not* released here: a failed worker's
        checkout is usually the most interesting thing on the machine, and
        ``services/worktrees.py`` will park it as ``dirty`` rather than reset it
        if there is uncommitted work in it. Stopping the worker is what gives
        the slot back, and that is a decision a human or the orchestrator makes.
        """
        for roster in self._rosters.values():
            worker = roster.workers.get(worker_id)
            if worker is None or worker.outcome is not None:
                continue
            worker.outcome = "failed"
            worker.detail = detail[:280]
            worker.updated_at = time.time()
            log.info("orchestrator.worker_failed", worker=worker_id, detail=worker.detail)
            self._publish()
            return

    async def _retire(
        self,
        worker: _Worker,
        session: AgentSession | None,
        outcome: WorkerOutcome,
        detail: str,
    ) -> None:
        if worker.outcome is None:
            worker.outcome = outcome
            worker.detail = detail
        worker.updated_at = time.time()
        pump, worker.pump = worker.pump, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        if session is not None:
            # Interrupt first so a tool call parked on a permission future
            # unwinds, then close. Both are already exception-guarded inside the
            # session; this guard is for the case where they are not reached.
            with contextlib.suppress(Exception):
                await session.interrupt()
            with contextlib.suppress(Exception):
                await session.close()
            if self._sessions is not None:
                self._sessions.sessions.pop(worker.worker_id, None)
        await self._release(worker.slot, worker.lease_id)
        worker.released = True

    async def _release(self, slot: str | None, lease_id: str | None) -> None:
        """Give a slot back, and never let that failure end a stop.

        A release that raises would leave the worker running in a half-stopped
        state; the pool's own reaper is the backstop for a lease that outlives
        its holder, so the right answer here is to log and carry on.
        """
        if self._worktrees is None or slot is None or lease_id is None:
            return
        try:
            await self._worktrees.release(slot, lease_id)
        except (SlotNotFoundError, LeaseError) as err:
            log.info("orchestrator.release_skipped", slot=slot, detail=str(err))
        except Exception:
            log.exception("orchestrator.release_failed", slot=slot)

    # ---- internals ----------------------------------------------------------

    def _resolve(
        self, orchestrator_id: str, worker_id: str
    ) -> tuple[_Worker | None, AgentSession | None]:
        """This orchestrator's worker, and its live session.

        Scoped to the orchestrator on purpose: one orchestrator must not be able
        to read, drive or stop another's worker by id.
        """
        roster = self._rosters.get(orchestrator_id)
        worker = None if roster is None else roster.workers.get(worker_id)
        if worker is None:
            return None, None
        session = None if self._sessions is None else self._sessions.get(worker_id)
        return worker, session

    def _publish(self) -> None:
        self._bus.publish(OrchestratorEvent(snapshot=self.snapshot()))

    def worker_sessions(self) -> list[SessionInfo]:
        """Every live worker, for tests and for the board's honesty checks."""
        if self._sessions is None:
            return []
        ids = {w for roster in self._rosters.values() for w in roster.workers}
        return [info for info in self._sessions.live_infos() if info.session_id in ids]
