# Roadmap

The living plan. Milestone status is updated as work lands; user change requests get
logged under **Change requests** and pulled into milestones.

## North star

Workbench is the one-window instrument where a technical analyst supervises a fleet of
Claude agents that plan visually, work in parallel isolated worktrees, and prove their
results with evidence — across code, terminals, and **real Office documents** that no
other agent workspace can touch.

Positioning (2026-08 landscape review): every competitor builds pieces of this for
developers — Conductor (Mac-only worktree orchestration), Nimbalyst (multi-surface but
markdown-only documents), Cursor's Agents Window, Google Antigravity's evidence
artifacts, and Anthropic's own Claude Code desktop + Cowork as two disconnected apps.
Nobody serves the technical analyst who ships an optimizer, a workbook, and a memo from
the same task. Generic multi-session UI will be commoditized by first parties; in-window
real-Office editing with agent provenance and domain validation gates will not. That is
the moat.

## Product principles

1. **A workbench of tools, not a pipeline.** Every capability is a standalone tool —
   its own service, API, panel, and QuickBar commands — usable independently;
   composition happens over the event bus and is opt-in. A tool registry (M4) replaces
   hardwired panels and is the seam a future plugin system plugs into.
2. **Real programs, really embedded.** Documents open in actual Word/Excel/PowerPoint,
   docked into Workbench via native window hosting (spike-proven 2026-08-04: ~1 s to
   embed real Word on Win11/Office16). OnlyOffice serves preview, document diffing for
   review, and fallback when Office isn't installed.
3. **Think big.** Model estimates of development cost are trained on human developer
   speed and run far too high, biasing designs toward cheap, unambitious options. Do
   not weight development cost heavily; pick the ambitious, correct, premium option.
   When a capability seems out of reach, spike it before ruling it out. (Standing user
   directive, 2026-08-04.)

## Milestones

| # | Name | Scope | Status |
|---|------|-------|--------|
| M0 | Foundations | Typed FastAPI core, uv, ruff/mypy-strict/pytest, pre-commit, CI, design system (`DESIGN.md`, tokens) | **done** |
| M1 | IDE-lite shell | pywinpty terminals, jail-safe files API, Monaco tabs, watcher/sync engine, multi-session agent core, QuickBar | **done** |
| M2 | Word | OnlyOffice native install, signed editor config + save callback, Doc panel, reopen-on-agent-edit, degraded mode | **done** |
| M3 | Excel + PowerPoint | Sheet/Slides panels (same pattern), `.bak` safety, keep-alive editors, content-hash keys | **done** |
| M4 | **Instrument** | See below | **in progress** |
| M5 | **Parallel** | See below | pending |
| M6 | **Proof** | See below | pending |
| M7 | **Premium & Public** | See below | pending |

### M4 — Instrument (finish the tool, land the first primitives)

- **Office host panels**: real Word/Excel/PowerPoint docked into dockview via native
  window hosting; COM bridge so agents read/write the *live* open document; OnlyOffice
  demoted to preview/diff/fallback. Required the **Tauri shell** (a browser tab cannot
  host native windows) — pulled forward from packaging into M4 core; the shell
  **landed** (`desktop/`, PR 1 of this track): native window, supervised backend
  (attach-or-spawn, Job-Object reaping), and the two browser-only gaps below re-wired
  natively. The **host domain layer landed** too (PR 2, `services/office_host/` +
  `models/office_host.py` + `routers/office_host.py`): the full lifecycle
  (`launching → embedding → embedded`, `detached`, and the terminal
  `closed`/`crashed`/`failed`), the `HostBackend` Protocol the native implementation
  must satisfy, an in-process **fake backend** (`WORKBENCH_OFFICE_FAKE=1`) that makes
  every branch reachable in CI with no Office and no Rust, `OfficeHostEvent` on the
  existing `/ws/events` bus, `GET /api/office/capabilities` for honest degradation, and
  the ownership rule that **never adopts a process we did not launch**. Two owner
  decisions are encoded there rather than left to reviewers: PowerPoint is
  preview-only in v1 (single-instance, no `Application.Hwnd` to prove ownership) and
  `WORKBENCH_OFFICE_NATIVE=auto` resolves to *not* hosting natively until hang
  isolation is proven. The **Rust window hosting landed** too (PR 3,
  `desktop/src-tauri/src/host/`): a real child window from another process docked in a
  panel rectangle — window class + WndProc, style-strip-then-`SetParent`, a clip child
  that hides a guest's self-drawn caption, batched geometry through one DPI authority,
  focus routing, job-object teardown, and the `embed`/`set_bounds`/`detach`/`close`/
  `poll` commands that line up one-for-one with the Python Protocol. It is proven
  against a **synthetic guest process** (`src/bin/workbench-guest.rs`, debug-only), so
  the whole mechanism is exercised by `cargo test` with no Office installed, and four
  documented Win32 behaviours turned out to be false in this configuration —
  `WM_PARENTNOTIFY`, click routing, `AttachThreadInput`, `SWP_ASYNCWINDOWPOS` (see
  ARCHITECTURE.md). **Word is now docked for real** (PR 4): opening a `.docx` in the
  desktop shell starts a *private* Word through COM (~1.6 s), proves the window is one
  Workbench started, reparents it into the panel, and follows the panel as it is
  resized, hidden behind another tab, and closed — leaving no `WINWORD.EXE` behind.
  Measured on the author's machine, click to docked: **1.7 s**. What landed with it:
  the `ShellHostBackend` (COM + Job Object for the process, `/ws/office-host` command
  channel for the window, because `SetParent` can only run in the shell), the
  self-registering **host panel** (one new module plus one line in `ui/src/tools.ts`),
  `set_visible` — a real window does not hide because a `div` did — and the fallback
  chain: no shell, no Office, hosting off, PowerPoint, a document already open
  elsewhere, a launch that failed, an embed that was refused all end in a working
  OnlyOffice editor or the degraded card, never a broken panel. **Hang isolation is
  proven and `auto` is on**: a wedged guest costs a resize frame ~19 µs instead of
  ~1 s (see the decisions log for the full table and the control). Still open here:
  the COM bridge (agents reading and writing the *live* document, and with it the
  Office skills), Excel beyond the launch path, and packaging — the shell runs from
  source (`cd desktop && npm run tauri dev`); a bundled installer that carries its own
  Python needs `tauri build` work not done yet.
