# The productivity loops — five PRs that turn features into trusted, ambient systems

Status: **plan** (this document is the direction's Plan PR; no feature code lands with
it). It turns the owner's ratified product direction of **2026-08-09** — confirmed twice,
all five items A–E to be implemented — into five fake-first, independently shippable PRs
with explicit file ownership, so parallel lanes never collide. They are disjoint from each
other; §6 draws the four ordering constraints they have against PRs already open, each one
checked against that branch's diff rather than its title.

The direction in one sentence, in the owner's terms: **close the loops, do not add
features; the wedge is the proof engine.** Every item below is a loop that the repo has
already built both ends of and never joined. Nothing here is a new capability. Reading the
code rather than the roadmap, that is literally true five times over:

| Loop | What is already built | What is missing — the open end |
|---|---|---|
| **A** live-reconcile trust | `ReconciliationCheck` + a `WorkbookReader` Protocol (`services/reconciliation.py`); a live COM read/write bridge (`ShellDocumentBridge`, #92) | The Protocol has exactly one implementation, and it reads **disk**. A workbook open with unsaved edits reconciles green against stale bytes. |
| **B** spec-from-code | `ReconciliationSpec` as data; a bounded, timed-out subprocess runner (`services/gates.py`, #115); the watcher bus | Nothing turns *the analyst's own function* into `expected` values, and nothing re-runs on save. Every reconciliation is hand-fired. |
| **C** evidence persistence | `ValidationResult`, `EvidenceItem`, `PayloadStore`, `ValidationApproval`, the bus event + `GET /api/validation` replay (#82) | All of it is in memory. A restart forgets the proof, and there is no way to hand it to anyone. |
| **D** agent-activity surface | `SessionActivityEvent` at tool-call granularity, jailed and coalesced (`services/activity.py`); Mission Control; the pane system | The signal reaches one panel. The tree, the board rows and the status bar do not read it, and an agent-driven pane open **steals focus**. |
| **E** CLI composability | `workbench-cmd`, the command relay, `CommandManifestItem.takes_params`, `run_command`'s `params` | `takes_params` is `False` for every command and `params` is documented "reserved: current commands ignore it". One invocation costs ~4.6 s, so nothing is scriptable in practice. |

This reopens the **scope freeze** (ROADMAP, 2026-08-06) on the one ground the freeze itself
names — *a decision the owner makes*. Recorded rather than waved through, because the value
of that rule is that using it leaves a mark.

**One follow-up this document cannot do itself:** `ROADMAP.md` needs a *Change requests*
entry for 2026-08-09 and a link to this file, in the shape M6 and M7 already have
("Plan of record: `docs/plan/…`"). This lane owns exactly one file and deliberately does not
touch `ROADMAP.md`, which another lane is writing; the entry is the first PR-A commit's job,
or a one-line docs PR, and it is named here so it is not forgotten.

---

## Corrections to the brief, made loudly

This plan was commissioned with a design already decided, and the instruction was to ground
every claim in the real code and correct any mismatch out loud rather than write the brief
back. Six corrections, each with what was actually read or measured. **None of them changes
what any of the five items is for**; four of them make an item cheaper and two make it
correct.

**1. PR-D needs no new event and no `PostToolUse` hook — the signal already exists, one
moment earlier.** The brief asks for "one lightweight `ActivityEvent` on the existing bus at
tool-call granularity `{session_id, tool, target}`, emitted from the `PostToolUse` seam
(`permission_broker`/`sdk_factory`)". `models/activity.py` already publishes exactly that:
`SessionActivityEvent` carries `SessionActivity{session_id, title, folder, kind, entries[]}`
and each `ActivityEntry` is `{entry_id, tool, summary, target, started_at, settled_at, ok}`,
jailed workspace-relative, coalesced at 250 ms, fanned out on `/ws/events`. It is fed from
`AgentSession._handle_message` through the `ActivityObserver` seam (`agent_sessions.py`
~line 648) at the moment a tool call is **announced**, and patched by `note_tool_settled`
when the result lands. There is **no `PostToolUse` hook anywhere in this repo** (grep:
`permission_broker.py` registers `PreToolUse` and nothing else). Adding one would be a
second source of a fact one source already publishes — the thing `services/activity.py`'s
own docstring refuses ("provenance stays the only authority on authorship") — and it would
fire *after* the edit, which is the wrong moment for a surface whose whole claim is
**under active edit**. So PR-D adds no event, no model and no hook. What it adds is the
three surfaces that never read the feed, one fix to `describe()`, and the focus fix below.

**2. Consequence: PR-D does not depend on the staged-review PR2 lane — and that lane is not
a branch anyone can rebase onto anyway.** The brief sequences PR-D after SR-PR2 because
SR-PR2 rewrites `build_context_bridge`'s tool selection and `_AUTO_ALLOWED` in
`services/sdk_factory.py` and touches `services/permission_broker.py`
(`docs/plan/staged-review.md` §"PR 2 — owned files"). With correction 1, PR-D touches
neither file. Two things worth saying plainly, because a plan that names a ref people cannot
fetch wastes their afternoon:

- **SR-PR2 has no branch and no PR number.** Checked 2026-08-09:
  `git ls-remote --heads origin '*staged-review*'` returns exactly `plan/staged-review` and
  `m6/staged-review-gates` (that is PR1, merged as #115), and `gh pr list --state open` has
  no entry for it. SR-PR2 exists *only* as a section of `docs/plan/staged-review.md` — the
  proof being that `services/sdk_factory.py` on `origin/master` still carries the
  unconditional `_AUTO_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep"]` (line 60) that
  the section says it will replace. Nowhere in this document does a branch name for it
  appear, and nobody should go looking for one.
- The ordering constraint PR-D really had was **#122** (`fix/activity-workspace-reroot`),
  which owns `services/activity.py` — the file PR-D's one server-side edit lands in. **#122
  merged on 2026-08-09** (`4a709da`), so that constraint is now discharged: PR-D branches
  from a master that already has it, and the seam it needs survives the re-rooting work
  unchanged (`describe`, `_PATH_KEYS` and `_DETAIL_KEYS` are all still there, at
  `services/activity.py:103–164`). The staged-review track was never relevant to it either
  way.

**3. dockview v7's origin tags do not control focus, and focus is the actual bug.**
Verified: `ui/src/registry.ts::openToolPanel` ends with an unconditional
`api.getPanel(id)?.api.setActive()`, so **an agent-driven `run_command` open steals focus
today** — that is a real, reproducible defect, not a hypothetical. #118's origin tags are
`onWillMutateLayout` / `onDidMutateLayout` carrying `'user' | 'api'`, adopted to stop
`layouts.json` answering its own echo; they classify a *layout mutation*, and there is no
focus in them. PR-D therefore needs both halves and the plan says which does what: an
`{ focus?: boolean }` option on `openToolPanel` that skips `setActive` is what stops the
theft, and the v7 tag is what keeps the resulting `api`-origin mutation from being written
back as a layout the user never chose. Only the first is the fix.

**4. A live COM workbook read hands back tz-aware datetimes with a *wrong* offset — #120
would refuse every row, and "normalising" them corrupts the fall-back day.** Measured on
this machine (Windows 11, `W. Europe Standard Time`, pywin32 311) by driving a real private
Excel over COM against a workbook holding a naive `2024-10-27 02:00` — the Nordic fall-back
hour the DST gate exists for:

```
disk (openpyxl, data_only)  datetime.datetime(2024, 10, 27, 2, 0)        tzinfo=None
live (Range.Value over COM) pywintypes.datetime(2024, 10, 27, 2, 0,
                                tzinfo=TimeZoneInfo('GMT Standard Time', True))
  .replace(tzinfo=None)     -> 2024-10-27 02:00   correct wall clock
  .astimezone().replace()   -> 2024-10-27 03:00   WRONG BY AN HOUR
```

Three things follow, and PR-A pins all three in tests. (a) The offset COM attaches is not
the machine's zone and carries no information — it must be **dropped, never honoured**;
`.astimezone(...)` is the plausible-looking normalisation that silently moves every hourly
row, on exactly the date the gate exists for. (b) `_as_local_datetime`'s `OffsetNotAllowed`
refusal (#120) stays exactly where it is: the live reader normalises **at its own seam** and
never hands a tz-aware value across, so the string-timestamp refusal keeps protecting the
case it was written for. (c) The disk reader already yields naive local wall clock, so the
two implementations of `WorkbookReader` agree by construction rather than by luck.

**5. A live reader cannot reuse `office_com.excel_grid`.** That function runs every cell
through `_cell_text`, which stringifies — an integral float becomes `"1234"` and a date
becomes text. `document_window.Grid` is `dict[(row, col), str]` all the way down, because it
was built for a *reading* window. Reconciling parsed-back strings would throw away the
precision a tolerance band is about, and would turn the date cell of correction 4 into a
string `_as_local_datetime` then refuses. PR-A adds a values-preserving read
(`office_com.excel_range_values`) beside the text one rather than changing it — `office_read`
is a shipped contract and its shape does not move.

**6. `takes_params` and `params` already exist, and `params` is dropped on the floor.**
`models/commands.py::CommandManifestItem.takes_params` ships as advisory and
`ui/src/commandRelay.ts::buildManifest` hardcodes `takes_params: false` for every command;
`CommandInvokeRequest.params` is documented "forwarded to the window verbatim. Reserved:
current commands ignore it". It is stronger than "ignore": `handleInvoke` calls
`executeCommandById(event.command_id, …)` and `executeCommandById` calls `command.run()`
with **no arguments at all** — `event.params` never reaches the registry. So PR-E is not
adding a field to either model; it is closing a wire that is already laid and connected at
neither end.

---

## Design constraints carried from the house rules

Every PR below inherits these without restating them:

- **Typed payloads only.** Every REST/WS body is a Pydantic model in
  `server/src/workbench_server/models/`, mirrored in `ui/src/types.ts`. `mypy --strict`,
  ruff and pytest green; new behaviour ships with tests.
- **Thin routers, logic in services.** structlog only, never `print`. `pathlib` for paths.
  Windows-first, tested on PowerShell.
- **Registered capabilities.** A UI capability is one module plus one line in
  `ui/src/tools.ts` — never an edit to `App.tsx`, `commands.ts` or `StatusBar.tsx`.
  DESIGN.md tokens, zero raw hex. zustand only; a capability-private `create()` lives in
  the capability's own module (the `validation.ts` / `usage.ts` precedent).
- **Plural by default.** Nothing assumes it is the only one of itself. Every plural tool
  ships the two-instances-through-save/restore test, and an unscoped `page.locator` on a
  pane-internal class fails review.
- **Agent-tool byte budgets + the AXI three shapes.** Any agent-facing tool is an
  `AgentToolSpec` with measured `max_result_bytes` / `max_schema_bytes`, a description under
  `MAX_DESCRIPTION_CHARS` (800), and its own test asserting all three. Every result
  truncates with a stated size and the argument that widens it, says "none" explicitly, and
  ends with the obvious next step.
- **Bug fixes start with an end-to-end reproduction.** PR-A and PR-D each carry one (a
  green reconciliation against stale disk; a focus theft), and each repro becomes the
  regression test.

---

## 1. PR-A — Live-reconcile trust

> **Value sentence: no false PASS.** The reconciliation gate may never report green about a
> workbook whose numbers it did not actually read.

This is the wedge, and today it has a hole big enough to walk the whole product through.
`ReconciliationCheck` opens the `.xlsx` with `OpenpyxlReader` and compares. The user's
workbook is docked in a panel, in a real Excel, with an hour of unsaved edits in it. The
gate reads the file on disk and says **pass** — about numbers that are not the numbers.
Measured, against a real Excel, on the same probe as correction 4:

```
live B1 after an edit      = 9999.0      Workbook.Saved = False
disk B1 (openpyxl, again)  = 1234.5      <-- what the gate reconciles against
live C1 (=B1*2)            = 19998.0
disk C1                    = None        (never opened in Excel: no cached value)
```

A gate that passes there is worse than no gate. It is the silent green the whole M6
milestone was built to refuse, arriving through the milestone's own front door.

### The seam already exists — it has one implementation

`services/reconciliation.py` declares `WorkbookReader` as a Protocol and says in its own
module docstring that "the COM path ... is an optional later PR that slots in behind the
same `WorkbookReader` protocol". PR-A is that PR. It is a **second implementation and a
gate**, not a redesign.

```
class WorkbookReader(Protocol):          # today
    def cell_value(self, sheet, cell) -> float | int | str | None: ...
    def column_pairs(self, sheet, ts_column, value_column, start_row) -> list[tuple[...]]: ...

    # PR-A adds one method, on both implementations:
    def provenance(self) -> ReadSource: ...
```

`DiskWorkbookReader` is today's `OpenpyxlReader`, renamed for symmetry (one rename, one
module). `LiveComWorkbookReader` is new: it holds an `OfficeHostService` handle for the
docked workbook and reads through the #92 bridge, on the shell backend's single apartment
thread, exactly as `ShellDocumentBridge` already does.

### The front gate — dirty and unreadable is BLOCKED, never a pass

Before a single cell is read, the check asks the office host whether the workbook is docked
(`OfficeHostService._live_host_for(path)`, which already exists) and, if it is, whether it
is saved. Three outcomes and no fourth:

| Docked? | `Workbook.Saved` | Live read available? | Verdict |
|---|---|---|---|
| no | — | — | read disk, `EvidenceItem` sourced `file @mtime` |
| yes | `True` | either | read **live** if available, else disk; source named either way |
| yes | `False` | yes | read **live**, source `live @timestamp + calc-state` |
| yes | `False` | **no** | **BLOCKED**, naming the remedy — never a pass against stale disk |

The blocked line names the fix in the `NO_SLOT_DETAIL` register the gates check already
uses: *"`model.xlsx` is open in Excel with unsaved changes and the live reader is not
available here (it needs the desktop shell). Save the workbook, or run this from the desktop
shell, and re-run — reconciling the file on disk would judge numbers you have already
changed."*

**And here the frame has a gap that PR-A has to close rather than route around.** A check
cannot currently *say* "blocked". `CheckOutcome` is `Literal["pass", "warn", "fail",
"skipped"]`, and `derive_risk` maps `skipped → low`; the only way a result reaches `blocked`
today is by producing **no evidence at all**, which throws away the very sentence that makes
a refusal useful. So PR-A adds `"blocked"` to `CheckOutcome` and one row to each of the four
tables that switch on it — `_OUTCOME_RISK` (`blocked → blocked`) and `_summarize`'s parts
tuple in `services/validation.py`, `_OUTCOME_SEVERITY` in `services/reconciliation.py`, and
the `StatusVisual` map in `ui/src/validation.ts`. Four named places, each a one-line append,
and every one of them would otherwise be a `KeyError` on the first blocked run — which is
exactly why they are enumerated here instead of discovered later. The difference between
`low` and `blocked` is the difference between a badge someone scrolls past and a badge that
stops them, and the tests assert both directions.

`Workbook.Saved` and `Application.CalculationState` are both confirmed readable over COM on
the owner's configuration (probe output, correction 4: `Saved=True → False` across an edit,
`CalculationState=0` for Done). `CalculationState` matters because a workbook mid-calculation
is a third state: `pending`/`calculating` is **not** a pass either, it is a `skipped` line
saying "Excel has not finished calculating; re-run in a moment".

### Every EvidenceItem names its source

Today a `numeric` evidence line reads `workbook↔code reconciliation (model.xlsx)`. That is
no longer enough once there are two places the numbers can come from, so `ReadSource` rides
the report and the label:

```
class ReadSource(BaseModel):
    kind: Literal["live", "file"]
    read_at: datetime                       # server-minted, naive local (see below)
    # live only:
    calculation: Literal["done", "pending", "calculating"] | None = None
    saved: bool | None = None               # was the docked book clean at read time
    # file only:
    mtime: datetime | None = None
    # file only, and load-bearing: a workbook openpyxl read with no cached values
    cached_values: bool | None = None
```

`ReconciliationReport` gains `source: ReadSource`, and the grouped `EvidenceItem.detail`
ends with it: *"read live at 14:02:11, calculation done, workbook unsaved"* or *"read from
disk, modified 13:41:08"*. A reader who cannot tell which of two numbers a green badge is
about does not have proof; they have a colour.

`read_at` is **naive local wall clock**, matching the module's own contract, and it is
minted server-side. It is never taken from the COM object (correction 4).

### Fake-first, and the CI story

`FakeDocumentBridge` already mints content from the *document's name* — the `FAILURE_TRIGGERS`
precedent its docstring names — so PR-A extends the same mechanism rather than adding a
knob: a workbook whose name contains `dirty` is scripted `Saved=False`, one containing
`calculating` reports `CalculationState=calculating`, and the default is clean and done. A
`nolive` name makes `ready()` return `False` for the workbook path, which is how the
**BLOCKED** row of the table above is reached in CI with no Office and no window. The whole
front gate, both readers and every row of that table are green under
`WORKBENCH_OFFICE_FAKE=1`.

### PR-A — owned files, models, tests

- **Owns (new):** `server/tests/test_reconciliation_live.py`.
- **Owns (edits):** `services/reconciliation.py` (the `provenance()` method on the Protocol,
  `DiskWorkbookReader` rename, `LiveComWorkbookReader`, the front gate),
  `models/reconciliation.py` (`ReadSource`, `source` on `ReconciliationReport`),
  `models/validation.py` + `services/validation.py` + `ui/src/validation.ts` (the
  `"blocked"` `CheckOutcome` and its four tables),
  `services/office_host/document_bridge.py` (+`workbook_status`, +`read_cells`,
  +`read_columns` on the Protocol), `real_document_bridge.py`, `fake_document_bridge.py`,
  `office_com.py` (`excel_range_values`, `workbook_saved`, `calculation_state`),
  `ui/src/types.ts` + `ui/src/panels/ReviewPanel.tsx` (render the source line).
- **Models added:** `ReadSource`; `LiveWorkbookStatus` in `models/office_bridge.py`.
- **Test story.** The **E2E repro first, red before the fix** (`ui/e2e/reconcile-live.spec.ts`):
  with `WORKBENCH_OFFICE_FAKE=1`, open `budget-dirty.xlsx`, edit a cell through
  `office_write`, run the reconciliation against the *old* expectations and assert the
  result is **not** `pass` — that test fails on master today, which is the reproduction. Then
  in `test_reconciliation_live.py`: the four-row table above, one test each; a tz-aware
  value from the live reader is normalised by `replace(tzinfo=None)` and **never** by
  `astimezone` (asserted on `2024-10-27T02:00` with both folds, the regression test for
  correction 4); a live read and a disk read of the same clean workbook produce identical
  comparisons and *different* `ReadSource`s; `calculation=pending` is `skipped`, not `pass`;
  and the front gate is asserted to have consulted the host **before** opening the file, so a
  later edit cannot reorder it into a read-then-check.
- **Owner's hands:** none for the PR. PR-A lands the correction-4 probe as
  `scripts/dev/probe_live_com.py` — a self-cleaning script that drives one private Excel and
  reports whether it leaked a process — because the tz behaviour it pins belongs to pywin32
  and Office, not to us, and a version bump on either is the thing that would change it. It
  is a **probe, not a gate**: no CI job runs it, and it is named as such so nobody mistakes
  its absence from the quality gate for an oversight.
- **Composes with — two hard ordering constraints against open PRs**, of the same kind as
  PR-D's #122 (which has since merged) and stated in the same place for the same reason:
  PR-A's owned-file list above
  *is* the intersection, so a lane that starts without reading this writes its tests against
  a version of `services/reconciliation.py` that will not exist when it merges.
  - **#114** (`m6/gate-closure-check`) rewrites `services/reconciliation.py` by +120/−38.
    It publishes three symbols PR-A's module will be sitting next to — `_Unit`→`Unit`,
    `_UNIT_TO_BASE`→`UNIT_TO_BASE`, `_is_ambiguous`→`is_ambiguous_local` — because the new
    `services/market_check.py` imports `UNIT_TO_BASE, convert, is_ambiguous_local` from it,
    and it leaves `OpenpyxlReader` under its old name at both the class and the call site
    (verified: `git diff origin/master...origin/m6/gate-closure-check` touches no
    `OpenpyxlReader` line). **PR-A rebases onto #114** and performs the
    `OpenpyxlReader`→`DiskWorkbookReader` rename against #114's file. This is the dangerous
    order to get wrong, precisely because getting it wrong is *quiet*: the two edits are on
    different lines, so git merges them without a conflict and hands back a module whose
    surviving call site names a class that no longer exists. If #114 lands second instead,
    PR-A's rename must be re-applied to whatever new references #114 brought with it, and
    the merge is not believed until `uv run pytest server/tests/test_reconciliation*.py`
    and `test_market_check.py` are both green on the merged tree.
  - **#107** (`m4/office-host-powerpoint`) modifies `document_bridge.py`,
    `real_document_bridge.py`, `fake_document_bridge.py`, `office_com.py` (+305) and
    `service.py` (+172) — every office-host file PR-A edits, plus the `service.py` whose
    `_live_host_for` PR-A's front gate calls. The overlap is
    additive on both sides (#107 adds `read_powerpoint` to the `DocumentBridge` Protocol;
    PR-A adds `workbook_status`, `read_cells`, `read_columns` to the same class body), so
    the resolution is mechanical — but "two lanes appending methods to one Protocol" is a
    conflict every single time, not a risk. **PR-A rebases onto #107 if #107 is still open
    when PR-A starts**, and takes the free win noted under PR-D: #107's slide vocabulary is
    already in the file PR-A is editing.

  Neither constraint changes PR-A's design, its test story or its value sentence — they
  change only when it starts. They are written down because the failure mode the ownership
  tables in this document exist to prevent is precisely a lane calling itself disjoint on
  the strength of a PR title while its own file list says otherwise. Every "disjoint" in §6
  was checked against the branch's diff for that reason.

---

## 2. PR-B — Spec-from-code

> **Value sentence: ambient CI for workbooks.** Save the workbook; seconds later the chip
> flips. Nobody fires a check.

Reconciliation takes `expected` values **as data**, on purpose — `models/reconciliation.py`
is explicit that executing user code from a JSON body is the thing the never-execute
doctrine forbids. That decision stands. What it leaves open is that somebody has to produce
the data every time, which is why the gate is fired by hand and therefore fired rarely.

PR-B closes it the way the repo already closes this class of problem: the *file* names the
callable, the *server* owns the argv, and a **one-time content-hash approval** is what
turns a file in a folder into something that may run.

### The spec file

`.workbench/reconcile/<name>.toml`, TOML because it is a file a person edits:

```toml
workbook = "models/se3-dispatch.xlsx"
timezone = "Europe/Oslo"

[default_tolerance]
rel = 0.001
abs = 0.5

[[check]]
cell     = "Summary!D14"
callable = "se3.reporting:annual_revenue"
unit     = "NOK"

[[check]]
range     = "Hours!A2:B8761"          # timestamp column, value column
callable  = "se3.dispatch:hourly_mwh"
unit      = "MWh"
value_unit = "kWh"                     # the named x1000, not a silent one
```

`callable` is `module:function` **within the workspace**. A cell entry expects a scalar; a
`range` entry expects an iterable of `(naive local datetime, float)` pairs, which is exactly
`TimeIndexSpec`'s vocabulary — so a spec compiles into a `ReconciliationSpec` and **nothing
downstream of the check changes**. That is the whole architectural claim of this PR: it
produces `ExpectedValue` and `TimeExpectation` records and hands them to the gate that
already exists.

### Running the callable — the #115 machinery, a different posture, stated

`services/gates.py` is reused for its *mechanism* and deliberately not for its *catalog*.
`models/gates.py` says it plainly — "there is no field anywhere in this module through
which a JSON body can reach an argv, a cwd or a path" — so a caller-named callable can
never be a `GateCommand`. PR-B takes four things from #115 and leaves the fifth:

- **`_BoundedCapture`** — head buffer plus ring tail, bounded *while the pipe drains*. A
  spec whose function prints a 500 MB dataframe costs the window, not the memory.
- **`GateRunner`** as the protocol shape, with its own `SubprocessSpecRunner` /
  `FakeSpecRunner` pair (`WORKBENCH_RECONCILE_FAKE=1`, the `WORKBENCH_GATE_FAKE` posture).
- **`create_subprocess_exec`, never `shell=True`**, `stdin` used for the payload and nothing
  interpreted by a shell.
- **The per-run timeout and the "no exit code" branch**, which covers both "killed at the
  ceiling" and "never started" and names the setting that raises it
  (`WORKBENCH_RECONCILE_TIMEOUT_S`).

The argv is **fixed and server-owned**: `uv run python -m workbench_server.spec_entry`. The
only variable input is the spec document itself, delivered on stdin as JSON; `spec_entry`
imports the named callable, calls it with no arguments, and writes back a compact JSON
envelope of values on stdout. It is a new, tiny module with no server imports — it runs in
the *workspace's* interpreter, not the server's.

**The one place PR-B's posture differs from `gates`, and why.** `ToolchainGateCheck` refuses
a session with no pool slot rather than falling back to the live workspace root, because
running `pytest` there would judge the user's unsaved changes and write caches into the
folder they are editing. PR-B **runs in the workspace root on purpose** — the workbook and
the analyst's own code are there, and there is nothing else to point at. That is a real
widening and it is paid for by the approval below — and specifically by that approval being
keyed to the **code**, not merely to the spec that names it, which is what makes it the
"explicit one-time trust prompt, which is a feature" that both
`services/gates.py::workspace_config_refusal`
and `docs/plan/staged-review.md` name as the price of admission for reading a config file
out of a folder. PR-B is that feature, built; it does **not** unlock `.workbench/gates.json`,
which stays refused.

### The approval is keyed to the spec **and to the code it names**

The permission-broker pattern, applied to a document instead of a tool call. The spec is not
the thing that runs, so the spec's bytes cannot be the whole key:

- No spec ever runs on folder-open. Opening a workspace with twenty specs in it runs
  nothing and prompts nothing; the panel lists them as *unapproved*.
- Approving one records a **composite digest** → `{approver, timestamp, covered[]}` in the
  machine's app-data dir (**not** in `.workbench/`, for the reason `RecentsStore` spells
  out: a trust decision belongs to the person, and a trust record inside the folder it
  authorises is a trust record an attacker can write).
- **Editing the spec revokes it, and so does editing the code.** Both are the same rule,
  because both are the thing being trusted.
- The decision resolves through the same `ask_permission` future Mission Control's board
  already answers, so there is one permission surface and not a second one.

**Why the composite digest, stated as the defect it prevents.** `blake2b(spec bytes)` alone
would be a hole, and a bad one: the bytes in `.workbench/reconcile/dispatch.toml` are not the
code that runs. The code lives in `se3/reporting.py`, which the spec only *names*. Approve
once, then edit `annual_revenue`'s body, and the spec file — and therefore the approval —
is untouched; the watcher would keep firing the gate on every workbook save, running whatever
that function now contains, unattended, forever, with no second trust decision. An approval
that authorises arbitrary code it never hashed is not a trust prompt, it is a trust prompt's
shadow. So the digest folds in, in declared order:

1. the spec file's own bytes;
2. **the bytes of every module file the spec's `callable` entries resolve to** — `module:function`
   is resolved to a path under the workspace root at approval time, and the resolution itself
   is recorded, so a `callable` that later resolves to a *different* file is a mismatch too;
3. **the workspace-local import closure the previous run actually used** (below).

**Re-verified at every run, never only at approval time.** The watcher-triggered path
recomputes the digest before it spawns anything and compares. A mismatch does not silently
re-run and does not silently skip: it is a **`blocked`** outcome (the `CheckOutcome` PR-A
adds) naming *which* file changed and offering re-approval — the same posture as PR-A's
dirty-and-unreadable row, for the same reason. A gate that runs code nobody re-approved is
the silent green again, wearing a different hat.

**The transitive-import boundary, admitted rather than glossed.** Hashing the two files above
does not cover a helper the callable imports — `se3/reporting.py` can keep its bytes and pull
its arithmetic from `se3/_helpers.py`. Static import analysis would be a guess (the imports
can be conditional, or `importlib`). So `spec_entry` closes it with a fact instead of a guess:
after the callable returns, it walks `sys.modules`, keeps every module whose `__file__`
resolves under the workspace root, and returns those paths and their digests in the same
stdout envelope as the values. The service folds them into the stored approval's `covered[]`.
The honest consequence, both halves: the **first** approved run of a spec is covered only to
the entry points it named, and from the second run on, changing *any* workspace file that
actually participated revokes it. The panel renders `covered[]`, so what the approval covers
is a list a person can read, not a claim they have to take on faith. PR-C's export renders
the same list for the same reason: a proof that names a spec hash but not the code the hash
was supposed to stand for is the shadow again, on paper.

**What "approved" therefore means**, in one sentence the panel copy uses verbatim: *this
spec, running exactly this code, on this machine, until either changes* — never "this spec
name is trusted to run unattended forever". That bound is what makes it fair to quote
`workspace_config_refusal`'s "explicit one-time trust prompt, which is a feature" in the
paragraph above; a prompt that did not cover the code would not have earned the quotation.

- The decision resolves through the same `ask_permission` future Mission Control's board
  already answers, so there is one permission surface and not a second one.

Once approved, `services/watcher.py`'s `FileChangedEvent` for the spec's **workbook** —
debounced 500 ms on top of watchfiles' own 200 ms — re-runs the gate. Keyed on the workbook
and never on "any save", which matters because `.workbench/` is **not** in `IGNORED_DIRS`:
every file the app writes under it already publishes a change event, so a trigger on any
save would let PR-C's own evidence write re-fire the gate that produced it.

### PR-B — owned files, models, tests

- **Owns (new):** `models/reconcile_spec.py`, `services/reconcile_spec.py`,
  `routers/reconcile_spec.py`, `server/src/workbench_server/spec_entry.py`,
  `server/tests/test_reconcile_spec.py`, `ui/src/reconcileSpec.ts` (the **descriptor** —
  store, command, status chip; this is the module `tools.ts` imports, so it is on the eager
  launch path), `ui/src/panels/SpecPanel.tsx` (the **body**, behind a dynamic `import()` +
  `React.lazy` with a warm on idle, the `settings.ts` pattern, so none of it enters the
  entry chunk — `ui/e2e/perf/bundle.spec.ts` asserts what is *inside* it, not only what it
  weighs), `ui/e2e/reconcile-spec.spec.ts`.
- **Owns (append-only, one line each):** `tools.ts` registration; the router in
  `create_app`; `WORKBENCH_RECONCILE_*` in `config.py`.
- **Models added:** `ReconcileSpecFile`, `SpecCheck`, `SpecApproval` (the composite digest,
  the approver, and `covered: list[CoveredSource]`), `CoveredSource` (`path`, `digest`, and
  how it entered — `spec` / `callable` / `imported`), `SpecRunReport`, `SpecState` (the
  panel's list), `SpecApprovalRequest`.
- **Test story.** Compilation: a spec becomes a `ReconciliationSpec` whose `value_unit`
  carries the declared x1000 (so the conversion is *named* in the evidence, not silent). The
  security half, **six** ways, because the trust boundary is the whole reason this PR is
  allowed to run workspace code at all: a spec that has never been approved runs **nothing**
  on workspace-open (asserted by a runner spy with zero calls); editing an approved spec by
  one byte revokes it; **editing the body of the module the `callable` resolves to revokes
  it just as hard, and the test that pins that is the one that would have caught the
  spec-bytes-only design** — approve, rewrite `annual_revenue`, save the workbook, and assert
  the watcher path produced `blocked` with zero runner calls, not a re-run; a `callable`
  edited to name a *different* module is a mismatch, not a fresh implicit approval; a helper
  module that participated in run 1 revokes the approval when it changes before run 2
  (the `sys.modules` closure, asserted through `covered[]`); and a `callable` that raises,
  hangs or prints 50 MB becomes one `fail` evidence line with the setting that raises the
  ceiling, never an exception that sinks the run. Determinism: `FakeSpecRunner` scripts
  values so the whole watcher→gate→chip path is green in CI with no user code executed
  anywhere.
- **The E2E moment (`reconcile-spec.spec.ts`): save → chip flip.** Approve a spec, write a
  wrong number into the workbook fixture through the files API, and assert the spec chip
  goes `pass → fail` **without any user action**, inside a bounded wait. That single
  assertion is the entire product claim of PR-B, and it is the one that must not be faked.

---

## 3. PR-C — Evidence persistence and export

> **Value sentence: handable proof.** A result you can restart into, and a one-page report
> you can send to someone who was not there.

Today `ValidationService` holds results in an LRU and says so honestly: "In memory only — a
restart forgets every result". Every plan since M6 has named persistence as a later PR
(`m6-proof.md`, `staged-review.md`). This is that PR, and it does not invent a mechanism: it
adds a **disk source** to the #82 event-and-replay pattern the frame already has.

### Append-only JSONL, with the payloads beside it

```
.workbench/validation/
  results-2026-08.jsonl      one ValidationResult per line, append-only
  approvals.jsonl            one ValidationApproval per line, append-only
  payloads/<kind>/<ref>.json byte-budgeted; a payload over budget is written truncated
                             with its EvidenceTruncation, never dropped silently
```

Append-only because the alternative is rewriting a file that is the record of what was
approved. A corrupt or partial trailing line is skipped with a `structlog` warning and the
rest of the file is kept — the `services/layouts.py` posture: losing evidence costs a
reading, and guessing at it costs a wrong verdict.

**Replay on boot** reads the newest file, newest-first, up to the frame's existing
`MAX_RESULTS` (500), rehydrates the same bounded map, and then serves `GET /api/validation`
unchanged. Clients need no new call: the endpoint they already read on reconnect simply
stops being empty after a restart. `set_workspace_root` re-reads from the new root's
`.workbench/` instead of only clearing — which is the *correct* re-rooting behaviour and is
what the current clear-only implementation was standing in for.

**Retention** is a new field on `WorkbenchSettings` (`validation_retention_days`, default
90; `0` means keep forever). Two honest notes rather than one convenient omission: (a) the
settings document is **app-data scoped and deliberately owns no `set_workspace_root`** and
is deliberately absent from `create_app`'s rootables, so this knob is a machine-level
preference about how much disk to keep, while the *files* it governs are workspace data —
that split is stated in the panel copy, not papered over; (b) adding one field with a
default does **not** bump `SETTINGS_VERSION`, because `models/settings.py` promises exactly
that ("a document written by an older version simply arrives with the new field at its
default").

### The export

A `ValidationResult` is a wire type. A **proof** is something a person hands over. The
export renderer turns one result into a one-page Markdown document:

```
# Reconciliation evidence — models/se3-dispatch.xlsx
Result val_9f3c…    risk: pass    2026-08-09 14:02 (Europe/Oslo)

Workbook   models/se3-dispatch.xlsx   sha256 4f2a…  (as read)
Source     live, read 14:02:11, calculation done, workbook unsaved     [PR-A]
Spec       .workbench/reconcile/dispatch.toml  digest 1c7e…  approved 2026-08-07 by mathi
           covering  dispatch.toml, se3/reporting.py, se3/dispatch.py, se3/_helpers.py
Checks     3 evidence lines: 2 pass, 1 warn
  pass  workbook↔code reconciliation — all 8,760 cells within tolerance (rel 0.1%)
  pass  ruff check .  — exit 0 in 3.2 s
  warn  as-of causality — 4 forecast cells reconcile against values dated after their
        as-of timestamp
Approval   mathi, 2026-08-09 14:06 — "checked the four flagged hours by hand"
```

Every line comes from a field that already exists or that PR-A adds — with one exception
that is stated rather than assumed: the `Spec` block is PR-B's `SpecApproval`, and PR-C can
land first. So the renderer treats it the way it treats a missing approval, per AXI shape 2:
a result that came from no spec prints `Spec  —  not run from a spec`, never a silently
absent section, and the `covering` line is the approval's `covered[]` verbatim rather than
anything PR-C computes. Nothing in the document is computed twice. It reaches the user two
ways, both of which are the registered-capability
pattern rather than new plumbing: an **Export** action on a Review pane, and a registered
command `validation.export` — which is also, not by accident, the first real customer for
PR-E's parameterised commands (`validation.export{validation_id}`).

### PR-C — owned files, models, tests

- **Owns (new):** `services/validation_store.py`, `services/evidence_export.py`,
  `models/validation_store.py`, `server/tests/test_validation_store.py`,
  `server/tests/test_evidence_export.py`, `ui/e2e/evidence-persist.spec.ts`.
- **Owns (edits):** `services/validation.py` (the disk source behind `_store`, replay in
  `start`, re-read in `set_workspace_root`), `models/settings.py` +
  `ui/src/panels/Settings.tsx` (one field, one control),
  `ui/src/panels/ReviewPanel.tsx` (the Export action), `ui/src/validation.ts` (the command).
- **Models added:** `StoredValidation` (the JSONL line, version-stamped),
  `RetentionPolicy`, `EvidenceExport`.
- **Test story.** A written result reads back byte-identical through the model round trip; a
  truncated trailing line is skipped and the preceding lines survive; a payload larger than
  the byte budget is written **truncated with its `EvidenceTruncation` intact** rather than
  dropped; retention deletes by age and never deletes an approved result inside its window;
  a workspace switch loads the other project's evidence and not this one's. The export test
  asserts the report names the workbook, the source provenance, every check and the approver;
  that a spec-run result names the spec digest **and the `covered[]` files it stood for**;
  and that the two absent cases — no approval, and no spec at all — each say so explicitly
  rather than omitting the section (AXI shape 2, applied to a human reader). The
  no-spec case is asserted with PR-B's models absent, so the test is honest about PR-C
  landing first.
- **The E2E moment: restart → evidence survives.** Run a validation, kill the server,
  restart it, and assert the Review pane shows the same `validation_id`, the same risk and
  the same approval — with no client action but a reconnect.

---

## 4. PR-D — The agent-activity surface

> **Value sentence: trusted delegation.** You can leave the room, and when you come back the
> window tells you where the agents are without you opening anything.

Per correction 1, the feed exists and reaches exactly one panel. Per correction 3, an
agent-driven open steals your focus. PR-D is **one server-side edit, three surfaces and one
focus fix** — and it is the uniquely-ours item, because what that server-side edit stops
throwing away is a *document locus* no other agent workspace can name.

### (a) `describe()` learns the Office locus

`services/activity.py::describe` reads `_PATH_KEYS` then `_DETAIL_KEYS` and stops. An
`office_write` call arrives as `{path: "report.docx", paragraph: 3, content: "…"}`, so the
feed renders `office_write: report.docx` and throws the paragraph away — and an
`office_write` into a workbook renders the path and drops `Sheet1!D14`. PR-D adds a third,
narrow pass: after a path key matches, if the same input carries `paragraph`, or `sheet`
plus `cell`, the summary becomes `report.docx ¶4` / `model.xlsx Sheet1!D14`. `target` is
unchanged — still the jailed path the UI may open, never the locus — because *naming* and
*opening* are separate answers and `describe`'s docstring already says so. The paragraph
index is rendered 1-based, because ¶0 is not a thing a person has ever counted.

This is the only server-side edit in PR-D. It lands in the file **#122** owned — which
merged on 2026-08-09, so what was a rebase is now just "start from master".

### (b) The tree pulses where an agent is editing

`FileTree` rows get a decaying tint on the row whose path appears as a *running*
`ActivityEntry.target`, plus the §2.6 agent dot. **A decay, not a loop**, and that is a
DESIGN.md constraint rather than a preference: §5.4 states the working dot is "the only
looping animation in the app", and §5.5 restricts animation to `transform`, `opacity`,
`background-color`, `border-color`, `color`, `outline-color` — enforced against every
stylesheet by `ui/e2e/perf/motion.spec.ts` and `motion.test.ts`. So the pulse is
`background-color` on `--motion-tint-slow`, and it is a **new row in the §5.4 table**, which
PR-D adds (DESIGN.md is binding; a motion decision that is not in that table does not
exist). Under `prefers-reduced-motion` it keeps its duration as a tint, per §5.6 rule 4.

Bounded by construction, for the reason §5.4 already gives about the tree: at most
`MAX_ENTRIES_PER_SESSION × MAX_SESSIONS` running entries exist (8 × 16), so at most a
handful of rows are ever tinted, and the perf lane's 5,005-file fixture asserts the tint
costs no per-row work on the other 5,000.

### (c) Mission Control rows carry a current-target line

`ui/src/mission.ts` "computes the join and nothing else" over four services. PR-D adds the
newest running `ActivityEntry`'s summary as a line on the card — **readable without opening
the row**, which is the whole request. It derives nothing: it renders a string
`services/activity.py` already jailed and capped at 100 chars.

### (d) Status bar, in the V4 vocabulary

DESIGN §6.7 already specifies the right end of the bar: needs-attention count, working
count with a pulsing dot, last turn cost. PR-D adds no region and no chip — the working
count gains a tooltip naming the current targets, and the count keeps hiding at zero. A
quiet bar stays quiet.

### (e) The focus fix — the E2E repro

`openToolPanel(api, tools, toolId, { focus = true } = {})`. When an agent's `run_command`
opens a pane, `focus: false`: the panel is added, its tab flashes once on the tint channel,
and **the caret stays where the user left it**. dockview v7's `onDidMutateLayout` origin tag
(#118) is what lets `Layouts.tsx` treat the resulting `api`-origin mutation as one that does
not merit writing `layouts.json`.

### PR-D — owned files, models, tests

- **Owns (edits):** `services/activity.py` (`describe` only — on top of #122, now merged),
  `ui/src/registry.ts` (`openToolPanel`'s option), `ui/src/dock.ts` (`openPanel` passthrough),
  `ui/src/panels/FileTree.tsx` + its stylesheet, `ui/src/mission.ts` +
  `ui/src/panels/MissionControl.tsx`, `ui/src/activity.ts`, `DESIGN.md` §5.4 (one row).
- **Owns (new):** `ui/e2e/agent-ambient.spec.ts`.
- **Models added: none.** Correction 1 is the reason, and its absence is the strongest thing
  about this PR.
- **Test story.** The **E2E repro first, red before the fix**: open two panes, put the caret
  in one, drive an agent-originated `run_command` that opens a third, and assert focus never
  moved — that fails on master. Then: `describe` renders `report.docx ¶4` for an
  `office_write` and still jails the path (a locus must not become a way to leak one, so the
  outside-workspace case is asserted to render `(outside the workspace)` with no locus);
  `mission.ts` renders a running target and clears it on settle; the tree tints exactly the
  running rows and nothing else through a save/restore round trip with two FileTree panes
  (the plural test); and the perf lane asserts the tint adds no measurable per-row cost on
  the 5,005-file fixture.
- **Composes with:** #122 (**merged 2026-08-09** — it owned `services/activity.py`, and
  PR-D now simply starts from a master that has it); **#119**
  (**must rebase** — despite its "store permission flags" title it edits `ui/src/mission.ts`
  by +40/−11 inside `useMissionStore.answer`, plus `ui/src/mission.test.ts`; PR-D adds the
  current-target line to the same file, so this is the same different-functions-one-file
  situation as D/E on `registry.ts`, and it gets the same treatment: D goes second); #118
  (the origin tag — PR-D works without it, and writes one spurious layout save per agent
  open until it lands, which the plan states rather than hides); #107 (PowerPoint —
  `describe`'s locus pass covers a slide index for free if #107 lands first, and is not
  blocked either way).

---

## 5. PR-E — CLI composability

> **Value sentence: systems, not features.** A morning routine is a file you run, not
> twelve things you click.

### Parameterised commands, made real

Three commands take arguments, and each one's argument space is a **closed set the server or
the registry already owns** — that is the design rule, not a coincidence:

| Command | Params | Validated against |
|---|---|---|
| `layout.switch` | `{name}` | the saved layouts the window published |
| `workspace.open` | `{path}` | **`RecentsStore.entries()` only** — a path not on the recent list is refused |
| `session.start` | `{prompt, cwd}` | `cwd` jailed under the workspace root; `prompt` capped |

`workspace.open` is deliberately the narrowest. Re-rooting a server to an arbitrary path
from a CLI is a way to point the app at anything readable; restricting it to a list the user
built by opening folders themselves means the CLI can only revisit a decision the user
already made. A path that is not on the list is refused with the list named, never resolved.

**Two layers, and which one checks what.** The published `params_schema` validates *shape*
at the relay, before the bus. Membership of the recent list is checked by the command
itself, in `panels/Workspaces.tsx`, against the `WorkspaceState.recents` the window already
holds — because the window owns its own commands and the relay must not grow a second
opinion about what a workspace is. That split also keeps the narrowing honest about what it
is: the UI's own *Switch workspace…* still takes a typed path, as it does today. This
restricts the **CLI and agent** surface, which is a different threat model (an unattended
process reaching in) and is stated as such rather than dressed up as a general rule.

The mechanism is the field that already ships. Three edits and no new concept: a `Command`
descriptor may declare `params`, so `buildManifest` stops hardcoding `takes_params: false`
and publishes a `params_schema` beside it — from the **window**, since the window owns the
registry; `CommandRelay.invoke` validates against that published schema **before it touches
the bus**, so an invalid `params` is a typed refusal naming the offending field rather than a
ten-second wait for a window that quietly did nothing; and `executeCommandById` passes the
validated object to `command.run(params)`, which per correction 6 is the end that was never
connected. A command that declares no `params` keeps its zero-argument `run()` — the
signature widens to `run(params?: Record<string, unknown>)`, so no existing command changes
and none has to be visited.

The `run_command` agent tool inherits the mechanism, but **not for free, and the budget is
the reason**. `RUN_COMMAND.max_result_bytes` is 2,560, sized for "up to 50 lines of
`id :: title` (~45 bytes each) plus the capped-count footer" — that is ~2,250 bytes before
anything is added, so appending a schema to *every* row would blow it. So only the three
parameterised commands carry a hint, and it is a compact `id :: title  {name:str}` rather
than JSON Schema; the rest are unchanged. The ceiling is re-measured and re-pinned in
`test_agent_tools.py`, and `max_schema_bytes` (420) does not move, because the tool's own
input schema is untouched.

### The startup cost, measured — and where it actually goes

The brief cites ~3–4 s. Measured on this box (Windows 11, uv venv, five runs each):

| | measured |
|---|---|
| bare `python -c pass` | **0.08 s** |
| `import workbench_server.cli.commands_cli` | **~2.1 s** — of which `workbench_server.main` is **1.62 s** and `openpyxl` **0.41 s** |
| `httpx` + `config` + `local_auth` only (no `main`) | **0.86 s** |
| one full `workbench-cmd list` | **4.3 – 4.9 s** |

The decomposition names two remedies, and the cheap one is embarrassing: **`commands_cli.py`
imports `workbench_server.main` for one function** — `runtime_token_path` — and drags the
whole FastAPI application, every router, every service, `openpyxl`, `nbformat` and the agent
SDK in behind it. Moving that helper into `config.py` (or a two-line `runtime.py`) takes
~1.6 s off **every** invocation for a one-file change, before any batch mode exists. PR-E
does that first, and re-measures.

**The batch mode: `--script`, not a daemon.** Even after the import fix, ~2.5–3 s per process
remains (`httpx`'s lazy `httpcore`/`h11` machinery alone measured **2.05 s** on the first
request), so a twelve-step morning routine is thirty-plus seconds of interpreter starts.
`workbench-cmd --script routine.json` reads a JSON op-list and runs it in **one** process:
twelve ops cost one startup plus twelve relay round trips — which `services/commands.py`
describes as "a command runs in the browser in milliseconds", bounded above by
`INVOKE_TIMEOUT_SECONDS` (10 s) for a window that received the event and never answered. The
tradeoff, stated rather than implied:

- **`--script` (chosen).** No new process lifecycle, no port, no daemon to reap, no second
  auth surface — it reuses the token file, the `httpx.Client` and the endpoints exactly as
  they are. It cannot do sub-second *interactive* invocation, because each `workbench-cmd`
  you type still pays the startup.
- **A persistent channel (rejected, for now).** It would make each interactive invocation a
  round trip instead of an interpreter start, and it costs a background process with its own
  lifetime, its own crash story, its own reaping (the `desktop/src-tauri` supervision
  problem, a second time) and its own authentication of a local socket. The workloads that
  hurt are *batches*, and a batch is exactly what `--script` makes cheap. Named here so the
  decision is a decision, and revisited the day someone types `workbench-cmd` in a loop.

A script is `{"ops": [{"command_id": "...", "params": {...}}, ...]}` and it **stops at the
first failure by default** (`--continue-on-error` to keep going), reporting one line per op
and a final tally. Two AXI shapes, applied to a human at a terminal: an empty script says so,
and a failure names the op index and the command that failed.

### PR-E — owned files, models, tests

- **Owns (new):** `server/tests/test_command_params.py`, `server/tests/test_cli_script.py`,
  `ui/src/commandParams.ts`, `ui/e2e/cli-routine.spec.ts`.
- **Owns (edits):** `cli/commands_cli.py` (the import fix + `--script`),
  `models/commands.py` (`params_schema` on `CommandManifestItem`),
  `services/commands.py` (validation before the bus),
  `services/agent_tools.py` (the params hint in `run_command`'s listing, re-measured),
  `ui/src/commandRelay.ts` (`buildManifest` + `executeCommandById`),
  `ui/src/registry.ts` (`Command.params` and `run(params?)`),
  `ui/src/panels/Layouts.tsx` + `ui/src/panels/Workspaces.tsx` + `ui/src/panels/AgentPanel.tsx`
  (each declares its own parameterised command — one line each, in the module that owns it).
- **Models added:** `CommandParamsSchema`, `ScriptOp`, `ScriptResult`.
- **Test story.** Validation: an unknown field, a wrong type and a `workspace.open` path that
  is not on the recent list are each refused **before** the bus is touched (asserted with a
  bus spy at zero publishes); `session.start`'s `cwd` outside the workspace is refused with
  the jail's own message. The import fix ships with a test that **fails if
  `workbench_server.main` is ever imported from the CLI module again** — an
  `importlib`-based assertion, because a 1.6 s regression is invisible to every other gate we
  have. `--script` runs its ops in order, stops at the first failure, and reports one line
  per op.
- **The E2E moment: the scripted morning routine.** `cli-routine.spec.ts` drives a real
  `workbench-cmd --script` against the running E2E server — open a recent workspace, switch
  to the "Morning" layout, open two panes, start a session with a prompt — and asserts the
  **window** reached that arrangement, not that the CLI exited 0. It also asserts wall-clock:
  the four-op script completes in materially less than four separate invocations would, which
  is the only assertion that makes the batch mode a feature rather than a refactor.

---

## 6. The PR sequence, ownership, and what composes with what

Five PRs. **A, C, D and E touch disjoint files *from each other* and can run as four
parallel lanes**; B is the only one with a hard prerequisite *inside this plan* (A, for the
reader seam and the `ReadSource` it stamps on every result B produces).

Disjoint from each other is not the same claim as disjoint from everything in flight. Two of
the four — A and D — carry ordering constraints against PRs that are open as of 2026-08-09,
and those are drawn into the diagram rather than left to the prose, because a lane that
discovers its rebase at merge time has already written its tests against the wrong file:

```
        A   (rebase onto #114, and onto #107 if open) ──► B
        C
        D   (rebase onto #119; #122 has merged)
        E   (rebase onto D if both in flight)
```

| | PR-A | PR-B | PR-C | PR-D | PR-E |
|---|---|---|---|---|---|
| Value | no false PASS | ambient CI for workbooks | handable proof | trusted delegation | systems, not features |
| Server files | reconciliation, office_host bridge | new spec service + entry module | validation store + export | `activity.py` (`describe`) | commands, CLI |
| UI files | ReviewPanel (one line) | new SpecPanel | ReviewPanel, Settings | FileTree, mission, registry | commandRelay, registry, 3 panels |
| New wire models | 2 | 7 | 3 | **0** | 3 |
| CI proof | `WORKBENCH_OFFICE_FAKE=1` | `WORKBENCH_RECONCILE_FAKE=1` | temp workspace | `WORKBENCH_FAKE_AGENT=1` | real server, real CLI |
| Needs the owner's machine | no | no | no | no | no |
| E2E moment | blocked-on-dirty | save → chip flip | restart → evidence survives | two panes, no focus steal | scripted morning routine |

**Shared files, and who touches them** — *within these five*; the cross-plan overlaps are
the table below this one. `ui/src/panels/ReviewPanel.tsx` is touched by A (one source line),
C (the Export action) and, eventually, SR-PR2's findings rendering (a plan section, not a
branch — correction 2) — append-only additions to different regions of one component, and
the plan states the order **C after A** so the source line exists before the export renders
it.
`ui/src/validation.ts` is likewise touched by A (the `"blocked"` `StatusVisual` row) and C
(the export command) — same order, same reason. `ui/src/tools.ts` gains one line (B).
`ui/src/registry.ts` is touched by D (`openToolPanel`'s option) and E (a command's `params`
declaration) — different functions, and E rebases onto D if both are in flight. Nothing else
is shared.

**Composition with the PRs in flight, honestly.** Every row below was checked with
`git diff --stat origin/master...origin/<branch>` on 2026-08-09, not read off the PR titles
— which is how three of them came back overlapping a lane their title gives no hint of:
"gate-closure check" rewrites `services/reconciliation.py`, "PowerPoint hosting" rewrites the
whole document bridge, and "store permission flags" edits `ui/src/mission.ts`.

| PR (status 2026-08-09) | Overlaps | Verdict |
|---|---|---|
| **#114** gate-closure check | **PR-A** — both own `services/reconciliation.py` (#114: +120/−38) | **PR-A rebases onto it.** See PR-A's composition bullet: #114 renames `_Unit`/`_UNIT_TO_BASE`/`_is_ambiguous` public and leaves `OpenpyxlReader` alone, so PR-A's rename merges *cleanly and wrongly* if it goes first |
| **#107** PowerPoint hosting | **PR-A** — `document_bridge.py`, `real_`/`fake_document_bridge.py`, `office_com.py` (+305), `service.py` (+172) | **PR-A rebases onto it if it is still open.** Additive on both sides, conflicting in the same Protocol body every time |
| **#122** activity workspace re-root | **PR-D** — owns `services/activity.py` | **Merged 2026-08-09** (`4a709da`), constraint discharged. PR-D branches off a master that has it; `describe` and its key tuples survive unchanged |
| **#119** store permission flags | **PR-D** — both edit `ui/src/mission.ts` (#119: +40/−11, inside `useMissionStore.answer`) | **PR-D rebases onto it.** Different functions in one file, exactly like the D/E overlap on `registry.ts` above — and it also touches `ui/src/mission.test.ts`, which PR-D extends |
| **#118** dockview v7 | **PR-D**, softly | Wanted, not required: without it, one spurious layout save per agent-driven open (stated under PR-D rather than hidden) |
| **#113** voice seam | none | Disjoint, and a natural later customer for PR-E: a voice command resolving to `layout.switch{name}` is a parameterised invocation, which is why PR-E's validation lives in the relay rather than in the CLI |
| **#116** terminal/agent WS lifecycle, **#123** monaco shared model, **#125** QuickBar a11y (open); **#121** shell thread discipline (merged) | none | Disjoint from all five |

Two notes the table cannot hold:

- **#114 is still, separately, a customer of PR-C.** It is a third `ValidationCheck` whose
  results PR-C persists and whose evidence PR-C's export renders without knowing what it is.
  Overlapping PR-A on one file and composing with PR-C on none is not a contradiction — it
  is why the ownership lists in this document are per-file rather than per-milestone.
- **The staged-review PR2 lane has no branch to compose with.** Per correction 2 it exists
  only as a section of `docs/plan/staged-review.md`; `git ls-remote --heads origin` has no
  ref for it. It would rewrite `services/sdk_factory.py` and touch
  `services/permission_broker.py`, and **no PR here touches either file** — so whenever it
  is built, the two tracks are independent. If PR-D had been built to the brief's
  `PostToolUse` design, they would not have been.

---

## What this plan deliberately defers (so the scope is honest)

- **PDF export.** PR-C renders Markdown, which is readable, diffable, greppable and
  pasteable into anything. A PDF needs a renderer, fonts, a page model and a layout the
  design system has never had to describe, and none of that makes the proof more true. It is
  a formatting job on top of a finished document, and it is named here rather than implied.
- **A scheduler surface.** PR-B re-runs on save, which is the loop the owner asked for. "Run
  this spec every morning at 07:00" is a *different* mechanism — a persistent timer, a
  missed-run policy, a catch-up rule, and a story for what happens when the machine was
  asleep — and it is the shape of the push/PR babysitter that M6 and the staged-review plan
  have both deferred twice. Deferred a third time, on purpose.
- **Team sharing.** PR-C makes proof handable; it does not make it *shared*. A shared
  evidence store means identity, a server that is not on `127.0.0.1`, an authorisation model
  and a retention policy somebody other than the user owns. Every one of those is a product
  decision, and this repo's whole security posture (per-launch loopback token, nothing
  written to `~/.claude`, zero telemetry) is built on their absence.
- **`.workbench/gates.json`.** PR-B builds the one-time content-hash approval that
  `services/gates.py::workspace_config_refusal` names as the price of admission — and it
  spends it on reconciliation specs only. Per-workspace *gate* configuration stays refused,
  out loud, in the same words. A trust prompt built for one document type is not a licence
  for the other.
- **A persistent CLI channel.** Rejected with a measurement (PR-E), not with a shrug.
  Revisited when someone measures an interactive workload that `--script` does not cover.
- **Provenance surviving a restart, and folder-level provenance rollup.** PR-C persists
  *validation*, not provenance. They are different claims with different bars — provenance
  says who wrote a file and would rather say nothing than say the wrong thing — and it stays
  on the Deferred-ideas list where the ROADMAP already put it.
