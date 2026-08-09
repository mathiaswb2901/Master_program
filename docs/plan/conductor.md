# The conductor — hierarchical orchestration with a live project overview that scales

Status: **plan** (this document is the capability's Plan PR; no feature code lands with it).
Owner-ratified 2026-08-09 evening as the flagship orchestration vision. It turns "one
leader agent that holds a live overview of a whole project and coordinates workers across
its units" into a sequence of disjoint, fake-first, independently shippable PRs with
explicit file ownership, so parallel lanes never collide.

It is written against the repo as it stands after M5/M6/M7's landed waves, and it composes
with the seams that already exist rather than duplicating them. **The single new artifact
is the overview**; everything that dispatches, budgets, isolates, reviews and renders a
fleet already ships, and this plan is mostly a statement of what to reuse and what — for
once — is genuinely new.

## The value sentence

**One conductor holds a live, per-project *overview* of the whole project** — the
discovered units, a rolling one-paragraph digest of each so the leader has awareness
without holding every full text in context, who is working on what, and which unit's
numbers another unit leans on — **and dispatches workers in waves up to the machine's real
cap.** So a 20-chapter thesis or a 30-model energy analysis is supervised from a single map
a human could never hold in their head at once, and the user watches the whole project
advance in Mission Control without opening twenty sessions.

## What already exists (and two things the brief got wrong)

Every claim below was read against master, because a plan that composes with a seam it
misremembers is a plan that collides with it.

- **The orchestrator is real and is the delegation substrate** (`services/orchestrator.py`,
  `models/orchestrator.py`, `routers/orchestrator.py`, landed as M5 item 7 / #63). An
  *orchestrator* is an ordinary `AgentSession` carrying five extra MCP tools
  (`spawn_worker`, `list_workers`, `read_worker`, `send_to_worker`, `stop_worker` — a
  **separate tuple** from `AGENT_TOOLS` in `services/agent_tools.py`, because a schema is
  paid on every request and a chat session must not carry five tools it can never call). A
  *worker* is an ordinary session it started through `SessionManager.create_at(path, …,
  kind="worker")`, bound to a borrowed worktree slot. `OrchestratorService` owns the
  roster, the budget (`OrchestratorBudget`: `max_workers`, `max_worker_turns`,
  `max_worker_cost_usd`, `max_fleet_turns`, `max_fleet_cost_usd`), and the reaping; every
  refusal is a `SpawnRefusal` naming the limit, the observation **and the environment
  variable that raises it**. The conductor **extends** this, it does not reinvent it.
- **The worktree pool is real** (`services/worktrees.py`, `models/worktrees.py`,
  `routers/worktrees.py`, M5 item 6 / #46). `acquire(AcquireWorktreeRequest(holder=,
  owner_pid=))` borrows a detached-HEAD slot; `release`/`renew`/`prune` return it. Pool,
  never destroy; two idle signals (`owner_pid` + `expires_at`); **dirty is sacred**; a
  cross-process `PoolLock`; the pool root lives under the machine app-data dir, **never**
  under `.workbench/`. This is what makes "one writer per checkout" a feature and the only
  thing that makes N workers safe.
- **The live-fleet signal already ships — this is the first brief correction.** The task
  brief calls "Mission Control + the agent-activity surface" *PR-D, queued*. In master both
  landed: Mission Control is M5 item 7 (`ui/src/panels/MissionControl.tsx`,
  `ui/src/mission.ts`, #63) and the **agent-activity surface** is M5 item 10 —
  `services/activity.py` + `models/activity.py` + `GET /api/activity`, published as
  `SessionActivityEvent` on `/ws/events`, rendered by `ui/src/panels/ActivityPanel.tsx`.
  `mission.ts::buildCards` already *joins* activity, usage, the pool and `session_status`
  (and, since M6, a validation read) and "computes the join and nothing else". **The
  overview aggregates a signal that already exists** rather than growing a new one; the
  conductor's per-unit "who is on this and what are they touching" is `WorkerInfo` +
  `SessionActivityEvent`, read, not reinvented.
- **The validation/evidence frame is real** (`services/validation.py`,
  `models/validation.py`, `routers/validation.py`, M6 #82). A `ValidationResult` with typed
  `EvidenceItem`s and a **derived** `RiskLevel`, a `ValidationCheck` protocol (a registry
  of checks, not a hardwired pipeline), a `ValidationEvent` on the bus with `GET
  /api/validation` replay, a bounded result map, and **the one mandatory human approval**
  (`POST /api/validation/{id}/approve`; a stale approval is `404`, never a 200 read as a
  decision).
- **The fresh-context reviewer is real** (`services/review.py`, `models/review.py`, M6
  staged-review PR 2 / #126). A `ValidationCheck` that puts a fresh session in front of the
  subject's diff — spawned through `SessionManager.create_at`, **not** the SDK's
  `fork_session` (a fork inherits the implementer's self-justifications), diff includes
  untracked files, the reviewer is told what it was **not** shown, ceilings on turns,
  dollars and wall clock. **Evidence, never authority**: there is no call in it that records
  an approval, and it ships **no agent-facing tool that starts a review** (a session that
  could commission its own reviewer could loop on it, on the user's money). The conductor's
  reintegration reuses this check exactly as-is.
- **Structure is discoverable today.** `workspace_search` (#97, the `WorkspaceSearcher`
  behind `services/search.py`) and the recursive tree walk (`GET /api/files/tree`,
  `services/workspace.py::tree`) are how an agent reads a project it was not told the shape
  of. The leader greps and walks; it is never handed a `chapter` type.
- **Provenance and the watcher** (`services/provenance.py` → `FileProvenanceEvent`;
  `services/watcher.py` → `FileChangedEvent` on `/ws/events`) attribute a file change to
  the session that made it. When a worker writes its unit's file, the overview learns of it
  through the same signal, with no third mechanism.

**The second brief correction — the persistence posture.** The brief says store the
overview in `.workbench/` "same posture as evidence/project-voice". Two mismatches, stated
so they are not built on: (1) M6 keeps validation **evidence in memory** and names
persistence to `.workbench/` an explicit *later* PR — so "same as evidence" would mean *not
persisted*, the opposite of what a durable project map needs; (2) **`project-voice` is not
in master** — no file in `server/`, `ui/`, the ROADMAP, `ARCHITECTURE.md` or `CLAUDE.md`
mentions it, so it is named here only as an orthogonal, not-yet-landed lane (a per-project
writing style, which the conductor would *read* if it lands and never owns). The real
shipped precedent for per-workspace JSON that survives a restart is
**`services/layouts.py`** (`.workbench/layouts.json`: atomic write, a read that never
raises) beside `shortcuts.md` — both local, gitignored, never global. The overview persists
on **that** precedent, and the reason it persists where evidence does not is in §1.

## Design constraints carried from the house rules

Every PR below inherits these without restating them:

- **Typed payloads only.** Every REST/WS body is a Pydantic model in
  `server/src/workbench_server/models/`, mirrored in `ui/src/types.ts`. `mypy --strict`,
  ruff, pytest green; new behaviour ships with tests. structlog only, never `print`;
  routers thin, logic in `services/`; `pathlib` for paths.
- **Registered capabilities.** A UI capability is one module (`ui/src/panels/X.tsx` +
  `ui/src/x.ts`) plus one line in `ui/src/tools.ts` — never an edit to `App.tsx`,
  `commands.ts` or `StatusBar.tsx`. DESIGN.md tokens, zero raw hex.
- **Plural by default; no singleton assumption without a reason in a comment.** The one
  place this plan asserts a singleton — one overview per workspace — carries that reason
  inline (§1), because a workspace *is* one project root, exactly as `layouts.json` is one
  arrangement store per workspace.
- **Agent-tool byte budgets + the AXI three shapes.** Any agent-facing tool is an
  `AgentToolSpec` in `services/agent_tools.py` with a measured `max_result_bytes`,
  `max_schema_bytes`, a description under `MAX_DESCRIPTION_CHARS` (800), and its own test
  asserting those ceilings. Every result truncates with a stated size and the argument that
  widens it, says "none" explicitly, and ends with the obvious next step. The conductor's
  tools go in a **separate `CONDUCTOR_TOOLS` tuple** — the orchestrator precedent — so no
  chat session pays their schema.

## 1. The overview — the one new artifact

**The overview is a maintained, per-project map the leader reads and workers update.** Its
whole reason for existing is the context-budget reality of a 20-unit project: the leader
cannot hold every full chapter or every full model in its context, so it holds a **rolling
per-unit digest** and reads the full text only of the unit it is currently reasoning about.

`models/conductor.py`, `services/conductor.py`, `routers/conductor.py`.

### The shape

```
UnitState = Literal[
    "discovered",   # the leader found it; nobody is on it
    "queued",       # on the work queue, awaiting a free slot/cap
    "assigned",     # a worker holds it (worker_id set)
    "summarised",   # a worker wrote back a fresh digest; awaiting accept/review
    "reviewed",     # the reviewer produced evidence (a ValidationResult ref)
    "accepted",     # the leader (or a human) accepted it
    "stale",        # an upstream unit it depends on changed after it was accepted
    "blocked",      # it cannot proceed; reason names why
]

class ProjectUnit(BaseModel):
    unit_id: str                      # server-minted, stable — the map's handle
    ref: str                          # workspace-relative path OR a logical id; DISCOVERED
    kind: str                         # a free label the leader chose ("chapter",
                                      # "model", "workbook", "section") — NEVER an enum,
                                      # because the structure is discovered, not hardcoded
    title: str
    summary: str = Field(max_length=MAX_UNIT_SUMMARY_CHARS)   # the rolling digest
    summary_updated_at: datetime | None
    state: UnitState
    worker_id: str | None = None      # the worker on it — a REFERENCE, not a copy of it
    depends_on: list[str] = []        # unit_ids this one leans on ("ch3 cites ch5")
    last_result_ref: str | None = None  # a ValidationResult id, when it was reviewed

class ProjectOverview(BaseModel):
    workspace_ref: str                # which project root this maps
    units: list[ProjectUnit]
    created_at: datetime
    updated_at: datetime
    version: int                      # bumped on every write; the UI merges on it
```

**Why one overview per workspace, stated as the rule requires.** A workspace is exactly one
project root (M5 item 5's switcher re-roots the whole server; there is one `Workspace`
object and one `root`). The overview maps *that* project, so it is singular for the same
reason `layouts.json` is — not because "there is only one conductor" (there may be several
conductor *sessions* over time), but because there is one project to map. It is therefore
keyed by the workspace, held by `ConductorService`, and looked up — never
`store.activeOverview` on the client, which would be the singleton smell the pane rules
forbid; the UI reads it by workspace and merges on `version`.

**Live worker assignment + state is a join, not a field.** `ProjectUnit.worker_id`
*names* a worker; what that worker is doing (its `SessionActivityEvent` rows, its
`session_status`, its `WorkerInfo.turns`/`outcome`) is read live from the surfaces that
already own it. The overview never copies a worker's state into itself — that is the exact
duplication `mission.ts` was re-scoped to forbid, and a second copy of "what is it touching"
would be a number to keep honest with the activity feed.

**Cross-unit dependencies are declared edges the leader tracks.** `depends_on` is how "ch3
cites a number ch5 owns" lives in the map. When unit 5 moves back to `summarised` after an
edit, every unit whose `depends_on` includes it flips to `stale` — a flag the leader and
the human see, **not** an automatic rewrite of the dependents (see deferrals). v1
dependencies are recorded by the leader as it maps and reintegrates; *inferring* them from
content is later.

**Where it is stored, and why it persists.** `.workbench/overview.json`, written through a
`services/layouts.py`-style atomic writer with a read that never raises, gitignored, local,
never global. It persists — unlike M6's in-memory evidence — because a 20-unit project
outlives a server process: the leader must be able to pick the map back up after a restart
rather than re-discover and re-summarise 20 files. The **summaries** persist; the full unit
texts never enter it (they are the files on disk). A corrupt or version-mismatched file
resolves to an empty overview the leader re-maps, the layouts precedent.

### The leader's loop, as agent tools

The conductor session is the orchestrator session **plus** a `CONDUCTOR_TOOLS` tuple. It
keeps the five orchestrator tools (it still spawns and reads workers) and adds the overview
tools, each honouring the byte budget + three shapes:

- **`map_project`** — the leader writes the discovered units (from `workspace_search` / the
  tree walk) into the overview. Idempotent: re-mapping a project reconciles against existing
  `unit_id`s by `ref`, so a 21st unit is an appended row, **data not code**.
- **`update_unit`** — set a unit's rolling `summary` and `state` (this is what a worker's
  reintegration calls, §3), and optionally its `depends_on`.
- **`assign_unit` / `next_unit`** — put a unit on the work queue, or ask the queue for the
  next one to dispatch given the caps (§2). `next_unit` returning "none — all units are
  accepted or in flight" is the AXI shape-2 explicit empty, not blankness.
- **`read_overview`** — the whole map as compact text (units, states, one-line summaries,
  the dependency edges), truncated worst-first with the argument that widens it, so the
  leader can re-orient in one call rather than reading 20 files.

## 2. Delegation and honest scaling

The leader assigns workers per unit; workers get isolated worktrees (#46) and do focused
work. **N units does not mean N simultaneous live agents** — that is the difference between
an honest design and a fantasy, so it is stated plainly and enforced by machinery that
already exists.

**The mechanism is a work queue dispatched in waves.** The overview's `queued` units are a
queue; the leader dispatches workers up to the binding cap, and as workers settle and
release their slots the leader dispatches the next wave. The cap is not the conductor's to
invent — `OrchestratorService._budget_refusal` already checks, in order and **before
anything is created**: the orchestrator's own `max_workers`, the fleet turn/dollar
ceilings, `SessionManager.max_concurrent` (`WORKBENCH_MAX_CONCURRENT_SESSIONS`), and
`_take_slot` then asks the worktree pool. Any one binding produces a named `SpawnRefusal`
the leader reads as "wait for a slot", not an error. The conductor adds **no new cap**; it
adds the *queue* that keeps units moving through the caps that exist.

**The prerequisite, named honestly.** The out-of-the-box ceilings are all **4** today
(`config.py`: `max_concurrent_sessions = 4`, `worktree_pool_size = 4`,
`orchestrator_max_workers = 4`). So a conductor over a 20-unit project runs in **five waves
of four**, not twenty at once — which is correct and works today. Raising simultaneity is
the **M5 item 9 remainder** — "raise `max_concurrent_sessions` from 4 to 8 and keep it
configurable" — which is *still open* (the ROADMAP records it as not landed; there is no
separately-named "fleet-headroom lane" in the tree, and this plan will not pretend one
exists). The conductor **names that raise as its scaling prerequisite** and needs nothing
from it to be correct: more headroom means fewer waves, never different code. The genuine
wall behind even a raised cap is measured, not assumed — eight live `claude` CLI processes
held 130–674 MB working set each (ROADMAP M5 item 9), so wide simultaneity is
resource-bound on the machine, which is exactly why the queue-and-waves shape is the design
rather than a limitation to apologise for.

## 3. Reintegration

A worker's output is summarised back into the overview and, optionally, passed through the
fresh-context reviewer before the leader accepts it; **the human approval gate stays the
decider for anything that matters.**

The sequence, all on seams that ship:

1. **The worker settles.** `OrchestratorService._pump` already drains a worker's own event
   queue and notices `turn_done` (and a worker's death as `agent_error` → `failed`). The
   conductor reads the worker's output with `read_worker`.
2. **Summarise back.** The leader writes a fresh digest for the unit via `update_unit`
   (state → `summarised`). The digest *replaces* the unit's old `summary`, so the leader's
   awareness of a 20-unit project stays one paragraph per unit no matter how much the worker
   wrote. This is the whole context-budget point of the overview.
3. **Optionally review.** The conductor runs the #82 `ValidationService` with the #126
   review check against **the worker's own slot** — the checkout is resolved through the
   existing `SlotLocator.slot_of(worker_id)` that `OrchestratorService` already implements
   and that `services/gates.py` and `services/review.py` already consume, so the reviewer
   reads exactly the tree the worker wrote and takes **no lease** on it. The review produces
   a `ValidationResult` (evidence, never approval); its id lands in
   `ProjectUnit.last_result_ref` and the unit moves to `reviewed`. The conductor commissions
   the review; it does **not** gain a tool that lets a *worker* commission its own reviewer
   — that door stays shut (#126's rule).
4. **Accept.** The leader marks the unit `accepted` — but for anything that matters the
   **human approval gate** (`POST /api/validation/{id}/approve`) is the decider: a
   `medium`-or-worse `ValidationResult` is a subject awaiting human approval exactly as M6
   defined, and an unattended conductor follows the objective-session **deny-and-log**
   policy rather than auto-accepting a flagged unit. Acceptance of a clean, low-risk unit
   the leader may do itself; acceptance over the reviewer's objection it may not.

Reintegration adds **no new authority**: the summary is the leader's, the evidence is the
reviewer's, the approval is the human's, and the overview only records which of the three
has happened.

## 4. Fluid and agnostic

The leader **maps** the project rather than being told its shape. Nothing in the models
names a `chapter`: `ProjectUnit.kind` is a free string the leader chose, `ref` is a path or
a logical id, and the unit set is a list. A thesis is a list of `chapter` units discovered
by walking `*.md`; an energy analysis is a list of `model` and `workbook` units discovered
by walking `*.py` and `*.xlsx` and grepping for the functions that own each number. Adding
a 21st unit is a `map_project` call that appends a row — **data, not code** — and the
dependency edges are the same whether they connect chapters or spreadsheets. The only thing
the conductor knows a priori is that a project is *a set of units with summaries and
dependencies*; what a unit *is* comes from the workspace.

## 5. Models added

Under PR 1 (`models/conductor.py`): `UnitState`, `ProjectUnit`, `ProjectOverview`,
`ConductorEvent` (the bus event carrying the whole overview snapshot — small, one project;
the `OrchestratorEvent`/`UsageEvent` "snapshot not delta" precedent, which makes the
reconnect path and the live path identical), `ConductorSnapshot` (`GET /api/conductor`
replay: the overview plus the work-queue state), and the caps
(`MAX_UNIT_SUMMARY_CHARS`, `MAX_UNITS`, `MAX_DEPENDENCY_EDGES` — a bounded map is a map the
UI can render and a leader can read in one tool call). The `CONDUCTOR_TOOLS` `AgentToolSpec`
entries live in `services/agent_tools.py` (append-only). No new model competes with
`WorkerInfo`, `ActivityEntry` or `ValidationResult`; the overview references them by id.

## 6. The PR sequence

Two PRs, each fake-first and independently shippable, with disjoint file ownership so
parallel lanes never collide. PR 1 is the hard prerequisite; PR 2 depends on it.

### PR 1 — the project-overview store + the leader's map/summarise/assign loop (server)

- **Owns:** `server/src/workbench_server/models/conductor.py`,
  `server/src/workbench_server/services/conductor.py`,
  `server/src/workbench_server/routers/conductor.py`, the `CONDUCTOR_TOOLS` additions in
  `services/agent_tools.py` (append-only, a separate tuple), the `.workbench/overview.json`
  atomic store (a small `OverviewStore` in `services/conductor.py`, the `layouts.py`
  pattern), the `create_app` wiring, and `server/tests/test_conductor.py`. Adds
  `ConductorEvent` to the event bus's typed union — the one shared-file line, exactly the
  addition `SessionActivityEvent` and `ValidationEvent` each made.
- **Builds:** the overview models; `ConductorService` holding the overview, the work queue,
  and the wave dispatcher — which **calls `OrchestratorService`** for spawn/read/stop and
  **asks** its caps rather than duplicating any of them; the `map_project` / `update_unit` /
  `assign_unit` / `next_unit` / `read_overview` tools honouring the byte budget + three
  shapes; the REST surface (`GET /api/conductor` replay, and a thin stop/reset); the bus
  event; persistence.
- **Composes with:** #63 (delegation substrate — reused, not rebuilt), #46 (the pool, via
  the orchestrator), #97 + the tree walk (discovery, already in the session toolset). Names
  the **M5 item 9 cap raise (`max_concurrent_sessions` 4→8) as its scaling prerequisite**,
  needing nothing from it to be correct.
- **Fake-first / CI:** fully. `WORKBENCH_FAKE_AGENT=1` drives a scripted leader that maps a
  fixture project, enqueues its units, and dispatches workers — no real model, no Office,
  no tokens. `test_conductor.py` proves: mapping is idempotent by `ref`; the wave dispatcher
  never exceeds the cap (a 5-unit project at cap 2 runs in ≥3 waves, never >2 workers live);
  a settled worker's `update_unit` replaces the digest and the summary length stays bounded;
  a `stale` cascade fires when a depended-on unit is re-summarised; `read_overview`
  truncates with the stated size + widening argument and says "none" when the map is empty;
  the overview round-trips through `.workbench/overview.json` across a simulated restart; a
  corrupt file resolves to an empty overview.

### PR 2 — worker reintegration + the reviewer hook + the Mission-Control overview surface (server hook + UI)

- **Owns (server):** the reintegration path in `services/conductor.py` — the call into the
  #82 `ValidationService` with the #126 review check, resolving the worker's checkout
  through the existing `SlotLocator.slot_of`; append-only, it **touches neither
  `services/review.py` nor `services/validation.py`**, only calls them. Extends
  `test_conductor.py`.
- **Owns (UI):** a **new registered capability** — `ui/src/panels/ConductorPanel.tsx` +
  `ui/src/conductor.ts` + one line in `ui/src/tools.ts` + the mirror types in
  `ui/src/types.ts`. This is the **overview surface**: the whole project rendered as a
  board of units — each unit's title, its `UnitState` as a §6.4 status pill, its rolling
  summary, its assignee (linking to that worker's `agent#<worker_id>` pane), its risk badge
  when reviewed (the M6 `RiskLevel` → semantic-token mapping, **no new colour**), and the
  dependency edges with a `stale` marker. Numeric summaries render in tabular figures
  (DESIGN.md). A status-bar reading (units not yet accepted) that hides at zero; one
  QuickBar command ("Open project overview"); no default `Alt` chord (the
  Scratchpad/Workspaces precedent), bindable from `shortcuts.md` the day it registers.
- **Deliberately does NOT edit `ui/src/mission.ts` or `MissionControl.tsx`.** The overview
  is its own panel, so the user sees the whole project *there*; a Mission Control card
  gaining a "part of project — open overview" link is a one-line join sequenced **last and
  rebased onto whatever owns those files**, exactly as M6 PR 4 handled the `mission.ts`
  boundary. Naming it here so the order is not rediscovered; it is not in PR 2's owned set.
- **Fake-first / CI:** fully. vitest against typed fixtures for `conductor.ts` and the
  panel; a Playwright journey added to `ui/e2e/` drives the whole thing against
  `WORKBENCH_FAKE_AGENT=1` — see the E2E moment below.

### The E2E moment (PR 2's acceptance demo)

Against `WORKBENCH_FAKE_AGENT=1` in a per-run temp workspace seeded with a small multi-file
fixture project: a leader **maps** the project into units, **dispatches workers in waves**
up to the cap (the test asserts the wave bound — never more than the cap live at once), each
worker writes a **summary** back and the overview reflects each worker's **state and
summary**, the optional review attaches a `ValidationResult` to a unit, and the user opens
the **overview surface** and sees the whole project advancing — every unit's state and
digest — **without opening a single worker session**. That last clause is the whole product:
the map is legible from one pane.

## 7. Honest deferrals (named, not implied)

- **True high-way concurrency.** N live agents for N units is **resource-bound** (the
  session cap, the pool size, and the measured per-agent memory). The queue-and-waves
  mechanism is the honest answer; this plan does not promise twenty simultaneous workers,
  and even the M5 item 9 cap raise (4→8, still open) narrows the waves without removing them.
- **Automatic cross-unit conflict resolution.** The overview **tracks** dependencies and
  **flags** a dependent `stale` when its upstream changes; it does **not** automatically
  reconcile the conflict (rewrite ch3 because ch5's number moved). Resolving a stale unit is
  a dispatched worker's job under the leader's direction, a human decision at the gate — not
  an automatic edit.
- **Fully-autonomous multi-wave runs without human checkpoints.** The #82 human approval
  gate stays the decider for anything `medium`-or-worse, and an unattended conductor follows
  the objective-session **deny-and-log** policy. A conductor that ran to completion accepting
  its own reviewer's objections is explicitly **not** what this builds.
- **Auto-inferred dependencies.** v1 records `depends_on` as the leader maps and
  reintegrates; discovering "ch3 cites ch5's number" from content is a later pass.
- **Multi-level hierarchy.** A worker that is itself a conductor is out of scope — one level,
  the orchestrator's own "a worker cannot be an orchestrator" rule.
- **Persisting per-unit full texts or evidence into the overview.** The overview holds
  **summaries and references** only; full texts are the files on disk and evidence stays in
  the M6 result map (whose own persistence to `.workbench/` is M6's named later PR, not
  smuggled in here).
- **The live-COM / real-model verification.** Everything above is green on CI with no Office
  and no real model. A conductor driving *real* Claude workers over a real project is the
  owner's manual verification, named as such, not a CI gate.

## 8. Composition summary (reuse, never duplicate)

- **#63 orchestrator** — the delegation substrate. The conductor is the orchestrator session
  plus the overview toolset; spawn/budget/reap/roster are reused whole.
- **#46 worktree pool** — worker isolation, via the orchestrator. Untouched.
- **agent-activity (M5 item 10) + Mission Control (#63)** — the per-agent "what is it
  touching" and "who asked for it" signals the overview **aggregates by reference**. The
  brief's "PR-D, queued" is corrected: both ship.
- **#82 validation frame + #126 reviewer** — reintegration's optional review and the one
  human approval gate. Called, never modified.
- **project-voice** — an orthogonal, **not-yet-landed** per-project style lane the conductor
  would *read* if it ships and never owns. Named honestly as absent from master today.
- **the M5 item 9 cap raise (4→8)** — the stated scaling prerequisite, still open; the
  conductor is correct without it and only runs fewer waves with it.