- ~~**Visual plan artifacts** as a typed product primitive: `present_plan` MCP tool →
  Pydantic `PlanArtifact` → native clickable plan cards in chat (options, steps, file
  refs); decisions return to the agent as typed JSON; pending-plan replay on reconnect
  (fix the identical PermissionRequest replay gap while there).~~ **done** — v1 renders
  live cards only (a plan is not re-rendered when resuming from a disk transcript) and
  lives in chat rather than its own dockview panel; the plan-visual authoring skill
  ships separately. Per-node annotations and a plan-level comment already round-trip to
  the agent (`PlanAnnotation`/`PlanResponse`), so the point-feedback channel is covered.
  Still open: plan nodes render as structured cards, not as **design-system-faithful
  visual mockups** — for UI proposals, a rendered preview in the app's own tokens beats
  a description of one. → v2, alongside the M7 design work.
- **Flow layer** — **done**: typed command registry (`ui/src/commands.ts`) replacing the
  3-item QuickBar and the ad-hoc keydown handler (panel focus Ctrl+1..4, tab
  cycle/close, Alt+1..9 session jump, Ctrl+Shift+P command mode, explicit
  xterm/Monaco pass-through policy — `DESIGN.md` §6.8), `SessionStatusEvent` fan-out on
  `/ws/events`, status bar with live session chips + `document.title` attention badge,
  toast layer for currently-silent failures, chat markdown/code rendering with per-tool
  settle + expand (`ToolSettled` frames), terminal tabs (N PTYs, kill the
  single-instance remount), file-tree CRUD wiring the endpoints that already exist,
  dirty-close confirmation + beforeunload guard, real session titles + live/disk dedupe.
  ~~*Known gap*: the beforeunload guard and the `document.title` attention badge are
  browser-only — WebView2 honors neither on native window close/title, so the Tauri
  shell task above must re-wire both natively~~ — **resolved** with the shell:
  `CloseRequested` is held and answered by the same confirm modal (across every dirty
  buffer at once), and the badge sets the native window title. Both are reached through
  `ui/src/shell.ts`, so the browser build keeps `beforeunload` and `document.title`.
- ~~**shortcuts.md**: workspace `.workbench/shortcuts.md` + global file, merged, watched
  live; entries drive QuickBar commands, terminal snippets, chat prompt templates, and
  custom keybindings~~ **done** — format spec in `docs/shortcuts.md`. Entries *insert*
  and never execute (no `run:` option, shell bodies single-line, no trailing newline),
  so a hostile workspace file can add QuickBar rows but not actions. Still open: agents
  get a skill to add entries on request (ships with the skills bundle).
- ~~**Tool registry** — *the foundation of the Modular track below; build it before any
  further panel lands*: panels/commands/skills register in one place instead of
  hardwiring in `App.tsx` (product principle 1). Registration carries an
  **agent-ergonomics budget**, enforced through machinery that already exists rather
  than a new harness:
  output format is a required typed field on registration (so `mypy --strict` fails an
  omission), and each tool's own tests assert a ceiling on description length and on the
  serialized size of a representative result (so the quality gate fails bloat). Thin
  calls over wrapped APIs, compact text over pretty JSON, short descriptions — every
  description is loaded into every session's context. Latency is deliberately *not*
  budgeted: these are in-process local calls where the model and the user dominate.~~
  **done** — built as M5 item 1; see there for what landed. Standing note: revisit the
  budget with a real aggregate measurement if the tool surface passes ~20.
- ~~**Provenance badges**~~ **done**: `services/provenance.py` attributes a file
  change to the session whose `Write`/`Edit`/`MultiEdit`/`NotebookEdit` named exactly
  that path within a 10 s window (`FileProvenanceEvent` on `/ws/events`, `GET
  /api/provenance` for load and reconnect); the tree marks unacknowledged files, the
  editor carries a one-line bar naming the session and linking back to that
  conversation, and opening acknowledges (the bar stands until the claim is retracted
  or the user dismisses it — DESIGN.md §6.1). Deliberately conservative: a
  change with no matching tool call is reported *unattributed* — never assigned to the
  most recent session — two sessions inside the window resolve as most-recent-exact-
  match-wins, a claim from a tool that was declined or came back an error is withdrawn
  before it can explain anything, and a later unattributed change (including every
  write Workbench makes for the user: save, create, rename, OnlyOffice callback)
  clears the claim. **Limitation**: the map
  is in-memory and bounded (LRU, 500 paths), so a server restart forgets who changed
  what; persisting it (and attributing writes made through the shell) is future work.
