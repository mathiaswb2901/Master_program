# Staged review — from "workbooks proven" to *work* proven

Status: **plan** (this document is the Plan PR for M6's deferred staged-review checks; no
feature code lands with it). It is the second plan doc under `docs/plan/`, written against
the repo as it stands after [`m6-proof.md`](m6-proof.md)'s PRs landed (#82 the
validation frame, #85 the reconciliation gate, #86 the Review panel plus the Mission
Control join, #89 the `office_reconcile` agent tool, #90 objective sessions).

M6 proved *numbers*: a workbook reconciles with the code, unit- and DST-aware, with the
mismatch and its delta as evidence a human approves. That is the domain moat and it is
shipped. What it does not yet prove is the rest of what an agent hands over — that the
change it wrote compiles, lints, passes its own tests, and survives someone actively
trying to break it. **Staged review generalizes the proof from "workbooks proven" to
"work proven"**, and it does so without a new mechanism: the two checks below are
`ValidationCheck` implementations, and everything downstream of a check — risk derivation,
the evidence gallery, the bus event, the replay, the Mission Control badge, the objective
close, the one mandatory human approval — is already built and stays untouched.

That is why this is **two PRs and not five**.

## What the #82 frame already did (and what it left)

The M6 frame is the hard part of this feature, and it is done. Each row below is a thing
neither PR in this plan has to build, design, or argue about again:

| Already shipped | Where | What it means here |
|---|---|---|
| `ValidationResult` / `EvidenceItem` / `ValidationSubject` | `models/validation.py` | A check returns `list[EvidenceItem]`. That is its whole contract. |
| Risk **derived** from evidence, no evidence = `blocked` | `services/validation.py:derive_risk` | A gate cannot report a silent green, and a new check cannot invent a risk scale. |
| `ValidationCheck` protocol + registry | `services/validation.py` | A new gate is one class with an `id` and an async `run`, plus one `register()` line in `create_app`. |
| Bounded per-kind `PayloadStore` | `services/validation.py` | A captured log or a diff report is stashed and referenced, never inlined into the result or the `/ws/events` frame. |
| `ValidationEvent` + `GET /api/validation` replay | `routers/validation.py` | Every window sees a gate result, including one that reconnects after it. |
| The human approval gate, stale id = 404 | `POST /api/validation/{id}/approve` | **The sole decider stays the human.** Neither check below may touch it. |
| Review panel, evidence gallery, risk badge, status reading | `ui/src/panels/ReviewPanel.tsx` | New evidence kinds render with **no UI work** — the gallery is written against `EvidenceItem`, not against reconciliation. |
| Objective status derived from validation results | `services/sessions.py:derive_objective_status` | A gate result closing an objective is free. |

**The one gap the frame left, verified in the code rather than assumed.** Nothing exposes
`ValidationService.payload(kind, ref)` over HTTP — `routers/validation.py` has four routes
and none of them is a payload route — so `EvidenceItem.payload_ref` is currently a dead
handle in the browser. The Review panel says so itself, in the expander it ships today:

> The bounded payload view (ref …) arrives with the payload endpoint — deferred past this
> PR. The line above is the whole of what #82 carries inline.

For reconciliation that was survivable: the grouped evidence line carries the counts, and
`office_reconcile` reads the report server-side for the agent. For a toolchain gate it is
not: the *entire* value of a failing gate is the captured output, and a gate whose log a
human cannot read is a gate they have to take on faith — which is the opposite of this
milestone. So **PR 1 closes that gap** as part of shipping the first check that needs it.

## Constraints carried from the house rules

