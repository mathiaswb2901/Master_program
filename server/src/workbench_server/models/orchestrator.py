"""Mission Control's second half: an agent session that runs other sessions.

The board (``ui/src/panels/MissionControl.tsx``) renders the fleet by
*composing* services that already exist — ``services/activity.py`` for what each
session is touching, ``services/usage.py`` for what it has cost,
``services/worktrees.py`` for the slot it holds. This module adds the one thing
none of them can answer: **who asked for this session, and under what ceiling**.

Four constraints are in the schema rather than in prose, because each is a
promise a later edit could quietly break:

1. **A refusal names its ceiling and the setting that raises it.**
   :class:`SpawnRefusal` has no free-text-only form: ``limit``, ``observed`` and
   ``setting`` are required, so a cap that is hit renders as "3 of 3 workers —
   raise WORKBENCH_ORCHESTRATOR_MAX_WORKERS" and never as a dead button
   (CLAUDE.md's plural-tool rule; ROADMAP product principle 4c).
2. **The budget is data, and it is served.** :class:`OrchestratorBudget` is on
   the snapshot, so the pane shows the ceiling it is working under instead of
   the UI restating a number the server enforces.
3. **A worker's cost is not derived here.** :class:`WorkerInfo` carries no
   dollars: the board reads them from the usage service's per-session entries,
   which is the same authority the budget check reads. Two places computing what
   a worker spent is exactly the duplication the re-scope of ROADMAP item 7
   forbids.
4. **A worker is a session, and says which one.** ``worker_id`` *is* the
   session's local id, so the board's card, the activity row, the usage entry
   and the agent pane (``agent#<session_id>``) are all the same thing seen from
   different distances — no id map, and clicking a card reveals its pane.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: How a worker ended, when it did. ``stopped`` is the orchestrator (or the
#: user) asking; ``failed`` is the worker's own turn raising. They are separate
#: because only the second one is news.
WorkerOutcome = Literal["stopped", "failed"]

#: Longest task text a spawn may carry. The whole prompt is the worker's first
#: message, so this is a real bound on what one tool call can start — and it is
#: what keeps the spawn tool's input schema honest about its cost.
MAX_TASK_CHARS = 4_000

#: Cap on any one sentence this module puts on the wire.
MAX_DETAIL_CHARS = 300


class OrchestratorBudget(BaseModel):
    """The ceilings an orchestrator works under, as the server enforces them.

    Served rather than restated in the UI, for the same reason
    :class:`~workbench_server.models.agents.SessionLimits` is: what a ceiling
    counts is a server rule, and a client that guessed would grey a control at
    the wrong moment.

    Every field names an environment variable in
    :class:`~workbench_server.config.Settings` — that is what
    :class:`SpawnRefusal` reports when one binds.
    """

    #: Workers one orchestrator may have running at once.
    max_workers: int
    #: Turns a single worker may take before it is refused more work.
    max_worker_turns: int
    #: Dollars a single worker may spend before it is refused more work.
    max_worker_cost_usd: float
    #: Turns every worker of this orchestrator may take, together.
    max_fleet_turns: int
    #: Dollars every worker of this orchestrator may spend, together.
    max_fleet_cost_usd: float


class WorkerInfo(BaseModel):
    """One worker, as the board and the orchestrator's own tools see it.

    Deliberately thin. State comes from the session (``SessionStatusEvent``),
    activity from the activity feed, cost from the usage service; what is
    genuinely *here* is the parent, the task, the slot and the outcome.
    """

    #: The worker session's local id — the same string as its agent pane's key.
    worker_id: str
    #: The orchestrator session that spawned it.
    orchestrator_id: str
    #: The task text the orchestrator sent as the worker's first message,
    #: capped for display. The board shows it; the worker received all of it.
    task: str = Field(max_length=MAX_TASK_CHARS)
    #: Pool slot name (``slot-01``), or None when the worker runs in the
    #: workspace because the pool had nothing to give.
    slot: str | None = None
    #: Absolute path the worker's cwd is bound to. Shown, never opened: it is
    #: outside the workspace jail by construction (``services/worktrees.py``).
    path: str
    #: Turns this worker has taken, counted from the usage service.
    turns: int = 0
    #: Set once the worker is no longer running.
    outcome: WorkerOutcome | None = None
    #: Why it ended, when there is something to say. One sentence.
    detail: str | None = Field(default=None, max_length=MAX_DETAIL_CHARS)
    created_at: float
    updated_at: float


class SpawnRefusal(BaseModel):
    """A cap that bound, with everything the pane needs to render it.

    Never a bare message: a ceiling the user cannot see the way *out* of is the
    dead button the pane rules forbid. ``setting`` is the environment variable
    that raises it — or, for the two ceilings this service borrows, the setting
    that owns them (``WORKBENCH_MAX_CONCURRENT_SESSIONS``,
    ``WORKBENCH_WORKTREE_POOL_SIZE``).
    """

    reason: Literal[
        "worker_limit",
        "worker_turns",
        "worker_cost",
        "fleet_turns",
        "fleet_cost",
        "session_limit",
        "no_worktree",
        "unavailable",
    ]
    #: One sentence, safe to render as-is.
    detail: str = Field(max_length=MAX_DETAIL_CHARS)
    #: The ceiling that bound, as a number. None for ``unavailable``, which is
    #: not a ceiling at all.
    limit: float | None = None
    #: What was measured against it.
    observed: float | None = None
    #: The environment variable that raises the ceiling, when one does.
    setting: str | None = None


class OrchestratorInfo(BaseModel):
    """One orchestrator session and the workers it owns."""

    orchestrator_id: str
    workers: list[WorkerInfo] = Field(default_factory=list)
    #: The most recent refusal this orchestrator met, so the board can show the
    #: cap *where the user is looking* rather than only inside the transcript
    #: of a conversation they may not have open.
    last_refusal: SpawnRefusal | None = None


class OrchestratorSnapshot(BaseModel):
    """``GET /api/orchestrator``: every orchestrator, its workers, the budget.

    Serves initial load and reconnect; :class:`OrchestratorEvent` carries the
    same shape live, so the board has one renderer and one merge — the pattern
    ``GET /api/activity`` and ``GET /api/usage`` already established.
    """

    orchestrators: list[OrchestratorInfo] = Field(default_factory=list)
    budget: OrchestratorBudget
    #: ``WORKBENCH_MAX_CONCURRENT_SESSIONS`` and how many sessions are working,
    #: carried here so the board can say *which* of the two caps is the binding
    #: one without a second request.
    max_concurrent_sessions: int
    #: Slots the worktree pool may grow to, and how many this orchestrator
    #: holds. ``None`` when there is no pool to hold one (no git, not a repo).
    worktree_capacity: int | None = None


class OrchestratorEvent(BaseModel):
    """Bus event: an orchestrator's roster changed.

    The whole snapshot rather than a delta, like :class:`UsageEvent`: it is
    small (one orchestrator, a handful of workers), it is what the board
    renders, and it makes the reconnect path and the live path identical.
    """

    type: Literal["orchestrator"] = "orchestrator"
    snapshot: OrchestratorSnapshot