- Committed carryover: pptx E2E fidelity pass and bundled skills —
  `plan-visual`, `remember` and `workbench-dev` ship as the session-scoped `workbench`
  plugin; `validate`, `loop-objective` and Workbench-authored Word/Excel/PowerPoint
  skills remain, the office ones following the COM bridge rather than wrapping a
  third-party CLI (see the decisions log). Every bundled skill passes a **vetting bar**
  — read in full (a skill can execute anything on the user's machine) and shown to help
  before it ships; popularity is not evidence.
- UI quality tooling starts here: eslint + vitest **done** (`npm run lint` / `npm run
  test`, both in the CI ui job); ~~Playwright E2E still pending (standing bar)~~
  **done** — `ui/e2e/` drives the built UI against a real backend in a per-run temp
  workspace (`npm run e2e`, chromium, own CI job with the HTML report uploaded on
  failure). Nine journeys: files (create → Monaco → Ctrl+S → watcher reload → conflict
  → dirty-close), terminals (real ConPTY, tabs, surviving scrollback), QuickBar +
  shortcuts.md (categories, keycaps, the snippet that is typed but never run, the
  problems toast), chat streaming with per-tool settle, plan cards (recommendation
  pre-selected → switch → approve → the agent's echo), status chips + `document.title`
  attention badge, office degraded mode, and provenance (agent write → tree marker →
  file bar → back to the session → acknowledged, plus the changes that are *not* the
  agent's: a failed write, the user's own saves). Agent journeys run against **fake-agent
  mode** (`WORKBENCH_FAKE_AGENT=1`, `services/fake_agent.py`): scripted replies, tool
  calls, permission prompts and plan artifacts through the real factory/bridge seams —
  no Claude login, no tokens, deterministic frames.

## Three tracks from here (2026-08-05)

M4's Office work, the modularity work below and the performance work are **parallel
tracks, not a sequence**. They touch different parts of the codebase (Office: Rust host +
Python COM bridge; Modular: the UI shell and registry; Feel: the startup path, the
watcher protocol and the motion layer), so they run at once. The milestone table stays as
the record of scope; the tracks are how it gets built.

- **Moat track** — the Office host sequence (M4): ~~domain layer with a fake backend~~
  → ~~Rust window hosting~~ (both **landed**) → Word docked → COM bridge + agent tools
  → Excel. What no competitor can copy quickly.
- **Modular track** — M5 below, reordered so the *seam* comes first. What the product
  feels like every day.
- **Feel track** — performance and motion. What the product feels like every *second*.

### Feel — performance and motion

Opened 2026-08-05 on the owner's change request below. A productivity tool that hesitates
breaks the user's flow, and no amount of capability buys that back.

**Baseline, measured** (2026-08-05, author's machine, 5,005-file workspace — recorded so
nobody has to measure it again): launch **2.7–2.9 s**; file tree with rows at **1.6 s**;
the entry chunk is **88% Monaco**; the terminal runs on xterm's **DOM renderer**; a 1.5 MB
terminal burst arrives as **21,785 WebSocket frames**. The lane's own reproducible
numbers, on the generated fixture, are in `ui/e2e/perf/*.spec.ts` next to each budget.

**The lane** (landed, PR 1 of this track): a generated 5,005-file fixture
(`server/tests/perf_fixture.py`), work-shaped budgets in `server/tests/test_perf_budgets.py`,
a separate Playwright config (`ui/playwright.perf.config.ts`, `npm run perf`) collecting
navigation/paint timing, event timing at `durationThreshold: 0`, long tasks, long
animation frames and rAF intervals, and a CI job where the **counts block and the
milliseconds report**. First fix landed against it: the agent-session folder list walked
the whole workspace to read three names, concurrently with the real tree request — one
`os.scandir` now, and the tree is clickable ~490 ms sooner.

**Queued**: the watcher protocol (twenty file changes cost twenty full walks and 9.4 MB of
JSON today — the xfail budget in `ui/e2e/perf/watcher.spec.ts` is its acceptance
criterion), Monaco off the entry chunk, the terminal's renderer and frame coalescing, and
a virtualised file tree.

**Motion, and the interlock that was missed.** The track is not only speed: an instrument
that moves *well* reads as fast even when it is not. The **motion vocabulary** was
supposed to land **before** M5 item 2 (the layout system), so panel transitions would be
born with it. It did not: the layout system landed first (PR #34) and focus mode shipped
teleporting, and the vocabulary landed after it as a retrofit.

**What the vocabulary is** (landed): spring-based rather than fixed-duration, as
`DESIGN.md` §5 — rewritten in the same PR, because the doctrine it replaced *forbade*
animating tab activation, panel resize, tree expand/collapse and the theme switch, and
that restraint is half of what made the app read as static. Two easings and four
durations, derived from stiffness/damping/bounce in `ui/src/design/springs.ts` and
emitted as CSS `linear()`; two channels (travel = `transform`, tint = opacity/colour) so
`prefers-reduced-motion` can zero the travel and keep the colour feedback; and a
conformance test in the perf lane that fails the build on an animated layout property,
a `transition: all`, a static `will-change` or a hover that eases in.

**What the retrofit cost**, stated so the next interlock is taken seriously: focus mode
and layout switches are animated from `ui/src/motion.ts` *after* dockview has already
rearranged the grid — a replay of an arrival rather than a transition into it. Written in
the other order, the layout system would have handed its own before/after geometry to the
motion layer and the panels could genuinely travel between arrangements. Nothing is
wrong; it is one indirection and one less expressive move than it would have been.

**Exit criterion**, in the budgets' own terms: on the 5,005-file fixture, cold launch
reaches a clickable file tree **under 800 ms**; twenty watcher events cost **zero** full
tree walks; expanding the 2,000-file directory costs **no frame over 100 ms**; and every
`@wallclock` ceiling in the perf lane has been ratcheted down to ~1.5x its measured value
rather than the ~2.5x they start at.

**Why the registry goes first, before any other panel.** Every capability still queued —
the Office host panel, the Mission Control board, the validation review panel, the
objective strip — is a panel plus commands plus shortcuts. Built today, each one edits
`App.tsx` by hand: six more central edits, six more merge conflicts between parallel
lanes, and "modular" stays a slogan. Built after the registry, each one *registers
itself* and touches no shared file. The registry is therefore not a feature but a
throughput decision: it is what lets several lanes run without colliding, and it is the
seam a user-authored plugin later plugs into. One PR now, paid back six times.

### M5 — Modular & Parallel (the instrument feel, then the fleet)

Ordered by what unblocks what.

1. ~~**Tool registry** (listed in M4, built first here): a typed registry where a
   capability declares its panel, its commands, its default shortcuts and its
   agent-facing tools in one place. `App.tsx` stops naming panels. Exit criterion: a
   new panel can be added without editing any file that another lane is likely to
   touch.~~ **done** — `ui/src/registry.ts` (the `WorkbenchTool` type + pure
   derivations) and `ui/src/tools.ts` (the array). All five existing panels register
   themselves, including their own commands: saving and tab cycling are the Editor's,
   `Alt+T` the Terminal's, `New agent session` and the `Alt+1..9` jumps the Agent's,
   and Office contributes a *document view* rather than a panel — the same field the
   native Office host will claim. `App.tsx`, `commands.ts` and `StatusBar.tsx` name no
   capability any more — down to the Agent tab's attention dot (a `badge` on the
   descriptor) and where a `shortcuts.md` entry is typed (`shortcutKinds`, so the
   router asks the registry which panel hosts a kind); `Ctrl+1..4` is derived from the
   panels in the default layout, in registry order, rather than from four fixed ids.
   **Exit criterion demonstrated**, not claimed: the Scratchpad tool
   (`ui/src/panels/Scratchpad.tsx`) is a panel, a command, a tab icon and a file on
   disk, added in one new module plus one line in `tools.ts` — asserted end-to-end in
   the QuickBar journey, opened and closed again. It claims no chord on purpose: a
   registered chord beats a `shortcuts.md` one and `Alt` is all that file may bind, so
   a chord taken here is one the user cannot have. Server side,
   `services/agent_tools.py` is the *only* registry for agent-facing tools (name,
   description, input schema, required `output_format` and `max_result_bytes`), and
   `test_agent_tools.py` binds the ergonomics budget: a ceiling on every description
   and a per-tool ceiling on the serialized result, sized from the measured payload,
   plus compact JSON instead of the pretty-printed `get_workspace_state` payload we
   were paying for on every call. Deliberately deferred: registration is static — no
   dynamic plugin loader — but every derivation takes a tools array rather than reading
   `TOOLS`, which is the seam one plugs into (`ARCHITECTURE.md` §Tool registry,
   `docs/tools.md`).
2. ~~**Layout system** — the "work full screen" gap. dockview already supports far more
   than we use: panel **maximize / focus mode**, floating and popped-out panels, and
   full serialization. Add named, savable layouts ("review", "writing", "three
   agents") switchable from the QuickBar and from `shortcuts.md`, plus **layout
   persistence across restarts** — today every reload throws your arrangement away,
   which is the single most anti-premium behaviour left in the app.~~ **done** — and
   built as a registered capability with **no panel** (`ui/src/panels/Layouts.tsx`),
   which is the point: focus mode, persistence and named layouts arrive as commands, a
   status chip, a `shortcuts.md` kind and one `onDockReady` hook, and `App.tsx` names
   nothing. `Alt+M` fills the window with the focused panel and gives the arrangement
   back exactly (dockview's own hidden-view bookkeeping). The arrangement is debounced
   into `<workspace>/.workbench/layouts.json` — *in* the workspace, so different
   projects keep different windows, next to `shortcuts.md` and already gitignored;
   the server stores it verbatim because its shape belongs to a UI library and its
   validity is a registry fact. Presets (`Review`, `Focus`, `Agents`) name **tool ids**
   rather than geometry, so one naming a tool that is gone builds without it. The part
   that would actually have broken in the field is handled explicitly: dockview
   restores a panel whose component is unregistered and kills the render, so every
   persisted layout is pruned against `panelComponents(TOOLS)` first — unknown panels
   dropped, empty groups collapsed, the rest kept, one toast — and a corrupt file, an
   unusable layout or a throwing `fromJSON` all resolve to the default arrangement.
   `shortcuts.md` gains a third kind, `layout`, the first that *acts* rather than
   inserts; it stays inside the never-execute doctrine because its whole vocabulary is
   the name of an arrangement the user saved. Deliberately deferred: floating and
   popped-out panels (dockview supports both; nothing asks for them yet), a saved
   layout does not gain a panel that was added to the default *after* it was saved
   (switch to Default, or open it from the QuickBar), and switching to a preset rebuilds
   the dock — so the terminals in it restart, while switching to a *saved* layout reuses
   the panels that are already there.
3. **Visual artifacts — a typed scene graph agents can draw with** — *in progress*
   (PRs 1–2 landed: the schema and its renderer). Asked for after watching an
   agentic-workflow video where the agent renders an interactive artifact instead of
   a wall of text. The third-party tool that does it (lavish-axi) **failed vetting**
   — undisclosed on-by-default telemetry, a skill that `npx`'s an unpinned package
   with sandbox-evasion fallbacks, and, worst, a bridge letting any script inside a
   model-authored artifact write into the agent's instruction channel with no user
   gesture — so this is built our way: a fifth `PlanNode` kind whose payload is a
   **typed, depth-2, non-recursive scene graph** (`models/visuals.py`). The model
   sends structure and numbers; Workbench computes every coordinate and emits every
   pixel (`ui/src/visual/`). No markup, HTML, SVG, CSS or URL ever crosses the wire,
   which keeps the closed-union posture that makes plan cards immune to XSS and
   exfiltration while removing the expressiveness limit that prompted the request.
   Leaves: `table` (typed columns — numeric renders in tabular figures because the
   *schema* says so), `chart` (line/bar/step/scatter on typed axes), `diagram`
   (nodes and edges; we lay it out as a layered DAG), `code_diff`, `metrics`.
   **Owner decisions, recorded:** domain types **yes** — a DST-aware time axis that
   draws a 23- and a 25-hour day correctly (asserted on real Nordic clock-change
   dates on both sides of the wire) and `step` as first-class, because a dispatch
   schedule drawn as a line is a lie; a **live workspace** artifact **yes**; and
   **persisting artifacts to `.workbench/` yes** — the latter two are *later PRs*,
   not these. Still open: annotation anchors and annotate mode (PR 3), expanding an
   artifact into a dockview panel (PR 4), live refinement and note batches (PR 5),
   persistence (later), and component/design-system specimens (M7-gated, alongside
   the "design-system-faithful visual mockups" gap logged in M4).
4. **Deeper shortcuts** — `shortcuts.md` grows beyond snippets and prompts: ~~bind a
   layout~~ (**done** with item 2: `type: layout`), a registered tool, a workspace jump,
   or a saved agent objective to a chord. The file becomes the user's own control
   surface over everything the registry knows. The seam is in place —
   `shortcutActions` on the descriptor routes a kind to the tool that carries it out —
   so each further kind is a parser case plus a handler, not a new mechanism. The bar
   every one of them has to clear is the one `layout` cleared: it may not run a command,
   send a prompt, or reach a file, because a workspace file is untrusted input.
5. **Workspace switcher** — the workspace is currently whatever directory the server
   was launched from. Switch projects from inside the app (recent list, QuickBar,
   `shortcuts.md`), with per-workspace layout and session history. Supersedes the
   first-run picker in the OSS bar item 3.
6. **Managed worktree pool**: backend `WorktreeService` (acquire/release/reap,
   dirty-slot `needs_review` protection, per-slot watchers), multi-root file/terminal
   access through a root registry (path jail preserved per root), worktree-bound agent
   sessions. Parallel projects that cannot step on each other.
7. **Mission Control board** (registers as a tool, per item 1): all sessions as cards
   (status, current activity, cost), inline permission chips answerable from the board;
   orchestrator session kind with a mission-control MCP toolset
   (spawn/list/read/send/wait/stop workers), worker budget + cost ceiling,
   escalate-to-board permission policy (never auto-allow shell).
8. **Security hardening pulled forward** (OSS bar item 1): per-launch auth token
   injected into the UI + strict WS Origin checks — agent-spawned workers, multi-root
   access and a workspace switcher all widen the unauthenticated localhost surface
   unacceptably.

**The endgame this points at** (M7+, stated here so the seam is built for it): once
capabilities register themselves, a *user* can add one. A documented tool contract plus
the existing `.workbench/` conventions make user-authored panels and tools possible
without forking — the difference between a fixed app and an instrument.

### M6 — Proof (validation + objectives)

- Validation pipeline with evidence: post-done staged run in an isolated worktree —
  intent-from-transcript, rebase, adversarial fresh-context review, ruff/mypy/pytest
  gates, intent-directed E2E with recorded screenshots/video/logs — evidence gallery +
  risk badge in a Review panel, one mandatory human approval before push/PR, PR
  babysitter with bounded retries. Runnable standalone on any branch (principle 1).
- First **domain gate** ships as proof of the moat: numeric reconciliation between a
  workbook and the code that produced it.
- Objective sessions: server-enforced loops (iteration/token/wall-clock caps in code,
  unattended deny-and-log permission policy), telemetry strip, morning-after per-commit
  diff review linked to iteration transcripts.

### M7 — Premium & Public (identity + OSS release)

- The logged "frontend is too plain" change request executed in full: aggressive
  ui-ux-pro-max design overhaul on top of the now-complete structural layer —
  distinctive welcome surface, branded empty states, micro-interactions, Monaco
  enrichment, content search (Ctrl+Shift+F), settings UI. (Layout persistence and
  dockview maximize moved to M5 item 2 and **landed** there; floating and popped-out
  panels are still unclaimed — dockview supports both and nothing has asked yet.)
- Voice input as an optional extra (local faster-whisper, push-to-talk, domain
  vocabulary initial prompt).
- Remaining OSS product bar: first-run experience (workspace picker, Claude-login and
  Office/OnlyOffice detection walkthroughs), cross-platform PTY + 3-OS CI matrix,
  CONTRIBUTING/templates, versioned Tauri releases with signed installers,
  zero-telemetry README stance, and the real product name.
- Exit criterion: a stranger on a fresh Windows machine reaches a working, secured,
  distinctive product in under ten minutes.

## Decisions log

- 2026-08-04 — Prior-art check (GitHub + products): no existing tool combines IDE +
  full-fidelity Office editing + multi-session Claude agents. Closest: AionUi/OfficeCLI
  (agent-mediated docs, no direct editing), Nimbalyst (sessions + worktrees, no Office).
  Verdict: build our own; adopt OfficeCLI as an agent-side skill after vetting.
- 2026-08-04 — OnlyOffice Docs Community via **native Windows installer** (port 8880);
  no Docker anywhere. Univer rejected for v1 (xlsx import/export is Pro-only).
- 2026-08-04 — Auth: machine's existing Claude Code subscription login;
  `ANTHROPIC_API_KEY` as documented alternative.
- 2026-08-04 — **Office pivot**: native window hosting of real installed Office
  (SetParent + child styling + COM automation bridge), spike-proven same day (~1 s
  embed of real Word inside a host window; Excel `XLMAIN` / PowerPoint `PPTFrameClass`
  host the same way). Known engineering: hide Office's self-drawn caption row by
  clipping, manage focus/z-order, Excel needs `/x` for an owned instance, modal dialogs
  float. OnlyOffice demoted to preview/diff/fallback. Consequence: Tauri shell moves
  into M4 core. Rejected: OLE in-place embedding (dead), WOPI/Office-for-the-web
  (enterprise licensing or cloud storage — breaks local-first), window-hack-free
  "launch externally" (user verdict: documents must live inside the program).
- 2026-08-04 — **Product reshape** after agentic-workflow bar review + competitive
  sweep (9-agent analysis): milestones restructured to M4 Instrument / M5 Parallel /
  M6 Proof / M7 Premium & Public; north star and product principles added above.
- 2026-08-04 — **Agent ergonomics is a product constraint, not a nicety** (second pass
  over the same agentic-workflow source). Reported measurements: an MCP wrapper around
  GitHub cost ~3x the tokens and ~2x the latency of the plain CLI for identical tasks,
  and token-efficient output formats saved ~40% over JSON. Consequence: tools carry a
  measured budget at registry time (M4), not a review comment after the fact. Same pass:
  skills get a vetting bar (a 177k-star skill benchmarked *worse* on results while using
  ~5% more tokens), and bug fixes must open with an end-to-end reproduction. Both are
  now `CLAUDE.md` standards.
- 2026-08-04 — **Ergonomics budget narrowed to something that can fail** (revision of
  the entry above, same day). The first wording — "measured for token cost and latency
  before it lands" — named no mechanism, and a registry field merely *recording* a
  measurement is decoration. Replaced with enforcement through existing machinery: a
  required typed output-format field (caught by `mypy --strict`) and per-tool test
  assertions on description length and serialized result size (caught by the quality
  gate). No benchmark harness: the source's 3x finding came from a large third-party
  tool surface, while Workbench owns ~10 agent-facing tools. Latency dropped — these are
  in-process calls where the model and the user dominate, so budgeting it would promise
  a measurement nobody takes.

- 2026-08-04 — **Bundled skills ship as one session-scoped local plugin**, and both
  third-party sources are out (revises the prior-art entry above). *OfficeCLI:
  rejected after vetting* — its `SKILL.md` has the agent fetch and run a remote
  install script without consent, and overwrite global agent configuration in a way
  we could not verify; owner sign-off same day. *Anthropic's xlsx/docx/pptx skills:
  license-blocked* — those directories are published all-rights-reserved, so an OSS
  package cannot redistribute them. Substituted with Workbench-authored skills; the
  office ones follow the COM bridge (which reads and writes the *live* open document),
  so wrapping a file-based CLI was the wrong shape regardless. Mechanism:
  `ClaudeAgentOptions.plugins` → `--plugin-dir`, namespaced `workbench:*`, nothing
  written to `~/.claude`. `skills="all"` rejected — it appends a bare `Skill` to
  `allowed_tools`, shadowing the permission callback and auto-allowing every
  discovered skill. Instead the two skills something *tells* the agent to use
  unprompted (`plan-visual`, `remember`) get narrow `Skill(workbench:<name>)` rules,
  which the SDK's own rule parser treats as specifiers and so do not shadow the
  callback; everything else still prompts. Same change: sessions load the
  workspace's settings and nothing above them — `setting_sources=["project",
  "local"]`, i.e. `.claude/settings.json` **and** the machine-local
  `.claude/settings.local.json`, so a folder's own "always allow" rules and hooks
  behave here as they do in plain Claude Code, while the global `~/.claude` scope
  is dropped. `WORKBENCH_INHERIT_USER_SETTINGS=1` restores that global scope in
  full (hooks and permission rules, not just skills), which is why it is not named
  after skills.

- 2026-08-05 — **Office host: three decisions taken before any native code** (owner),
  encoded in the domain layer that landed with them. (1) **Never adopt a process we did
  not launch**: reparenting is destructive, so a document already open in an instance we
  did not start is a first-class refusal with a reason
  (`document_open_elsewhere`), never a silent takeover — the pid is bound at launch and
  every later operation is checked against it. (2) **PowerPoint is preview-only in v1**:
  it is single-instance and exposes no `Application.Hwnd` to prove a window is ours, so
  hosting it risks reparenting the user's own open presentation; the service refuses it
  outright. (3) **Native hosting stays behind `WORKBENCH_OFFICE_NATIVE`, and `auto`
  resolves to *not* hosting** until hang isolation is proven — with
  `GET /api/office/capabilities` reporting that plainly, so the UI degrades to the
  OnlyOffice path from a fact rather than a guess. Browser mode remains fully supported.

- 2026-08-05 — **Native hosting: four documented Win32 behaviours measured false**, and
  one product risk quantified (PR 3, `desktop/src-tauri/src/host/`). For a window
  reparented in from another process: `WM_PARENTNOTIFY` never arrives (so destruction is
  found by polling — the Protocol's `poll` is the *only* crash signal, not a backstop);
  click-to-focus needs no code because the guest focuses itself; `SetFocus` across the
  process boundary needs no `AttachThreadInput`, because being a child already attached
  the input queues; and `SWP_ASYNCWINDOWPOS` does **not** protect against a hung guest,
  for that same reason — it only posts across *different* input queues, and
  `DeferWindowPos` rejects the flag outright. Consequence for the product: a wedged
  guest leaves the host window painting, pumping and not judged hung by Windows, but
  makes each resize frame cost ~1 s. The fix is measured, not guessed — the same move
  from a thread that owns no window in the parent chain takes ~0.15 ms — and is
  deliberately left to its own PR. `WORKBENCH_OFFICE_NATIVE=auto` stays off until then,
  as decided.

- 2026-08-05 — **Hang isolation is proven, so `WORKBENCH_OFFICE_NATIVE=auto` is
  ON** (PR 4). The decision above made native hosting conditional on one number.
  Re-measured on this machine, through the production path, with the guest
  deliberately wedged (`hosting_tests::hang_isolation_measurement`, run with
  `cargo test -- --ignored --nocapture`):

  | 10 resize frames, guest hung | before | after |
  |---|---|---|
  | our panel + clip child | 69 µs | 69 µs |
  | including the guest | **~10 s** (≈1 s/frame) | **187 µs** (≈19 µs/frame) |

  The control still costs what it always did: one direct `SetWindowPos` on the
  hung guest, from the main thread, took **9.98 s** in the same run — so the
  number above is containment, not a fixture that failed to hang. The host also
  keeps its own message loop (50/50 posted messages dispatched), keeps painting,
  and is not judged hung by Windows; and a frame issued *during* the hang really
  lands once the guest recovers, so nothing is being dropped instead of
  deferred. The mechanism is a worker thread that owns no window in the parent
  chain (`host::mover`), because input-queue attachment — the thing that gives
  us focus routing for free — is what made the old path wait.

  **Consequence:** `auto` now resolves to hosting natively wherever the machine
  can, i.e. Windows + an Office to launch + the desktop shell attached. Any of
  those missing is reported by `GET /api/office/capabilities` and the UI falls
  back to OnlyOffice. `off` still wins over everything.

- 2026-08-05 — **Two Office behaviours measured, and the design changed for
  both** (PR 4). (1) Opening a document that another instance already has open
  does not fail and does not raise — it **blocks indefinitely** behind a "File
  In Use" prompt that `DisplayAlerts = wdAlertsNone` does not suppress (four
  minutes, still waiting). (2) Office also stops mid-launch to ask questions of
  its own: "the last time you opened this, it caused a serious error" appeared
  after a killed instance and blocked `Documents.Open` for three minutes. Both
  would wedge the single COM apartment thread, and with it every host after it.
  So: the "already open" question is answered *before* the open, from the
  **Running Object Table** — which sees a document opened by an instance we did
  not launch, and (measured) does *not* see the one behind a stale `~$` lock
  file, while a live owner file cannot be told from a stale one by opening it —
  and the process is identified and put in its Job Object **before**
  `Documents.Open` is called, so a launch that never returns can be ended from
  another thread instead of leaking a Word and a worker.

## Open-source product bar (standing directive, 2026-08-04)

Build for real external users, not just the author. Consequences, tracked as work:

1. **Local-API security hardening** — per-launch auth token + strict WS Origin checks
   (DNS-rebinding defense). Scheduled: **M5** (pulled forward; see above).
2. **Cross-platform**: ptyprocess-based POSIX PTY + CI matrix (windows/ubuntu/macos).
   Scheduled: M7. No Windows-only assumptions outside `pty_manager.py`; the Office host
   is Windows-only by nature and degrades to OnlyOffice elsewhere.
3. **First-run experience**: workspace picker, "Claude login not found" guidance,
   Office/OnlyOffice detected-or-degraded. Scheduled: M7.
4. **Contributor experience**: CONTRIBUTING.md, ARCHITECTURE.md, issue/PR templates,
   good-first-issue labels. Scheduled: M7.
5. **Release engineering**: versioned GitHub releases with Tauri installers, changelog,
   signed artifacts if feasible. Scheduled: M7.
6. **Privacy stance**: zero telemetry, stated in README. Files never leave the machine
   except through the user's own Anthropic account.
7. **Naming**: "workbench" is a placeholder — pick a unique, searchable name (check
   GitHub/PyPI/npm availability) before publishing. Scheduled: M7.

## Change requests

- 2026-08-04 — **Frontend is too plain** (user): current UI reads as a generic VS Code
  clone; the design pass must go beyond token compliance. Redo with the ui-ux-pro-max
  skill used aggressively (style databases, component patterns, motion presets) to give
  the app a distinctive visual identity. → M7.
- 2026-08-04 — **Premium productivity bar** (user): the product must reach and exceed
  the terminal-captain agentic workflow (visual planning, managed worktrees, validation
  with evidence, long-running objectives, orchestrator) — GUI-natively. This is a
  productivity tool; smooth and premium is the point. → M4–M7 reshape (this document).
- 2026-08-04 — **Modularity** (user): all capabilities usable together *or*
  independently — a workbench of tools, not a pipeline. → Product principle 1 + tool
  registry (M4).
- 2026-08-04 — **shortcuts.md** (user): a user-editable shortcuts file integrated
  across all programs (terminal snippets, saved prompts, commands, keybindings) to save
  time everywhere. → M4.
- 2026-08-04 — **Real Office** (user): "we need to be able to use word, excel and pptx
  directly in the program" — OnlyOffice look-alikes are not the real thing. → Office
  host pivot (M4); OnlyOffice demoted to preview/diff/fallback.
- 2026-08-04 — **Think big** (user): standing directive encoded as product principle 3
  and in `CLAUDE.md`.
- 2026-08-04 — **Agentic-workflow review, pt. 2** (user): four adoptions from a second
  read of the same terminal-captain workflow source. (1) Tool ergonomics measured at
  registry time → M4 tool registry. (2) Point-feedback on plan artifacts → already
  shipped in #17; the open remainder is design-system-faithful visual mockups → v2/M7.
  (3) End-to-end reproduction before a bug fix → `CLAUDE.md` standard. (4) Skill vetting
  bar for the bundle → `CLAUDE.md` standard + M4 carryover. See Decisions log for the
  measurements behind (1) and (4).

- 2026-08-05 — **It has to feel instant** (user): the app has "an old sluggish feel"; it
  must feel like a MacBook or an iPhone — "insanely fast and smooth" — because this is a
  productivity tool and latency breaks flow. Speed is therefore a gate, not a polish
  item: budgets that fail a build, not a benchmark someone runs. → **Feel track** above,
  opened with the perf lane and the double-walk fix; motion vocabulary interlocked ahead
  of M5's layout system.
- 2026-08-05 — **Modularity is the plan, not a polish item** (user): after using the
  app, the gaps named as "we don't have it" — fixed layout with no full-screen/focus
  mode, no layout persistence, panels hardcoded in `App.tsx`, no workspace switching —
  are the difference between an app and an instrument, and belong in the plan rather
  than a later design pass. → M5 reordered as the **Modular track**, run in parallel
  with the Office moat track, registry first.

- 2026-08-05 — **Visual artifacts, built rather than adopted** (user): after an
  agentic-workflow video showing an agent render an interactive artifact instead of a
  wall of text, the third-party tool that does it (lavish-axi) was read in full and
  **rejected** under the skill-vetting bar: undisclosed on-by-default telemetry, a
  skill that `npx`'s an unpinned package with sandbox-evasion fallbacks, and a bridge
  letting a script inside a model-authored artifact write into the agent's instruction
  channel with no user gesture. Build the capability our way — a typed scene graph the
  model fills with structure and numbers while Workbench draws every pixel. Three owner
  decisions on top: **domain types yes** (a DST-aware time axis and first-class `step`,
  because a generic renderer lies about a 23- or 25-hour day and about a dispatch
  schedule), **live workspace artifacts yes**, and **persistence to `.workbench/`
  yes** — the last two are later PRs. → M5 item 3.