The five in [`m6-proof.md`](m6-proof.md#design-constraints-carried-from-the-house-rules)
apply unchanged (typed payloads, thin routers, registered capabilities, plural by default,
agent-tool byte budgets and the AXI three shapes). This milestone adds two of its own,
because both checks reach outside the process for the first time:

- **Evidence, never authority.** A check produces `EvidenceItem`s and stops. It never
  calls `approve`, never mutates a `ValidationResult`, and never decides that a change is
  good. The human approval gate is the sole decider, and in both PRs that is asserted by a
  test rather than left to review — the `test_orchestrator.py` precedent, where "there is
  no code path in this module that resolves a permission" is an assertion.
- **No argv in a JSON body.** `m6-proof.md` refused to execute analyst-supplied code
  inside the server ("a shell in a JSON body, the same threat the `shortcuts.md`
  never-execute doctrine exists to stop") and took *values* instead. The same refusal
  binds here, and it is sharper because these checks really do run processes: a caller
  names **which configured gate** to run, never **what to run**. The argv is the server's.

## 1. PR 1 — the toolchain gate

`services/gates.py`, `models/gates.py`, `models/evidence.py`, plus the payload route the
frame left open.

**What it is.** A `ValidationCheck` with `id = "gates"` that runs a configured set of
project gate commands — `ruff`, `mypy`, `pytest`, `npm-test` — inside the worktree the
subject session is actually working in, and turns each into one `EvidenceItem` of kind
`gate` whose `payload_ref` names the captured log.

### Where it runs — the session's own slot, and nowhere else

The pool (#46, `services/worktrees.py`) already hands a Mission Control worker a borrowed,
detached checkout under a lease, and `services/orchestrator.py` holds the roster that
knows which slot belongs to which session. The gate **runs in that slot**, resolved
through a small injected seam so `services/gates.py` never imports the orchestrator:

```
class SlotLocator(Protocol):
    """Which checkout is this session writing in? None when it holds none."""
    def slot_of(self, session_id: str) -> SlotRef | None: ...

class SlotRef(BaseModel):
    slot: str | None      # pool slot name, None for a session outside the pool
    path: str             # the checkout the gate runs in
    base: str             # the commit the slot was leased at — the diff's other end
```

`OrchestratorService` implements it (one new method scanning the rosters; the roster
already carries `path`, `slot` and `lease_id` per worker), and `create_app` wires
`ToolchainGateCheck(locator=orchestrator_service, runner=…)`.

Three decisions fall out of this, and the third is the one worth stating out loud:

1. **The gate takes no lease.** The session holds it. The gate is a *reader* of a checkout
   somebody else borrowed, exactly as the Review panel is a view onto a result it does not
   own. Acquiring a second slot would be worse than useless — it would validate a
   different tree than the one the agent wrote.
2. **The tree is fingerprinted before and after.** `git rev-parse HEAD` plus the
   `--porcelain` count, on both sides of the run. If either moved, the whole run is
   `skipped` with "the tree moved under the gate while it ran (N files) — re-run when the
   session is idle", never a pass or a fail attributed to a tree that no longer exists.
   This is `_verify_reset`'s posture in `services/worktrees.py`, applied one level up: two
   git processes with a gap between them means you re-read rather than hope.
3. **A session with no slot gets a refusal, not a fallback.** A plain chat session works
   in the user's live workspace root, and running `pytest` there would write caches into
   the folder they are editing and judge a tree that includes their unsaved changes. So
   the evidence is one `skipped` line — "this session holds no worktree slot; gates run in
   the checkout the session writes in" — and the way to get one is named. Refusing is the
   feature; falling back to the workspace root would be the bug.

### What may run — a catalog, not a command line

`GateCommand` is server-owned data, and the spec selects from it by id:

```
class GateCommand(BaseModel):
    id: str                      # "ruff" | "mypy" | "pytest" | "npm-test"
    argv: tuple[str, ...]        # ("uv", "run", "pytest", "-q") — fixed, no shell
    label: str                   # what the evidence line is called
    timeout_s: float
    pass_codes: tuple[int, ...] = (0,)

class GateSpec(BaseModel):
    """ValidationSpec.params for check id "gates"."""
    gates: list[str] = []        # ids from the catalog; empty = the configured default set
    log_bytes: int | None = None # widen the captured window, capped at MAX_GATE_LOG_BYTES
```

`asyncio.create_subprocess_exec`, never `shell=True`, with the same
`GIT_TERMINAL_PROMPT=0`-style hygiene and hard per-gate timeout `run_git` already uses —
a gate that hangs would hang the request that started it and, through it, the lifespan
shutdown.

The catalog is **built in**, with the default set and the timeouts settable by the
operator through `WORKBENCH_GATES` and `WORKBENCH_GATE_TIMEOUT_S`. A per-workspace
`.workbench/gates.json` is **deliberately not built** (see the deferrals): a config file
inside the folder would mean that *opening* a project is enough to run its commands, which
is the escalation the PreToolUse broker (#104) exists to stop, arriving by a side door.
Adding a fifth shape is one row in the catalog and one line in its test.

### The evidence, and the bounded log

One `EvidenceItem` **per gate**, not one grouped line. That is the opposite of what
`ReconciliationCheck` does, and on purpose: reconciliation groups because it has forty
comparisons and one question, whereas four gates are four independent questions whose
answers a reader wants side by side. Four lines sit comfortably under `MAX_EVIDENCE`, and
per-gate outcomes let `derive_risk` do exactly the right thing — a failing `pytest` is
`high` even when `ruff` is clean.

```
class GateLog(BaseModel):
    """The payload behind a `gate` EvidenceItem."""
    gate: str
    argv: list[str]
    exit_code: int | None        # None = timed out or could not start
    duration_ms: int
    text: str                    # head + tail capture, byte-bounded
    truncated: EvidenceTruncation | None   # AXI shape 1, reusing the frame's model

class GateRunReport(BaseModel):
    path: str
    slot: str | None
    head: str                    # the commit judged, both sides of the run
    gates: list[GateLog]
    started_at: datetime
    duration_ms: int
```

`MAX_GATE_LOG_BYTES = 8_192` per gate, captured as a 2 KiB head plus a 6 KiB tail — the
tail because that is where `pytest` and `mypy` put their summary, the head because that is
where a "command not found" lands. **Bounded while reading, not after**: the runner keeps
a head buffer and a ring tail as the pipes drain, so a gate that prints 500 MB costs 8 KiB
of memory, not 500 MB. When it bites, `truncated` says how many bytes were withheld and
names `log_bytes` as the argument that widens the window.

### The payload route (the frame's gap, closed)

```
GET /api/validation/payload/{kind}/{ref} -> EvidencePayload   # 404 once the LRU drops it

class EvidencePayload(BaseModel):
    kind: EvidenceKind
    ref: str
    reconciliation: ReconciliationReport | None = None
    gate_log: GateLog | None = None
    # PR 2 appends exactly one field here: review: ReviewReport | None
```

Two notes, both load-bearing:

- It lives in a **new `models/evidence.py`**, not appended to `models/validation.py`,
  because `models/reconciliation.py` already imports `EvidenceTruncation` *from*
  `models/validation.py` — an envelope that names both payload types has to sit downstream
  of both or the import cycles.
- One optional field per kind rather than a discriminated union: a union needs a literal
  discriminator field added to every existing payload model, which changes shipped wire
  shapes for no gain the UI can feel. Adding a kind is one optional field and one
  narrowing branch — the same one-line-append shape `ValidationEvent` took to the bus.

A 404 is rendered by the Review panel's expander as "this log has been evicted" (the LRU
is bounded and honest about it), not as a spinner that never resolves.

### The agent tool — `run_gates`

The `office_reconcile` precedent (#89) applies exactly: a session that can prove its own
work does not need a human to run the gate for it. One `AgentToolSpec`, appended to
`services/agent_tools.py`, wired in `sdk_factory.build_context_bridge`.

- **Arguments:** `{gates?: string[], log_bytes?: int}`. No argv, no cwd, no path — the
  slot is resolved from the calling session's own id, which the bridge already closes over
  in `build_context_bridge` (`handle_spawn_worker(orchestrator, bridge.session_id, args)`
  is the shipped shape), so the tool cannot be pointed at another session's checkout.
- **Shape 1, truncate with a stated size and the argument that widens it:** `"pytest
  exit 1 (12.4s): 3 failed, 118 passed. Showing 400 of 2,140 bytes of output; pass
  log_bytes=4000 for the rest."`
- **Shape 2, say none explicitly:** all clean returns `"All 4 gates pass (ruff, mypy,
  pytest, npm-test) in 96s."`, and a session with no slot returns the refusal sentence
  rather than an empty result a model reads as either clean or broken.
- **Shape 3, end with the next step:** a failing run ends by naming the first failing
  gate's first failing location, which is where the model should read next.
- `max_result_bytes`, `max_schema_bytes` and the description ceiling are sized from the
  measured payload plus a stated margin and asserted in `test_agent_tools.py`, alongside
  the existing tools.

Auto-allowed like every other workbench tool, and that is defensible precisely because of
the argv rule: the tool cannot express an arbitrary command, so it is not the shell escape
`_AUTO_ALLOWED`'s omission of `Bash` and the PreToolUse broker exist to prevent. Stated in
the module docstring, and asserted by the test that the schema exposes no argv field.

### Fake-first / CI

Fully. `GateRunner` is a protocol with two implementations, the Office-host split exactly:
`SubprocessGateRunner` (real) and `FakeGateRunner` (scripted exit codes and canned output),
selected by `WORKBENCH_GATE_FAKE=1` — the `WORKBENCH_OFFICE_FAKE` posture, and the flag
the Playwright lane sets in `ui/playwright.config.ts` next to the two already there. CI
never runs a real `pytest` inside a slot; it proves the *flow*.

### PR 1 — owned files, models, tests

- **Owns (new):** `server/src/workbench_server/models/gates.py`,
  `server/src/workbench_server/models/evidence.py`,
  `server/src/workbench_server/services/gates.py`, `server/tests/test_gates.py`.
- **Owns (append-only, one place each):** the payload route in `routers/validation.py`;
  `slot_of` in `services/orchestrator.py`; `RUN_GATES` + its handler in
  `services/agent_tools.py`; its wiring in `services/sdk_factory.py`; the check
  registration and runner construction in `main.py`; `gates`, `gate_timeout_s` and
  `gate_fake` in `config.py`; the mirror types and the payload fetch in `ui/src/types.ts`,
  `ui/src/validation.ts`, `ui/src/panels/ReviewPanel.tsx`.
- **Models added:** `GateCommand`, `GateSpec`, `GateLog`, `GateRunReport`, `SlotRef`,
  `EvidencePayload`.
- **Test story** (`test_gates.py`): a fake runner's non-zero exit is a `fail` line and
  `derive_risk` reports `high`; a clean run is four `pass` lines; a timeout is `fail` with
  the timeout named, not a hang; a session with no slot is one `skipped` line naming the
  refusal; the before/after fingerprint mismatch turns a *passing* run into `skipped` (the
  regression test for trusting a tree that moved); an 8 KiB-plus log is captured
  head-and-tail with `truncated` stating the withheld bytes and naming `log_bytes`; an
  unknown gate id is a `fail` line, never a silent skip (the frame's unregistered-check
  precedent); the payload route round-trips a `GateLog` and 404s an evicted ref.
  `test_agent_tools.py` gains the three ceilings for `run_gates` and asserts its schema
  carries no argv. E2E: `ui/e2e/review.spec.ts` gains a leg that runs the gate in fake
  mode and **opens the log in the expander** — the payload path proven in a real browser,
  which is the half a unit test cannot claim.

## 2. PR 2 — the adversarial review

`services/review.py`, `models/review.py`.

**What it is.** A `ValidationCheck` with `id = "review"` that puts a **fresh-context**
agent session in front of the subject session's diff with one instruction — *try to prove
this is wrong* — and turns what it finds into evidence. It never approves anything.

### fork_session vs. the #63 spawn seam — weighed, and picked

The SDK ships `fork_session` (`claude_agent_sdk.fork_session`, and
`ClaudeAgentOptions.fork_session=True` alongside `resume`, which the transport turns into
`--fork-session`). It is genuinely unused in this codebase and it came out of the tooling
sweep as a candidate for exactly this check. Read closely, it is the wrong primitive here,
and the reason is the feature's whole point:

| | `fork_session` | The #63 spawn seam (`SessionManager.create_at`) |
|---|---|---|
| Context the reviewer starts with | **The source transcript, copied** (UUIDs remapped, `parentUuid` chain preserved) | **Empty** |
| Cost of turn one | Proportional to the whole implementer transcript | Proportional to the diff |
| Drivable under `WORKBENCH_FAKE_AGENT=1` | No — it is an SDK/CLI-level call, and fake mode swaps the *client factory* | Yes — the same factory seam every session already goes through |
| Side effects | Writes a new transcript file beside the source | None beyond a session the manager already owns and reaps |
| Fits the fleet (activity, usage, permissions, caps) | Outside all of it | Inside all of it, for free |

A fork inherits the implementer's reasoning — including its self-justifications and its
own claim that the tests pass. An adversarial reviewer that has already read why the
change is correct is not a fresh context; it is the same context with a new name. **So
this check uses the spawn seam**, which `create_at` was built for ("a session in a folder
the **server** chose") and which the orchestrator has been driving since #63.

`fork_session` is not discarded, only re-filed: it is the right primitive for a *different*
feature — "branch this conversation at message N and try another continuation" — and that
is recorded here so the research finding is not lost twice.

**Adopted from the same sweep, and also currently unused:**
`ClaudeAgentOptions.max_turns` and `ClaudeAgentOptions.max_budget_usd`, both set on the
reviewer's options. A check that spends without a ceiling is a check nobody will leave
switched on.

### What the reviewer sees

The diff is built in the subject's slot, against the commit it was leased at
(`SlotRef.base`, which `WorktreeInfo.head` carries for the life of a lease):

- `git diff <base>` — everything committed and uncommitted, in one read;
- **plus `git ls-files --others --exclude-standard`**, with the new files' contents
  appended. Untracked files are invisible to `git diff`, so without this an agent that
  wrote three brand-new modules would be reviewed as having changed nothing — a silent
  green of the exact kind this milestone exists to refuse.

Bounded at `MAX_DIFF_BYTES = 200_000` with a per-file cap so one generated file cannot
crowd out the rest, and the truncation is stated to the reviewer *in its own prompt*
("12 of 31 files shown, largest first; 41 KB of 190 KB") — the AXI shapes are not only for
tools, and a reviewer that does not know it was shown a slice will report absence as
evidence of absence.

The reviewer's brief is a **fixed server-side prompt**: refute-first, a finding must name
the input or state that breaks the change, a finding with no concrete failure path is a
`nit`, and it has no way to edit anything. A bounded `focus` string (≤ 500 chars, the
pipeline's `reviewFocus`) may be appended — that is prompt text handed to a read-only
session, which is a different thing from argv, and the distinction is why one is allowed
and the other is not.

### The reviewer session

`SessionKind` gains `"reviewer"` (one word in `models/agents.py`). That word is cheap. The
isolation it is supposed to buy is **not already in the code**, and this plan says so
plainly rather than letting a build lane discover it: three of the four bullets below are
**changes to existing, currently-unconditional logic that every other kind runs through** —
not an additive branch like the orchestrator's five tools. A lane that adds a branch and
stops will ship a reviewer that can still see and call `office_write` and `run_command`.

- **The toolset — a required refactor of `build_context_bridge`, not an addition.**
  `tools_for("reviewer")` returns **only** `report_findings` — no `office_read`, no
  `office_write`, no `run_command`, no `workspace_search` — living in its own
  `REVIEWER_TOOLS` tuple rather than in `AGENT_TOOLS`, for the same reason the five
  orchestrator tools do: a tool a chat session can see is a schema every chat session pays
  for on every request. **Defining that tuple changes nothing on its own.**
  `build_context_bridge` (`services/sdk_factory.py`, lines 134–142) hardcodes a base list
  of seven `@tool` closures for *every* kind and only ever *adds* to it (the `kind ==
  "orchestrator"` branch at line 143); no kind subtracts, and the construction never reads
  `tools_for`. The one caller of `tools_for` today is `allowed_tool_names`
  (`services/agent_tools.py:1438`), which feeds `allowed_tools` — and per the SDK's own
  docstring `allowed_tools` names "tool names that are auto-allowed without prompting"; it
  does **not** remove a tool from the model's context. Only `tools` (the builtin set) and
  `disallowed_tools` ("removed from the model's context and cannot be used") do that, and
  this codebase passes neither. So PR 2 **rewrites the base-list construction to build from
  `tools_for(kind)`** — one `@tool` closure per spec, selected by kind, with the
  orchestrator's five folded into the same selection instead of appended after it. That
  touches the path every existing kind takes, so it ships with a per-kind test asserting
  the exact tool names the bridge exposes: `chat` and `worker` get the base set unchanged —
  today's seven **plus PR 1's `run_gates`**, which the refactor must carry rather than drop
  — `orchestrator` gets that set plus its five, and `reviewer` gets `report_findings`
  alone. Names, not counts: a count passes while a tool is quietly swapped.
- **The builtin tools — `_AUTO_ALLOWED` is unconditional today and must stop being.**
  `_AUTO_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep"]` (`sdk_factory.py:58`) is
  spread into every session's `allowed_tools` with no kind gate (line 241), and no
  `disallowed_tools` is passed anywhere. A reviewer assembled by adding a branch elsewhere
  would therefore still have `Write` and `Edit` **pre-approved, ahead of `can_use_tool`**.
  PR 2 gates the spread on kind — `Read`, `Glob`, `Grep` for a reviewer — **and** passes
  `disallowed_tools` for that kind covering `Write`, `Edit` and `Bash`. Both, on purpose,
  because they do different jobs: gating the allow-list removes the silent auto-approval,
  while `disallowed_tools` is the SDK's only documented way to take the tool out of the
  model's context so the reviewer does not spend turns asking for one it cannot have.
- **The permission posture — new safety-critical code, with no precedent to copy.** There
  is no deny-and-log path in this repo to "apply". `can_use_tool`
  (`sdk_factory.py:209-212`) calls `bridge.ask_permission(...)` unconditionally for every
  kind — `worker` and `orchestrator` included — and the `PreToolUse` broker
  (`services/permission_broker.py:127`) escalates brokered shell calls through the *same*
  future, so there are **two** paths to the user's screen, not one. `m6-proof.md` §3
  described an unattended deny-and-log policy alongside `ObjectiveCaps`, but the objective
  sessions that shipped in #90 implement neither: `Objective` in `models/sessions.py` is a
  thin `{statement, acceptance}` record with status derived, and `ObjectiveCaps`,
  `max_iterations` and `max_wall_clock_s` appear nowhere in `server/src`. So PR 2 **builds
  this from nothing**: a `kind == "reviewer"` branch inside `can_use_tool` that returns
  `PermissionResultDeny` with a message naming the read-only posture and logs it, **without
  calling `bridge.ask_permission`**, plus the same short-circuit in the brokered hook so
  the second path cannot escalate either. The reason is the check's whole premise — a check
  that runs unattended must not be able to interrupt the user, and a reviewer that wanted a
  tool it was not given has a finding to make, not a permission to ask for. Because this is
  the first code in the repo that answers a permission with no human in the loop, it ships
  its own test: a reviewer's blocked tool call reaches neither `bridge.ask_permission` nor
  the pending-permission map behind the board, asserted with a spy rather than by reading
  the branch, and the denial is asserted to be logged.
- **What the one word does buy for free**, and it is the fourth bullet rather than the
  first: a `reviewer` appears in the activity feed and Mission Control as a `reviewer` row,
  so a review in flight is visible rather than a mystery pause. It also counts against
  `WORKBENCH_MAX_CONCURRENT_SESSIONS`, and a review that cannot start because that cap
  binds is a `skipped` evidence line naming the setting — the `SpawnRefusal` idiom, where
  a cap that is hit renders as a cap with a way out.

It runs with the slot as its cwd and **holds no lease**: "one writer per checkout" is
intact because the reviewer is not a writer.

### How findings come back — `report_findings`

Not by parsing prose. `present_plan` established the pattern in this repo — an agent
delivers a *typed artifact* by calling a tool through the `SessionBridge` — and a reviewer
delivering findings is the same shape:

```
ReviewSeverity = Literal["must_fix", "should_fix", "nit"]

class ReviewFinding(BaseModel):
    severity: ReviewSeverity
    file: str | None
    line: int | None
    claim: str          # what is wrong, one line
    refutation: str     # the input or state that breaks it — the reason to believe the claim
    confidence: Literal["certain", "likely", "possible"]

class ReviewReport(BaseModel):
    base: str
    head: str
    files_reviewed: int
    diff_bytes: int
    findings: list[ReviewFinding]
    truncated: EvidenceTruncation | None
    reviewer_session_id: str
    turns: int
    cost_usd: float
```

**One grouped `EvidenceItem` of kind `diff`** — the reconciliation-grouping precedent, and
here the right call for the opposite reason to PR 1: a review is one question with many
supporting details, so the details belong in the payload. Outcome is the worst severity
found: `must_fix → fail`, `should_fix → warn`, nits or nothing → `pass`. The detail line
is `"3 findings (1 must_fix, 2 nit) over 12 files, 41 KB of diff"`, or, when there are
none, `"No findings — 12 files, 41 KB of diff reviewed."` — AXI shape 2, because a review
that found nothing and a review that never ran must never look alike. A reviewer that
timed out, blew its budget or could not be spawned produces a `gate`/`fail` line naming
the ceiling and the env var that raises it, never an absence read as approval.

### Evidence only — the check never approves

`services/review.py` contains no call that can record an approval, and
`test_review.py` asserts it two ways: a `must_fix` review leaves `result.approval is None`
with the result at `high` and therefore *awaiting approval*, and the module is asserted to
reference no approval API at all. This is the `test_orchestrator.py` "never auto-allow
shell" assertion, aimed at the other privilege that must stay human. An agent that could
commission a reviewer *and* count its verdict as approval has quietly become its own
merge queue.

For the same reason PR 2 ships **no agent-facing tool** to start a review. A review is
started by a human, by `POST /api/validation/run`, or by an objective's spec. A session
that could commission its own reviewer could also loop on it, and the money that pays for
that loop is the user's.

### Fake-first / CI

Fully. `WORKBENCH_FAKE_AGENT=1` already replaces the client factory for every kind, so
`services/fake_agent.py` gains a `reviewer` branch that answers the review prompt by
calling `report_findings` with canned findings — the same seam, a different script. CI
proves spawn → diff → findings → grouped evidence → risk → *still awaiting approval*,
with no Claude login and no tokens. E2E: the Playwright workspace is already a real git
repository (`seedRepository` in `ui/e2e/workspace.ts` exists so the pool can serve), so
`review.spec.ts` gains a leg that runs the check against a worker's slot in fake-agent
mode and asserts the findings render in the gallery and the result is still awaiting
approval.

### PR 2 — owned files, models, tests

- **Owns (new):** `server/src/workbench_server/models/review.py`,
  `server/src/workbench_server/services/review.py`, `server/tests/test_review.py`.
- **Owns (append-only, one place each):** `"reviewer"` in `SessionKind`
  (`models/agents.py`); `REPORT_FINDINGS` + `REVIEWER_TOOLS` + the `"reviewer"` arm of
  `tools_for` in `services/agent_tools.py`; the reviewer script in
  `services/fake_agent.py`; registration in `main.py`; the review settings in `config.py`;
  one optional field on `EvidencePayload` (`models/evidence.py`, created by PR 1); mirror
  types and the findings rendering in `ui/src/types.ts` and
  `ui/src/panels/ReviewPanel.tsx`.
- **Owns (a rewrite of shared, currently-unconditional logic — the exception to
  "append-only", called out because it is the one place this PR can break other kinds):**
  `services/sdk_factory.py`. Three edits, none of them a new branch bolted on the side:
  the `build_context_bridge` tool list becomes a selection over `tools_for(kind)`;
  `_AUTO_ALLOWED` becomes kind-dependent and `disallowed_tools` starts being passed; and
  `can_use_tool` grows the reviewer's deny-and-log short-circuit, mirrored in the brokered
  `PreToolUse` hook (`services/permission_broker.py`).
- **Models added:** `ReviewFinding`, `ReviewReport`, `ReviewSpec`, `ReportFindingsRequest`.
- **Test story** (`test_review.py`): canned findings become one grouped `diff` line whose
  outcome is the worst severity; no findings is an explicit `pass` line that says so; a
  reviewer that times out or exceeds `max_budget_usd` is a `fail` naming the setting; the
  session cap binding is a `skipped` naming `WORKBENCH_MAX_CONCURRENT_SESSIONS`; the diff
  builder **includes untracked files** (the regression test for the silent-green trap) and
  truncates with a stated size; **the check never approves**, asserted both ways. In
  `test_sdk_factory.py`, the three isolation assertions the bullets above are worth nothing
  without: `build_context_bridge` exposes exactly `report_findings` for `kind="reviewer"`
  and the *unchanged* name sets for `chat`, `worker` and `orchestrator` (the regression
  test for the refactor); `build_agent_options(kind="reviewer")` auto-allows no `Write` or
  `Edit` and disallows `Write`/`Edit`/`Bash`; and a reviewer's blocked tool call resolves
  to a denial having called neither `bridge.ask_permission` nor the broker's escalation —
  a spy asserting zero awaits, since "it is denied" and "it is denied without waking the
  user" are different claims and only the second is the feature. `test_agent_tools.py`
  gains the three ceilings for `report_findings`.

## 3. Sequence and file ownership

**Two PRs, in order — deliberately not two parallel lanes.** Their *owned* files are
disjoint (`gates.py` + `evidence.py` vs. `review.py`, and their models and tests), but they
share five registration points: `services/agent_tools.py`, `services/sdk_factory.py`,
`main.py`, `config.py` and `ui/src/panels/ReviewPanel.tsx`. Four of the five are
append-only. **`services/sdk_factory.py` is not**, and that asymmetry is the strongest
argument for the order: PR 1 *appends* `run_gates` to the hardcoded tool list, and PR 2
*rewrites that list* into a selection over `tools_for(kind)` — so the two lanes running
concurrently would not merely conflict, they would silently disagree about whether
`run_gates` survives, which is a green suite and a missing tool. PR 2 rebases onto PR 1,
carries `run_gates` through the refactor deliberately, and gets the `EvidencePayload`
envelope to append its one field to. Stated here so the order is not rediscovered by
whoever runs the second lane.

Neither PR touches `services/validation.py`, `models/validation.py`,
`services/reconciliation.py`, `ui/src/mission.ts` or `services/sessions.py`. The frame is
finished; this milestone is two implementations of an interface it already published.

| | PR 1 — toolchain gate | PR 2 — adversarial review |
|---|---|---|
| Check id | `gates` | `review` |
| Evidence kind | `gate`, one line per gate | `diff`, one grouped line |
| Reaches outside for | subprocesses, in a borrowed checkout | a fresh agent session, in the same checkout |
| Agent-facing tool | `run_gates` (the session proves its own work) | none, by design |
| CI proof | `WORKBENCH_GATE_FAKE=1` | `WORKBENCH_FAKE_AGENT=1` |
| Needs the owner's machine | no | no |

Both are **fully CI-verifiable with no real toolchain run and no Claude login** — which is
the same property that let M6's reconciliation gate be green on a machine with no Office.

## What this plan deliberately defers (so the scope is honest)

- **The push/PR babysitter with bounded retries.** M6 deferred it because it needed the
  staged-review checks; those arrive here, and the babysitter still does not. It is a loop
  that pushes branches and answers CI on the user's behalf, i.e. a *writer* to a remote,
  and it needs its own threat model, its own caps and its own approval story rather than a
  ride on this one. Named, not forgotten — for the second time, on purpose.
- **Intent-directed E2E with recorded screenshots/video** as a `ValidationCheck`. Both
  checks here answer "does this hold up"; an intent-directed E2E answers "does it do what
  the user asked", which needs a stated intent, a driver, and artifact storage for the
  recordings — a third thing, not a variation on these two.
- **Per-workspace gate configuration** (`.workbench/gates.json`). Deferred with a reason
  rather than a shrug: opening a folder must never be sufficient to run that folder's
  commands. If it ships it needs an explicit one-time trust prompt, which is a feature.
- **Cross-checkout review** — reviewing a diff by applying it to a second, clean slot so
  the reviewer cannot be confused by the implementer's in-flight writes. The fingerprint
  check (PR 1) and the diff-in-the-prompt design (PR 2) make it unnecessary for now; if a
  reviewer is ever given `Bash`, it becomes necessary immediately.
- **Persisting gate logs and review reports to `.workbench/`.** Both ride the frame's
  in-memory bounded `PayloadStore`, so a restart forgets them and an evicted ref 404s
  honestly. Persistence stays the same named later PR it has been since M6.
