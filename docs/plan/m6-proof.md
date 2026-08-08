# M6 — Proof: the validation pipeline, the first domain gate, and objective sessions

Status: **plan** (this document is the milestone's Plan PR; no feature code lands with
it). It turns the ROADMAP's M6 "Proof" bullet into a sequence of disjoint, fake-first,
independently shippable PRs with explicit file ownership, so parallel lanes never
collide. It is written against the repo as it stands after M5 (registry, panes, provenance,
Mission Control, the worktree pool, the usage service, the Office host with its fake-first
document bridge) and composes with those seams rather than duplicating them.

This is the milestone that makes the north star's third promise real: agents that **prove
their results with evidence**, and — as the first *domain* gate, the piece no generic
agent workspace has — a **workbook↔code numeric reconciliation** that respects the failure
modes of electricity-analyst work (MW vs MWh, EUR/MWh vs EUR/kWh, local-time/DST axes, no
look-ahead leakage).

## Design constraints carried from the house rules

Every PR below inherits these without restating them:

- **Typed payloads only.** Every REST/WS body is a Pydantic model in
  `server/src/workbench_server/models/`, mirrored in `ui/src/types.ts`. `mypy --strict`,
  ruff, pytest green; new behaviour ships with tests.
- **Thin routers, logic in services.** `routers/*.py` validate and delegate;
  `services/*.py` hold the work. structlog only, never `print`. `pathlib` for paths.
- **Registered capabilities.** A UI capability is one module (`ui/src/panels/X.tsx` +
  `ui/src/x.ts`) plus one line in `ui/src/tools.ts` — never an edit to `App.tsx`,
  `commands.ts` or `StatusBar.tsx`. DESIGN.md tokens, zero raw hex.
- **Plural by default.** Nothing assumes it is the only one of itself. A validation
  result is *attached to* a resource (an agent output, a file), never stored as
  `store.activeValidation`. Panes that render validation are views onto a result they do
  not own, keyed by the result's own id.
- **Agent-tool byte budgets + the AXI three shapes.** Any agent-facing tool is an
  `AgentToolSpec` in `services/agent_tools.py` with a measured `max_result_bytes`,
  `max_schema_bytes`, a short description under `MAX_DESCRIPTION_CHARS`, and its own test
  asserting those ceilings. Every result truncates with a stated size and the argument
  that widens it, says "none" explicitly, and ends with the obvious next step.

## 1. The validation pipeline

**A validation is a service, not a controller.** `services/validation.py` runs a
validation and returns a typed result; `routers/validation.py` stays thin
(`POST /api/validation/run`, `GET /api/validation/{id}`, `GET /api/validation` for load
and reconnect); `models/validation.py` owns the wire types. This mirrors the Office-host
split exactly (`routers/office_host.py → OfficeHostService → HostBackend`), and for the
same reason: the risky, domain-specific work lives behind a seam that is testable with no
external program.

### The `ValidationResult` model (`models/validation.py`)

A validation produces one `ValidationResult`, the atom the rest of the milestone composes
with:

```
RiskLevel     = Literal["pass", "low", "medium", "high", "blocked"]
CheckOutcome  = Literal["pass", "warn", "fail", "skipped"]

class EvidenceItem(BaseModel):
    kind: Literal["numeric", "gate", "diff", "log", "artifact"]
    label: str                       # "workbook↔code reconciliation", "ruff", "pytest"
    outcome: CheckOutcome
    detail: str                      # one line the human reads
    payload_ref: str | None = None   # id into a bounded per-kind payload store, not inline

class ValidationResult(BaseModel):
    validation_id: str               # server-minted, ULID-like — the stable handle
    subject: ValidationSubject       # what was validated (see below)
    risk: RiskLevel                  # the badge value — the max severity across evidence
    evidence: list[EvidenceItem]
    summary: str                     # one sentence for the badge tooltip and the card
    created_at: datetime
    completed_at: datetime | None    # None while running
    truncated: EvidenceTruncation | None  # AXI shape 1: stated cut + how to get the rest

class ValidationSubject(BaseModel):
    kind: Literal["session_output", "file", "objective"]
    ref: str                         # session_id, workspace-relative path, or objective_id
    label: str
```

`risk` is **derived**, never asserted by the caller: it is the max severity over
`evidence`, with an explicit table (`fail → high` or `blocked` for a gate that could not
run at all, `warn → low/medium`, all `pass → pass`). A result with no evidence is a
**blocked** validation that says why — never a silent green.

### How a validation *runs*

`ValidationService.run(spec: ValidationSpec)` dispatches to one or more **checks**. A
check is a small typed protocol (`ValidationCheck`) so the pipeline is a registry of
checks, not a hardwired sequence — the same "registry, not a pipeline" instinct as the
tool registry:

```
class ValidationCheck(Protocol):
    id: str
    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]: ...
```

The **first shipped check is the reconciliation gate** (§2). The staged-review checks the
ROADMAP names (ruff/mypy/pytest on an isolated worktree, adversarial fresh-context review,
intent-directed E2E) are later checks that plug into the same protocol — deliberately
**out of scope for M6's first cut** and called out as such in the PR sequence, because they
depend on driving the worktree pool and a fresh agent, which is a second milestone of work.
M6 ships the *frame* (result model, service, badge, evidence gallery, objective binding)
proven end-to-end by the one domain check that is fully CI-verifiable.

### How evidence attaches to an agent's output, and how it composes

Provenance (`services/provenance.py`) already answers "which session wrote this file".
Validation answers the next question — "and is what it wrote *correct*" — and it attaches
at the same two anchors provenance already established, so the two compose without a third
mechanism:

- **To a session's output.** When an agent finishes a turn that named a workbook (the
  provenance signal already exists — a `Write`/`Edit` tool call naming a path), a
  validation can be run against that session's output and its `ValidationResult` published
  on the shared event bus as a `ValidationEvent`, exactly as `FileProvenanceEvent` and
  `SessionActivityEvent` are. `GET /api/validation` replays for a reconnecting client, the
  usage/activity precedent. **No singleton:** results are held in a bounded map keyed by
  `validation_id` (LRU, same posture as provenance's 500-path bound), and a session may
  have several.
- **To a file.** The reconciliation subject *is* a workbook path, so a `ValidationResult`
  for `subject.kind == "file"` sits beside the provenance entry for that same path — the
  editor's provenance bar (DESIGN.md §6.1) gains a sibling risk badge, not a competing one.

**Composition with Mission Control (#63) is a join, not a new authority.**
`ui/src/mission.ts` already computes `buildCards` by joining four services (activity,
usage, pool, `session_status`) and "computes the join and nothing else". Validation adds a
**fifth read**: the latest `ValidationResult` for each session's output. The card renders a
risk badge from it; it derives no new number. This is the same re-scope discipline the
board was built under — two live-fleet views that disagree is worse than one — so the
validation risk on a card and the risk in the Review panel are the same result object by
construction, never two computations of "how risky".

### How the risk badge surfaces in the UI

One registered capability, `ui/src/panels/ReviewPanel.tsx` + `ui/src/validation.ts` + one
line in `ui/src/tools.ts`. It contributes:

- **A Review panel** (plural — you can review two subjects side by side) rendering the
  **evidence gallery**: one row per `EvidenceItem`, its `CheckOutcome` as a §6.4 status
  pill, its detail line, and an expand to the bounded payload (the reconciliation table,
  a gate's captured log). Numeric mismatches render in tabular figures (DESIGN.md typography
  rule for numbers), the reconciliation table's own idiom.
- **The risk badge itself**, a §6.4 status pill. Its tokens map straight onto the existing
  semantic ramp — **no new colour is invented** (DESIGN.md §2.4 spends the accent
  elsewhere, and §2.5/§2.6 already carry the full status vocabulary):
  `pass → --success`, `low → --info`, `medium → --warn`, `high → --error`,
  `blocked → --agent-idle` (the "could not judge" grey, distinct from a red *fail*). The
  dot-only variant (§6.4) is what appears on a Mission Control card and a background tab;
  it always carries an `aria-label`, because colour is never the only signal.
- **A status-bar reading** that hides at zero (§6.7): a count of subjects at `medium` or
  worse awaiting review. Quiet bar, nothing to check.
- **One QuickBar command** ("Review validation…") and no `Alt` chord by default (the
  Scratchpad/Workspaces precedent: a registered chord is one the user's own `shortcuts.md`
  cannot have, and this is not a reflex). A chord is bindable from `shortcuts.md` the day
  the command is registered (M5 item 14's "bind anything").

**The one mandatory human approval** the ROADMAP requires lives here: a `medium`-or-worse
result is a subject the Review panel shows as *awaiting approval*, and
`POST /api/validation/{id}/approve` records the human decision (approver, timestamp,
optional note) as a typed `ValidationApproval` on the result. A stale approval (the result
was superseded) is **404**, not a 200 read as a decision — the Mission Control permission
precedent. The push/PR babysitter the ROADMAP mentions is **explicitly deferred past M6's
first cut** (it needs the staged-review checks that are themselves deferred); M6 ships the
approval gate, not the auto-push loop.

## 2. The first domain gate — workbook↔code numeric reconciliation

The proof of the moat, and the piece that must be **fake-first / fully CI-testable with no
Office**. This is the crucial design decision: the gate reads the `.xlsx` **directly with
`openpyxl`**, deterministically, on the same machine that runs CI — it does **not** go
through the live-Office COM document bridge. The COM path (reconciling against a workbook a
user has open and unsaved, with live formula results) is the *optional, later* path, and it
slots in behind the same reader protocol, mirroring `OfficeDocumentReader` /
`FakeDocumentBridge`. M6 ships the openpyxl reader; the COM reader is a named later PR.

`services/reconciliation.py` (a `ValidationCheck` from §1), `models/reconciliation.py`.

### Precise inputs

`ReconciliationSpec`:

```
class ExpectedValue(BaseModel):
    cell: str                    # A1, e.g. "Sheet1!D14" (sheet-qualified) or "D14"
    expected: float
    unit: str                    # "MWh", "MW", "EUR/MWh", "EUR", "%", "" (dimensionless)
    label: str | None = None     # "Day-ahead revenue, 2024-03-31 hour 02"

class ReconciliationSpec(BaseModel):
    workbook: str                # workspace-relative path to the .xlsx (jailed via safe_path)
    expectations: list[ExpectedValue]
    default_tolerance: Tolerance
    per_cell_tolerance: dict[str, Tolerance] = {}   # cell -> override
    timezone: str | None = None  # IANA name for a time-indexed workbook, e.g. "Europe/Oslo"
```

The **code-computed expected values** are supplied *as data* — a `cell → expected` mapping
with units — not by executing arbitrary user code inside the server (that would be a shell
in a JSON body, the same threat the `shortcuts.md` never-execute doctrine exists to stop).
Where the expected values *come from* is the analyst's own script or notebook; the gate's
contract is "here is what the code says, here is the workbook, tell me if they agree". A
later PR can add a convenience: an agent tool that *computes* expectations from a named
function, but the reconciliation core takes values, on purpose.

### Tolerance handling

`Tolerance` is a small typed union so the analyst states intent rather than a magic number:

```
class Tolerance(BaseModel):
    abs: float | None = None     # absolute, in the value's own unit
    rel: float | None = None     # relative, fraction (0.001 = 0.1%)
    # match if |a-b| <= abs  OR  |a-b| <= rel*|expected|; at least one must be set
```

Both may be set (either satisfies — a floor for near-zero values plus a relative band for
large ones, the standard way to compare floats that span orders of magnitude). A missing
tolerance is a **spec error reported up front**, never a silent exact-equality that fails
on the last bit of a float.

### Unit- and timezone-aware comparison (the domain failure modes)

This is what makes it a *domain* gate and not a spreadsheet-diff:

- **Units are compared, not assumed.** Each `ExpectedValue` carries a unit; the workbook
  cell's unit is taken from the spec (the analyst names it) — the gate does **not** guess
  from a header string. A comparison across incompatible dimensions (expected `MWh`, cell
  declared `MW`) is a **`fail` with an explicit reason** ("unit mismatch: expected MWh, cell
  is MW"), not a coerced number. A convertible pair (`EUR/MWh` vs `EUR/kWh`, `MWh` vs `kWh`)
  is converted through a small explicit factor table and the conversion is **named in the
  evidence** so a silent ×1000 can never hide inside a "pass". This is the single most
  common silent bug in this domain and the gate refuses to be the place it lives.
- **Time-indexed workbooks are DST-aware.** When `timezone` is set and an expectation's
  `label`/cell resolves to a timestamped row, the gate aligns rows by *local wall-clock
  time in that zone*, so a workbook whose 31 March column has 23 hourly rows and whose 27
  October column has 25 lines up against code that computed the same local hours — the
  23-/25-hour-day problem the visual scene graph already handles on real Nordic clock-change
  dates (ROADMAP M5 item 3). A UTC-everywhere comparison that silently drops the duplicated
  02:00 hour or invents a non-existent 02:00 is exactly the boundary bug this catches. The
  gate reuses the same zone handling; it does not invent a second calendar.
- **No look-ahead / leakage is a first-class check, not a comment.** For a workbook that
  declares a forecast-vs-actual structure, an optional `causality` field on the spec names
  the "as-of" column; an expectation that reconciles a forecast cell against a value that
  could only be known *after* its as-of timestamp is a **`warn` the analyst must clear**,
  surfaced in the evidence as a named leakage suspicion rather than a green tick. This is
  deliberately conservative (it flags, it does not block) because leakage is a property of
  the analyst's intent the gate can only suspect.

### The evidence shape

`ReconciliationReport` (the payload behind an `EvidenceItem` of kind `numeric`):

```
class CellComparison(BaseModel):
    cell: str
    label: str | None
    expected: float
    actual: float | None         # None when the cell was empty/unreadable
    unit: str
    delta: float | None          # actual - expected, in the compared unit
    outcome: CheckOutcome        # pass / warn / fail / skipped
    reason: str | None           # "unit mismatch…", "outside tol 0.1%…", "empty cell"

class ReconciliationReport(BaseModel):
    workbook: str
    matched: int
    mismatched: int
    total: int
    comparisons: list[CellComparison]   # bounded window; see truncation below
    truncated: EvidenceTruncation | None
```

Which cells matched, which mismatched, and **by how much** (`delta` in the compared unit)
— that is the evidence the analyst reviews and the agent reads.

### The agent-facing tool (byte budget + AXI three shapes)

`office_reconcile`, a new `AgentToolSpec` in `services/agent_tools.py` (a **separate**
concern from `office_read`; it does not widen the office toolset's schema cost for chat
sessions unless registered for them). It hands the model a **compact text** result, the
AXI list-payload idiom `office_read` already uses:

- The tool takes the workbook path and the expectations (small), returns a summary line
  plus a **bounded window** of mismatches, worst-first: `"3 of 40 cells mismatch. …"`.
- **Truncate with a stated size + the argument that widens it** (shape 1): "showing 10 of
  40 comparisons, worst first; pass detail=full for the rest" — `max_result_bytes` sized
  from the measured window, and its own test asserting the ceiling.
- **Say "none" explicitly** (shape 2): all-match returns "All 40 cells reconcile within
  tolerance." — not an empty string a model reads as either clean or broken.
- **End with the next step** (shape 3): a mismatch result ends by naming the workbook path
  and the first mismatching cell, the obvious place to look.

`max_description_chars`, `max_result_bytes`, `max_schema_bytes` all asserted in
`test_agent_tools.py` alongside the existing tools.

### The `openpyxl` dependency

This is the **one new runtime dependency** the milestone adds, and it is justified rather
than reflexive: reading an `.xlsx` deterministically with no Office installed is exactly
what makes the gate CI-verifiable, and openpyxl is the standard pure-Python xlsx reader
(no native build, no transitive weight of note). The alternative — hand-parsing the OOXML
zip with the stdlib — is reinventing openpyxl for no gain. It is added to `server`'s deps,
read-only (`load_workbook(..., read_only=True, data_only=True)` so we read *computed* cell
values, not formula strings), and the PR body states the justification per the house rule.
`data_only=True` reads the last values Excel cached, which is the correct source for
reconciling *numbers*; a workbook never opened in Excel (no cached values) is a **blocked**
reconciliation that says so and names the fix (open and save it once), never a silent
all-`None`.

## 3. Objective sessions

**An objective is a validated goal bound to a session.** Today a session runs until the
user stops it; an *objective session* runs toward a stated goal, and **pass/fail evidence
closes it** — the loop's exit condition is a `ValidationResult` reaching `pass`, not a
turn count alone.

`models/objectives.py`, `services/objectives.py`, `routers/objectives.py`.

### What an objective is

```
class Objective(BaseModel):
    objective_id: str
    session_id: str              # the session working toward it (see reuse below)
    goal: str                    # the human-stated goal
    validation_spec: ValidationSpec  # how "done" is proven — the reconciliation gate, e.g.
    caps: ObjectiveCaps          # server-enforced loop bounds
    state: Literal["running", "passed", "failed", "stopped", "capped"]
    last_result: ValidationResult | None
    created_at: datetime

class ObjectiveCaps(BaseModel):
    max_iterations: int
    max_tokens: int | None
    max_wall_clock_s: int | None
```

The caps are **enforced in code**, not prompted — the ROADMAP's "iteration/token/wall-clock
caps in code, unattended deny-and-log permission policy". The token cap reads the same
`UsageService` per-session figure Mission Control's budget refuses on (one authority), and
the unattended permission policy is **deny-and-log** — an objective session running while
the user is away never auto-allows (the orchestrator's "never auto-allow shell" threat
model, applied to the whole session): a blocked permission is denied and logged, not
granted, so an unattended loop cannot escalate itself.

### How it binds to a validated goal, and how evidence closes it

Each iteration: the session works a turn, the bound `validation_spec` runs (§1), and the
resulting `ValidationResult` is stored as `last_result` and published. `pass` → the
objective closes `passed`; a cap hit → `capped` (with the cap and the env var that raises
it, the SpawnRefusal idiom); an explicit stop → `stopped`. The **evidence that closes it is
the same `ValidationResult` object** the Review panel renders — an objective does not derive
its own notion of done.

### Reuse of the named-session store (#70), not a new store

M5 item 15 / #70 (**Detachable working sessions**) makes a session a first-class,
nameable, detachable thing that outlives the client. An objective is **not a second session
store** — it is a small typed record that *references* a `session_id` in that store and adds
three things it does not have: a goal, a validation spec, and enforced caps. Concretely:

- The session itself (its transcript, its worktree lease, its live agent) stays owned by
  the named-session store; `services/objectives.py` holds only the `Objective` records,
  keyed by `objective_id`, each naming a `session_id`. Closing/detaching the session is the
  named-session store's job; the objective follows it.
- The telemetry strip the ROADMAP names is a **reading**, not a store: the objective panel
  renders iteration count, token spend (from `UsageService`) and elapsed against the caps —
  the usage-meter precedent (a view, in-memory, nothing written to `.workbench/`).
- This keeps the "plural, no singleton" rule: there is no `store.activeObjective`; an
  objective is looked up by id, and two may run at once (two sessions, two goals).

The UI is one more registered capability (`ui/src/panels/ObjectivePanel.tsx` +
`ui/src/objectives.ts` + one line in `tools.ts`): a panel showing running objectives with
their live `last_result` badge and telemetry, a QuickBar command to start one against the
focused session, and a status reading (count of running objectives) that hides at zero.

## 4. The PR sequence

Five PRs, each fake-first and independently shippable, ordered by dependency, with explicit
disjoint file ownership so parallel lanes never collide. PR 1 is the only hard prerequisite;
PRs 3 and 4 can run in parallel once PR 2 lands; PR 5 depends on the named-session store
(#70) landing, not on this milestone's other PRs.

### PR 1 — The validation frame (server) · foundation, blocks 2–5

- **Owns:** `server/src/workbench_server/models/validation.py`,
  `server/src/workbench_server/services/validation.py`,
  `server/src/workbench_server/routers/validation.py`, its registration in `create_app`,
  and its tests (`server/tests/test_validation.py`). Adds `ValidationEvent` to the event
  bus's typed union (the one shared file it touches — the same one-line addition
  `SessionActivityEvent` and `FileProvenanceEvent` made).
- **Builds:** the `ValidationResult` / `EvidenceItem` / `ValidationSubject` models, the
  `ValidationCheck` protocol, `ValidationService` with a **no-op/echo check** so the frame
  is exercisable before the reconciliation check exists, the REST surface, the bus event,
  the bounded result map, and the approval endpoint.
- **Fake-first / CI:** fully. No Office, no external anything — a test check that returns
  canned evidence proves risk derivation, truncation, the bus replay, and approval/404.
- **Test story:** `test_validation.py` — risk is the max severity; empty evidence is
  `blocked`; replay after "reconnect"; a stale approval is 404; the AXI truncation shape.

### PR 2 — The reconciliation gate (server) · depends on PR 1

- **Owns:** `server/src/workbench_server/models/reconciliation.py`,
  `server/src/workbench_server/services/reconciliation.py`, its wiring as a
  `ValidationCheck`, the `office_reconcile` `AgentToolSpec` addition in
  `services/agent_tools.py` (append-only), the `openpyxl` dependency, and
  `server/tests/test_reconciliation.py` + the tool-budget assertions in
  `test_agent_tools.py`.
- **Builds:** the openpyxl reader, tolerance handling, unit conversion table + mismatch
  reasons, DST-aware row alignment, the leakage `warn`, the `ReconciliationReport` evidence,
  and the agent tool honouring the byte budget + three shapes.
- **Fake-first / CI:** fully. Tests carry **small committed `.xlsx` fixtures** built with
  openpyxl in a fixture helper (`server/tests/reconciliation_fixtures.py`) — a clean sheet
  that reconciles, a sheet with a ×1000 unit trap, and a 23-/25-hour DST sheet on real
  Nordic clock-change dates (2024-03-31, 2024-10-27), asserted on both sides. **No COM, no
  Excel.**
- **Owner's hands / real Office:** **none required for the gate itself.** The live-COM
  reader (reconciling an *open, unsaved* workbook through the document bridge) is a **named
  later PR, out of M6 scope**, and *that* one needs the desktop shell + installed Excel for
  its manual verification. M6's gate is green on CI alone.

### PR 3 — The Review panel + risk badge (UI) · depends on PR 1

- **Owns:** `ui/src/panels/ReviewPanel.tsx`, `ui/src/validation.ts`, the mirror types in
  `ui/src/types.ts`, one line in `ui/src/tools.ts`, and tests
  (`ui/src/validation.test.ts`, `ui/src/panels/ReviewPanel.test.tsx`).
- **Builds:** the evidence gallery, the risk badge mapped onto existing semantic/agent-status
  tokens (zero raw hex), the awaiting-approval affordance, the status-bar reading, the
  QuickBar command. Plural-safe: two Review panes reviewing two subjects, asserted through a
  save/restore round trip (the plural-tool test every plural capability owes).
- **Fake-first / CI:** fully — vitest against typed fixtures; the E2E journey (PR added to
  `ui/e2e/`) drives it against `WORKBENCH_FAKE_AGENT=1` with a canned `ValidationResult`,
  no Office.
- **Disjoint from PR 4:** PR 3 owns the *panel*; PR 4 owns the *Mission Control join*. They
  share only `ui/src/validation.ts` (PR 3 creates it; PR 4 imports the read helper) — so PR
  4 rebases onto PR 3, stated here so the order is not rediscovered.

### PR 4 — Composition: Mission Control + provenance badge (UI) · depends on PR 3

- **Owns:** the fifth read in `ui/src/mission.ts` (append a validation lookup to
  `buildCards`), the risk dot on the card in `ui/src/panels/MissionControl.tsx`, and the
  sibling risk badge on the editor's provenance bar. Tests extend `mission.test.ts`.
- **Builds:** the *join*, no new number — the card's risk and the panel's risk are one
  result object. The provenance bar gains a risk sibling without competing with the
  attribution line (DESIGN.md §6.1).
- **Fake-first / CI:** fully — vitest, extends the existing Mission Control fixtures.
- **Note:** touches `mission.ts` and `MissionControl.tsx`, which the task's in-flight-lane
  boundary flags — so this PR is **sequenced last among the UI PRs and rebased onto
  whatever owns those files**, never landed concurrently with a Mission Control change.

### PR 5 — Objective sessions (server + UI) · depends on PR 1 and on #70 landing

- **Owns:** `server/src/workbench_server/models/objectives.py`,
  `server/src/workbench_server/services/objectives.py`,
  `server/src/workbench_server/routers/objectives.py`, `ui/src/panels/ObjectivePanel.tsx`,
  `ui/src/objectives.ts`, one line in `ui/src/tools.ts`, mirror types, and tests
  (`test_objectives.py`, `objectives.test.ts`).
- **Builds:** the `Objective` record referencing a named session, the in-code caps
  (iteration/token/wall-clock), deny-and-log unattended policy, the pass/fail close driven
  by a `ValidationResult`, the telemetry strip as a reading, the panel/command/status.
- **Fake-first / CI:** fully — `WORKBENCH_FAKE_AGENT=1` drives a scripted session to a
  passing reconciliation and asserts the objective closes `passed`; a cap hit closes
  `capped` with the env var named; an unattended permission is denied-and-logged.
- **Depends on #70:** it references the named-session store rather than building one, so it
  lands after #70. If #70 is not yet in, PR 5 waits — it is the one PR here with an external
  dependency, and that is stated rather than worked around by minting a second store.

### What needs real Office / owner hands vs. what is fully CI-verifiable

- **Fully CI-verifiable (no Office, no owner):** PRs 1–5 in their entirety, including the
  reconciliation gate against committed xlsx fixtures and the DST cases. This is the whole
  point of the openpyxl-first design — the moat's first domain gate is green on a machine
  with no Office installed.
- **Needs real Office + owner verification (deferred past M6's first cut):** the live-COM
  reconciliation reader (reconciling an open, unsaved workbook through the document bridge)
  and the staged adversarial-review checks (ruff/mypy/pytest on a fresh worktree + a
  fresh-context review agent + intent-directed E2E). Both are named here as the milestone's
  *later* work so M6's shippable core is not blocked on them, exactly as the Office host
  sequence shipped its fake backend before its Rust and COM.

## What this plan deliberately defers (so the scope is honest)

- The **push/PR babysitter** with bounded retries (needs the staged-review checks).
- The **staged adversarial fresh-context review** and **intent-directed E2E with recorded
  screenshots/video** checks — the frame accepts them as `ValidationCheck`s, but building
  them is a second pass.
- The **live-COM reconciliation reader** — openpyxl is the M6 path; COM is later and needs
  the owner's Office box.
- **Persisting validation results / objectives to `.workbench/`** — like provenance,
  activity and usage, M6 keeps this in memory (a restart forgets), and persistence is a
  named later PR, not smuggled in.
