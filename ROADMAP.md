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
4. **Composable surfaces — no fixed panels.** Every capability is an *instantiable
   surface*, not a place in the window. A pane is a `(tool, instance)` pair: the
   instance id is the dockview panel id, and "what am I pointed at" is a small
   serializable `params` record (`{sessionId}`, `{ptyId}`, `{path}`, `{folder,query}`)
   carried in the saved layout. A tool declares whether it is singular or plural, and
   **plural is the default for anything a user could plausibly want twice** — twice is
   the baseline, not a feature request. Three rules a contributor applies without
   asking: **(a) nothing assumes it is the only one of itself** — not in its component,
   not in `store.ts`, not in a CSS selector, not in a test; an `activeX: X | null` field
   in the app store *is* the shape of a singleton assumption and needs a comment saying
   why the window really has only one. **(b) A pane is a view onto a resource it does
   not own** — the PTY, the SDK session, the Office window and the Monaco model live
   server-side or in one module-level registry keyed by their own id; `params` merely
   names one. Closing a pane closes a view; whether the resource dies with it is the
   tool's written decision, never an accident of unmount. **(c) A restored pane is
   vetted before it is believed** — layouts persist, resources do not, so every plural
   tool implements `adopt(params)` and a pane whose resource is gone renders a *named
   tombstone* with the one action that recovers it (Reconnect, Resume, Reopen, Open in
   Word), and a cap that is hit shows the cap and the setting that raises it, never a
   dead button. The test of the principle: if the user can imagine the arrangement, the
   app expresses it — we do not ship arrangements one at a time. (Owner directive
   2026-08-05; binding standard in `CLAUDE.md`; enforcement in M5 item 9.)

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
  **Composition is now part of this sequence's definition of done** (owner, 2026-08-05:
  "a tab where I work on Word and Excel side by side in full screen"). Under product
  principle 4 this needs no second Office registration and no new panel: Word already
  lands as a `documentView` claiming kind `office`, so *Word beside Excel* is editor
  plurality (two `editors#<path>` panes) plus two concurrent hosts, and "full screen" is
  the `Alt+M` that already ships. (That same `Alt+M` — focus mode, M5 item 2, **done** —
  is the whole answer to the owner's separate ask, *"if I want only code over my full
  screen I should have that"*: it maximizes **any** pane, code or otherwise, and needs
  nothing from Office or from panes.) The server is already instance-shaped for it —
  `OfficeHostInfo.host_id`, one host per document, a `rect: PanelRect` per host,
  `POST /api/office/host/{id}/bounds`, `OfficeHostEvent` on the shared bus,
  `GET /api/office/hosts` replaying to a reconnecting client. What is genuinely new is
  only what appears at N>1: per-pane geometry driven from each pane's own dockview
  `onDidDimensionsChange` across split/drag/maximize (watch the unit boundary — the Rust
  commands take CSS pixels, `PanelRect` is physical, and at N panes a missed conversion
  is N wrong rectangles instead of one obvious one), focus routing between two guests,
  the second pane naming an already-hosted document rendering the existing
  `document_open_elsewhere` refusal as a "open in another pane — go there" card, and
  hang containment, which landed with Word (#38) and was a **prerequisite** rather than
  an improvement: unconstrained, one wedged guest costs ~1 s per resize frame, so with
  two docked it would be every layout interaction in the window. Sequenced as its own PR
  after the COM bridge and Excel — it is the acceptance demo for the whole Office sequence.
  Exit criterion: two real documents docked in two panes, each keeping its own rectangle
  through split, drag and maximize, with one guest deliberately wedged and the window
  still usable.
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
  → ~~Rust window hosting~~ (both **landed**) → Word docked (*in flight*, PR #38) →
  hang containment (the window-less mover thread, WIP on the same branch — it is what
  flips `WORKBENCH_OFFICE_NATIVE=auto` on, and a hard prerequisite once two guests are
  docked) → COM bridge + agent tools → Excel → **the side-by-side proof**: Word and
  Excel docked in two panes at once. What no competitor can copy quickly.
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

**Landed since**: the terminal's renderer and frame coalescing (PR #36); and **the file
tree** — the two largest measured pieces of waste left in the app, both of them here. It
now reads **one directory at a time** (`GET /api/files/dir`, one `os.scandir`), **patches
itself** from the `file_changed` events it already receives instead of refetching the
workspace, and **renders only what is on screen**. Twenty file changes went from 20 full
walks and 9.4 MB of JSON to **zero requests**; expanding the 2,000-file directory went
from 2,010 mounted rows and a single 500 ms task to ~40 rows and no long task. The xfail
in `ui/e2e/perf/watcher.spec.ts` is now an ordinary passing budget, joined by a
mounted-row count and a long-task ceiling for the expand. Convergence with disk is three
rules, not trust: expanding re-lists, reconnect re-lists, and a `tree_invalidated` frame
covers the one change no file event can describe (see ARCHITECTURE.md, *The file tree*).

**Queued**: Monaco off the entry chunk (the entry bundle is still 4.1 MB raw / 1.06 MB
gzip, ~88% Monaco — the largest single number left in the launch path), and the motion
vocabulary below.

**Motion, and a hard interlock.** The track is not only speed: an instrument that moves
*well* reads as fast even when it is not. The **motion vocabulary** — the durations,
easings and transition primitives, as `DESIGN.md` tokens — must land **before** the
transitions it governs. Panel transitions written first and animated later are panel
transitions that never get animated; born with the vocabulary, every later panel inherits
it for free. This is a sequencing constraint between two tracks, so it is stated here
rather than inside either. **Re-pointed 2026-08-05**: the interlock originally named M5
item 2 (the layout system), which shipped in #34 without it — so the constraint expired
unmet once, and focus mode shipped teleporting. Its live target is **M5 item 9 (panes)**,
because pane split, swap, close and the picker are the transitions that would otherwise
be written un-animated forever, and because both lanes edit `Layouts.tsx` and
`styles/dockview.css`. The vocabulary lands first; panes rebases onto it, never the
reverse.

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
wrong; it is one indirection and one less expressive move than it would have been — and
it is the whole argument for holding the panes interlock above.

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

Ordered by what unblocks what. Item numbers are **stable ids**, not build order: items
9–13 were added 2026-08-05 (composable surfaces) and several of them are sequenced ahead
of items 4–8 — see **Sequencing** at the end of this section. Nothing was renumbered,
because other sections and five running lanes reference these numbers.

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
   the name of an arrangement the user saved. Deliberately deferred: ~~floating and
   popped-out panels (dockview supports both; nothing asks for them yet)~~ — **claimed
   2026-08-05, now item 13**; a saved layout does not gain a panel that was added to the
   default *after* it was saved (switch to Default, or open it from the QuickBar), and
   switching to a preset rebuilds the dock — so the terminals in it restart, while
   switching to a *saved* layout reuses the panels that are already there. That last
   line becomes a **bug** the moment panes land: rebuilding the dock over instances is
   not a restart, it is data loss, so item 9 must make preset switching reconcile
   against the panes that already exist. Two other facts from that file carry into item
   9, one good and one not: `pruneLayout()` already vets by `contentComponent` and
   treats the panel key as opaque, so **the persisted layout format needs no change and
   every saved layout keeps working**; but `LAYOUT_PRESETS` name tool ids *as* panel ids,
   so no preset can currently express two of anything.
2b. ~~**The pane system — tmux, not a four-panel IDE.**~~ **first PR landed** (item 9 is
   the plan of record and stays open for the rest of it). The owner's central
   complaint after item 2 was that it "still feels like a cheap VS Code editor… I want
   something super modular like TMUX", and the registry (item 1) was the prerequisite
   that made it buildable. What tmux actually gives its users, translated: **any pane
   splits in two** (`Alt+S` / `Alt+Shift+S`, then a picker for what goes in it), **any
   pane runs anything** (every registered tool, every live session, every open file,
   a new shell), **the keyboard owns the window** (`Alt+←→↑↓` to move,
   `Alt+Shift+←→↑↓` to swap, `Alt+O` to cycle, `Alt+X` to close), and **the arrangement
   is yours** — four agent sessions in a 2×2 is now a thing you build in four keystrokes
   rather than a thing the app does not have.
   The idea that makes it survive a restart is the **pane id** (`ui/src/panes.ts`):
   `toolId` or `toolId#instanceKey`, where the key is `agent#<session_id>`,
   `editors#<path>`, `terminal#<n>`. dockview serializes panel ids into
   `.workbench/layouts.json` and nothing else about a panel, so the id *is* the
   persistence — no second store, no id map, and a saved layout brings back *those*
   conversations rather than that many empty panes. `pruneLayout` gained the matching
   vetting: an instance pane of a tool that is a singleton today, or one whose id and
   component disagree, is dropped with its own sentence, while a key that has simply not
   loaded yet is left alone (sessions arrive long after the layout does). The picker is
   the QuickBar in a new **pick mode** rather than a second overlay — a capability hands
   it rows, and `QuickBar.tsx` still names no capability. The split affordance reaches
   the tab strip through a new `groupActions` registry contribution, so `App.tsx` still
   names none either. Deliberately deferred: an editor pane for a `keepMounted` document
   view (a second OnlyOffice editor on one file is a co-editing session with yourself —
   the pane says where to open it instead); a live session's on-screen transcript is
   still not replayed after a reload, which is the pre-existing agent-socket behaviour
   and not something a pane id can fix; and swapping two panes resizes nothing but does
   not preserve a *tab group's* internal order when a pane holds several tabs. Left to
   item 9, not done here: hibernation, idle session reaping, `adopt(params)` tombstones,
   a raised `max_concurrent_sessions`, and preset switching that reconciles against the
   panes that already exist rather than rebuilding the dock over them.
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
   first-run picker in the OSS bar item 3. Also unlocks **half B of item 12**: opening a
   session whose folder is outside the current workspace needs this (or the multi-root
   jail from item 6), which is why the session browser ships its read half first.
6. ~~**Managed worktree pool**: backend `WorktreeService` (acquire/release/reap,
   dirty-slot `needs_review` protection)~~ — **the pool is done**
   (`services/worktrees.py`, `models/worktrees.py`, `routers/worktrees.py`, landed
   early and out of sequence because item 7 needs it). Still open in this item, and
   deliberately: multi-root file/terminal access through a root registry (path jail
   preserved per root), worktree-bound agent sessions, and per-slot watchers — the
   pool stands alone without them and its endpoints are usable today. No UI yet
   either: four UI lanes were live when it landed, so it shipped backend-only.
   **Four design decisions, taken from `kunchenguid/treehouse` (MIT, read 2026-08-05,
   not adopted as a dependency — a pool bound to agent sessions and inside our path
   jail has to be ours) and each of which we would otherwise have rediscovered the hard
   way** — all four now **implemented**, each with a test named after it:
   - **Detached HEAD.** A pooled worktree carries no branch, so "already checked out in
     another worktree" cannot happen. Every fix-stage agent this session had to push
     from a differently-named local branch for exactly that reason; the whole class goes
     away. *Implemented* as `git worktree add --detach`; the test proves it the way it
     bites, by watching `git checkout main` be refused *inside* a slot while the pool is
     unbothered.
   - **Pool, never destroy.** A finished worktree is *reset and returned*, not removed —
     so `node_modules`, `.venv` and build caches stay with it and the cold install is
     paid once per slot rather than once per task. This is the honest answer to the
     problem PR #32 tried to solve by junctioning `node_modules` from the main checkout
     and had to be closed over: `git worktree remove` recurses *through* a Windows
     junction and would have emptied the main checkout. Never removing the worktree
     removes the hazard along with the cost. *Implemented*, and enforced on the commands
     rather than in prose: a test watches every git argv a full
     acquire→release→discard→prune cycle runs and fails on `worktree remove`, on
     `clean -x` (which would delete the very caches this exists to keep) and on
     `--force`. The reset itself runs at **acquire** time, which is the only moment the
     pool knows what to reset *to* and the moment nothing is running in the slot.
   - **Two idle signals.** Process/owner exit *and* an explicit durable lease that
     survives with nothing running. Ours is cleaner than a subshell — a closed agent
     session or pane is an exact signal — but the lease is what stops a slot being
     reaped while an agent is still working in it unattended. *Implemented* as
     `owner_pid` + `expires_at`, reclaimed only when **both** say idle, with a renewable
     lease. The liveness probe is `OpenProcess` + `GetExitCodeProcess` and never
     `os.kill(pid, 0)` — which on Windows CPython is `TerminateProcess`, i.e. a probe
     that kills what it asks about. Its two imprecisions (exit code 259, a recycled pid)
     both hold a slot *longer*, which is the direction that costs a wait rather than an
     agent's work.
   - **Fail safe on corrupt state.** If the pool's state file is truncated or lost,
     rebuild from what is on disk and mark every slot **leased until verified** —
     assume in use, never assume free. Same instinct as the dirty-slot rule above, and
     the right default for anything that can destroy work. *Implemented* for truncated,
     unreadable, non-JSON *and* wrong-version documents; verified on a live server by
     truncating `pool.json` and restarting, which came back with all three slots held
     and the next acquire honestly refused.

   **Dirty is sacred, and it outranks all four.** A slot with tracked changes or
   untracked files is never handed out and never reclaimed without an explicit `force`,
   a `git status` that *fails* counts as dirty, and the disk beats the state file.
   What Windows really does was measured rather than assumed (a real `CreateFile`
   share-mode-0 handle): an exclusively-held file reads as `M` to `git status`, so the
   **dirty guard fires before any reset is attempted**, and the `reset --hard` behind it
   fails with `unable to unlink`. Both are protective. The reset is retried on a bounded
   budget and then becomes `needs_review` — never `--force`, because git's reset is
   already forceful and the failure is the filesystem's. Dirty protection is not a
   one-way door: every sweep re-asks, so a slot a transient lock parked comes back by
   itself. The pool root is under the machine's app data dir and **not** in the
   workspace, asserted three ways (the jail refuses it, the tree does not list it, a
   real `worktree add` through the API produces no watcher event) — inside `.workbench/`
   it would put N copies of the project in the file tree and turn every checkout into a
   watcher storm.
7. **Mission Control board** (registers as a tool, per item 1): all sessions as cards
   (status, current activity, cost), inline permission chips answerable from the board;
   orchestrator session kind with a mission-control MCP toolset
   (spawn/list/read/send/wait/stop workers), worker budget + cost ceiling,
   escalate-to-board permission policy (never auto-allow shell). **Re-scoped
   2026-08-05**: it is *the board over the activity feed*, not a board that grows its own
   feed — it renders item 10's `SessionActivityEvent` and reads item 11's usage service
   for its per-worker ceiling rather than deriving numbers a second time. Two live-fleet
   views is the duplication the composability principle exists to prevent.
8. **Security hardening pulled forward** (OSS bar item 1): per-launch auth token
   injected into the UI + strict WS Origin checks — agent-spawned workers, multi-root
   access and a workspace switcher all widen the unauthenticated localhost surface
   unacceptably.
9. **Panes — split anything, and the principle it carries** — *first PR landed*
   (`m5/split-anything`, item 2b): splitting, the pane id, the plural seam, the picker
   and the keyboard are in. Still open here: hibernation, idle session reaping,
   `adopt(params)` tombstones, the raised cap, preset reconciliation, and the
   instance-count perf budget — the exit criterion below is not met yet.
   This is the owner's "super modular like TMUX" ask and the
   implementation of product principle 4: `paneId := toolId | toolId#instanceKey`, so
   `agent#<session_id>`, `editors#<path>` and `terminal#<n>` are panes that survive a
   restart because the pane id *is* the persistence. The registry gains the plural seam
   (instance options + titles, `pluralPanelIds`, `paneVocabulary`, `paneTitle`,
   `groupActions` — the split affordance on every tab strip); commands gain a **scope**,
   so `Ctrl+S` stops meaning "save `store.activePath`" and starts meaning "save what
   *this* editor pane shows", and `Ctrl+1..N` binds positions rather than tool ids. A
   Panes tool (split, navigate, swap, move, close, pick-target) contributes no panel and
   drives the dock through `onDockReady` — the `Layouts.tsx` precedent exactly, one
   module plus one line in `tools.ts`. **Do not propose a second pane system: finish
   this one.** What it has to unpick is written down so it is not rediscovered:
   `placementOf()` setting `id: tool.id` is the singleton in one line; `focusPanel()` is
   called with *tool* ids from two places; `store.ts` keeps `activeSessionId`,
   `activeTerminalId` and `activePath` as app-global singletons that no longer have a
   justification; `AgentPanel` is simultaneously the session list and the chat, so ten
   agents in a grid cannot be expressed; `Terminal` and `EditorArea` each own a tab strip
   that should be panes; `monaco.disposeModel(path)` pulls a model out from under a
   second pane on the same file; `registry.test.ts` pins the singleton as truth and gets
   rewritten, not deleted; and the E2E helpers use unscoped selectors that would pass
   against a broken plural app. `docs/tools.md` and `ARCHITECTURE.md` write the singleton
   down as a *rule* ("exactly Editor / Files / Agent / Terminal", "`id` equal to
   `component` for every singleton panel") and must change in the same PR, or the next
   contributor re-adds the assumption. **Resource reality, measured rather than assumed**
   (2026-08-05, author's machine): eight live `claude` CLI processes held 130–674 MB
   working set each, so ten *talking* agents is 1.5–5 GB — but the SDK client is created
   lazily on first message, so ten agent *panes* cost nothing until they are used. Five
   mechanisms answer that, none of them "it scales": stated per-tool caps that render the
   cap and the setting that raises it *in the pane*; lazy acquisition made universal (a
   pane mounts chrome immediately and takes its resource on first interaction, which is
   what makes restoring a saved ten-agent layout instant); **hibernation** of off-screen
   panes that releases the *renderer* and keeps the *resource* (xterm disposed, PTY
   running; Monaco view disposed, model kept; an Office host `detach`ed) — **a working
   agent is never hibernated, because an agent working off-screen is the product**;
   tombstones over lies via `adopt(params)`; and **idle session reaping** on the server,
   which is not optional — sessions are never reaped today (`close_all()` runs only at
   shutdown), so without it "ten agents in a grid" is a memory leak with a UI. Reaping is
   visible: "session slept — Resume", never a silently dead chat. Also raise
   `max_concurrent_sessions` from 4 to 8 and keep it configurable, noting what it really
   caps: sessions *working*, not sessions open. **Not promised**: several Monaco panes
   plus several xterm panes plus two native Office windows in one WebView2 is a heavy
   window; the perf lane gains an instance-count budget (panes opened vs. long animation
   frames) that gates the grid layouts, or "modular" becomes "hesitates". Exit criterion:
   two agent panes, two terminals and two editors coexist in one window, each independent
   through a save/restore round trip, and every pane whose resource is gone shows a named
   tombstone with its one recovery action.
10. **Live agent activity — "see everywhere Claude is editing"** (registers as a tool;
    panel + command + status contribution). Provenance answers *who wrote this file I am
    looking at*, after the fact; nothing answers *show me everywhere the fleet is working
    right now*. One row per live session ordered by most-recently-active — status dot,
    title, folder, and the current tool line **replaced in place** rather than appended,
    fading to the last-completed line when a call settles. Retention is one line per
    session, so the surface is O(sessions), not O(tool calls), and with four agents
    working you see four rows changing at four different rhythms: fleet legibility
    without opening four chats. The signal already exists and is already summarised —
    `ToolUseNote`/`ToolSettled` carry a computed one-line description — but they are
    emitted only to *that session's* socket, so a client sees activity only for
    conversations it has opened. The change is one new bus event
    (`SessionActivityEvent(session_id, folder, tool, summary, phase, ok)`) published from
    the two call sites that already build those frames. **It is a firehose and must be
    treated as one**: batched server-side with the policy `services/terminal_stream.py`
    already proves (first frame after a quiet stream goes immediately, then at most one
    frame per window, coalescing per session so only the latest line survives), no result
    excerpts on the shared bus, and the same workspace jail — a fleet-wide feed discloses
    paths and commands from every session at once, which is wider than the per-session
    socket. Built on `SessionStatusEvent` + the new event only, so it neither waits for
    nor collides with the watcher-protocol rewrite in the Feel track. Exit criterion:
    four sessions working at once are legible from one pane, updating in place, with no
    measurable cost to the shared `/ws/events` socket under a Grep-heavy turn.
11. ~~**Native plan usage meters** (5-hour window, weekly per model)~~ **done** — the
    owner's ask to see in the app what Claude Code shows in the terminal, and the four
    constraints below are on screen rather than papered over. What landed:
    `services/usage.py` + `models/usage.py` + `GET /api/usage`, captured at the SDK
    seam (`AgentSession._handle_sdk_message` gained the `RateLimitEvent` branch it was
    silently discarding), published as `UsageEvent` on the existing `/ws/events` bus,
    and rendered by a registered tool (`ui/src/panels/UsagePanel.tsx` + `usage.ts`) —
    a status-bar reading plus an on-demand panel of meters, one module and one line in
    `tools.ts`. Nothing is persisted: live state about an *account* is not workspace
    data, so a restart honestly reads "not reported yet".
    **The plan's shape was wrong in one way and the code follows the source, not the
    plan** (decisions log): `RateLimitEvent` carries a **single** `RateLimitInfo` — one
    `rate_limit_type`, one `utilization` — so the windows arrive as *separate* events
    and the snapshot is accumulated one window at a time. A window nobody has
    transitioned in is **absent**, never zero.
    The four constraints, each with a rendering: (1) **no query API** — confirmed
    against the bundled CLI's own `--help` (no `usage` subcommand) — so there is no
    refresh button, because none could work; (2) emitted on *transition*, on a live
    session's stream, so every bucket carries `observed_at`, the snapshot carries a
    server-measured `age_s`, each meter is stamped with its own age, and past 15
    minutes the panel says the figures are old and why; (3) an account that never emits
    gets a designed empty state that degrades to `total_cost_usd` + `model_usage` from
    `ResultMessage`, labelled **"Session cost — not plan usage"** — and the status bar
    shows *nothing* rather than a zero; (4) exactly the SDK's own weekly types, no
    synthesized per-model breakdown, and a missing `utilization` renders as an em dash.
    Nothing leaves the machine; the zero-telemetry stance holds — including the log,
    which is the half that is easy to miss: the desktop shell copies the backend's
    stdout into `shell.log` and keeps it across restarts, so utilization and reset
    times are `debug`-only and the regression test asserts that at fd 1 rather than
    only asserting that no file lands in `.workbench/`.
    **Deferred, and why:** the between-turns read path. The SDK buffers messages in its
    own receive channel, so an event that fires between turns is delivered at the start
    of the next turn rather than lost — which is caveat 2 doing its job. Draining that
    channel from a background task would *steal* the next turn's messages, so a true
    between-turns reader means restructuring `AgentSession` around a single reader loop
    that dispatches to the current turn. That is its own PR, and until it lands the
    figures update when you next talk to an agent — which the UI says plainly.
12. **Session browser — every conversation, grouped by folder** (registers as a tool,
    plural: one browser can be scoped to a project while another watches everything). The
    Claude Code resume list, natively: folder groups → session rows (title, relative time,
    live/disk dot), searchable. Opening a row does **not** open a chat inside the browser
    — it opens an agent *instance* beside or in place of the focused pane, which is the
    composability principle paying rent: one browser drives ten agent panes. The read half
    exists (`services/session_index.py` reads `~/.claude/projects/<encoded-cwd>/*.jsonl`,
    derives titles, dedupes live against on-disk by SDK id, and `GET
    /api/agents/sessions` already returns `FolderSessions` groups) — this is a
    presentation gap, not a capability gap, so **the AgentPanel's folder list becomes this
    browser's compact form rather than a parallel implementation**. Two real risks:
    `list_sessions()` takes one folder at a time, so browsing means walking
    `projects_root` itself, and `encode_project_dir()` is **lossy and not reversible**
    (`C:\a\b` and `C:/a-b` collide) — so a display path is resolved by matching candidates
    against real directories and the raw encoded key is shown when it cannot be, never a
    guessed path; and a `projects` dir with hundreds of folders means hundreds of
    glob+stat+first-line reads per refresh, so it needs an mtime-gated cache and
    pagination or it becomes the thing that makes startup slow. **Split on an honest
    dependency**: half A — browse, search and resume anything readable, including projects
    outside this workspace — ships alone and is complete; half B — *opening* a session
    whose folder is outside the workspace jail — waits for item 5. Exit criterion: every
    project under `~/.claude/projects/` is listed with a resolved-or-honestly-encoded
    folder name, and a row opens as its own agent pane next to the focused one.
13. **Pop-out — panes on a second monitor** (was item 2's deferral note and M7's
    "unclaimed" line; the owner's "full screen sharing mode" and "no limits" claims it).
    dockview supports floating groups and popped-out windows; with panes in place this is
    an arrangement rather than a feature. It carries one engineering fact nobody had
    written down: **a popped-out panel is a separate WebView2 window with no HWND in the
    main window's parent chain**, so a native Office pane cannot simply pop out — it needs
    a second native host window or an explicit, reasoned refusal. Decide that before
    shipping, or the first user who pops out a docked Word gets an orphaned invisible
    window, which is exactly the class of bug the host ownership rules were written to
    prevent. Exit criterion: any pane pops out to a second monitor and restores to its
    group, and a native Office pane either follows or refuses with a reason on screen.

**Sequencing (2026-08-05), weighted toward what the owner can see.** Hours of invisible
infrastructure read as nothing produced, so the order below front-loads visible shape
change without cutting a gate. **0.** Land the two shape-changing lanes first:
`feel/motion-foundation` (small, complete, owns `dockview.css` + `Layouts.tsx`), then
**item 9 panes** rebased onto it — the single most visible change on this list, and it
costs zero new scope because it is already in flight. **1.** Product principle 4 and the
`CLAUDE.md` "Panes are instances" standard **landed with this plan revision**, ahead of
item 9's code, so the panes PR inherits them and must not re-touch those sections — five
lanes hold these files and a second edit is pure conflict. **2.** **Item 11 usage meters** — shortest path from
nothing to visible, and independent of panes, the watcher rewrite and Office, so it can
run in parallel with step 1. **3.** **Item 10 activity** — the first new panel that panes
makes worth having (you want to split it beside your editor and maximize it), and the feed
Mission Control should render. **4.** **Item 12 session browser, half A** — after item 10
because they share a row shape and building the live one first means the browser reuses it.
**5.** Moat track in parallel throughout, in its existing order, ending in the
side-by-side Office proof. **6.** **Item 13 pop-out**, after panes and after the Office
composition PR, which is what makes its native-window decision necessary. **7.** Then
the existing order, unchanged and better founded: item 4 → item 5 (which unlocks item
12's half B) → item 6's carried halves (its pool landed early and out of sequence,
because item 7 needs it) → item 7 → item 8. **8.** M6 and M7 untouched — and every surface
added above ships in current `DESIGN.md` tokens and gets restyled with everything else
in M7. No lane invents a look.

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
  dockview maximize moved to M5 item 2 and **landed** there; ~~floating and popped-out
  panels are still unclaimed — dockview supports both and nothing has asked yet~~ —
  claimed 2026-08-05, now M5 item 13.) The redesign now has substantially more structure
  to dress than when it was logged: panes, an activity feed, a usage meter and a session
  browser are exactly the surfaces that stop the app reading as a code editor — which is
  what the change request was actually about. Nothing above smuggles the redesign
  forward; it all ships in current tokens and gets restyled here.
- Voice input as an optional extra (local faster-whisper, push-to-talk, domain
  vocabulary initial prompt).
- Remaining OSS product bar: first-run experience (workspace picker, Claude-login and
  Office/OnlyOffice detection walkthroughs), cross-platform PTY + 3-OS CI matrix,
  CONTRIBUTING/templates, versioned Tauri releases with signed installers,
  zero-telemetry README stance, and the real product name.
- Exit criterion: a stranger on a fresh Windows machine reaches a working, secured,
  distinctive product in under ten minutes.

## Decisions log

- 2026-08-05 — **Two more `kunchenguid` repos read against the vetting bar; both
  learn-and-build, neither adopted** (owner asked; the third, `lavish-axi`, was
  rejected outright earlier today — see its entry below).
  **`axi`** (MIT) is design standards plus reference CLIs. No disqualifying finding:
  no telemetry, no global config writes, and its skill install is optional and unneeded
  because principles can simply be applied. We already enforce its central idea further
  than it states it — `agent_tools.py` fails the build on an unmeasured tool, where the
  standard only prescribes — and we found the sharper form ourselves (a *schema* is
  paid on every request whether or not the tool is called). Three of its principles are
  real gaps and are now standing rules in `CLAUDE.md`: truncate with a stated size and
  an escape hatch, say "none" explicitly, end with the obvious next step. Its TOON
  output format is rejected with a number, not a shrug: the ~40% claim is on large list
  payloads, ours are hundreds of bytes, and compact JSON already measured ~16% under
  pretty-printed. These three bind hardest on the COM bridge's `office_read` —
  a spreadsheet range is exactly the payload that needs "50 of 2,000 rows, ask like
  this for more".
  **`treehouse`** (MIT) is a pooled git-worktree manager — the same thing as M5 item 6.
  Not adopted as a dependency (a pool bound to agent sessions and inside our path jail
  has to be ours), but four of its design decisions are better than what item 6
  specified and are folded in there: detached HEAD, pool-never-destroy so dependency
  and build caches survive reuse, two idle signals (owner exit *and* a durable lease),
  and fail-safe recovery that marks slots leased until verified. The second of those
  also settles a mistake made earlier today: PR #32 tried to kill the cold-install cost
  by junctioning `node_modules` and had to be closed because `git worktree remove`
  recurses through a Windows junction and would have emptied the main checkout. Not
  removing the worktree removes the hazard and the cost together.
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
- 2026-08-05 — **Instances are first-class surfaces** (owner change request below;
  product principle 4). The window is not a set of panels with a layout on top; it is a
  set of *instances* of registered tools. A pane is `(toolId, instanceId)` where the
  instance id **is** the dockview panel id, and the tool id stays the stable contract it
  already was, because dockview resolves a panel through `contentComponent` — so the
  registry is re-keyed in exactly one place (`placementOf()` setting `id: tool.id`) and
  nowhere else. **State lives in three tiers, and the tier rule is the whole design**:
  (1) pane-local view state (scroll, focus, xterm viewport) lives in the component,
  keyed by instance, and dies with the pane — never persisted; (2) *instance params*, the
  small serializable "what am I pointed at", ride dockview's own `params`, which
  `toJSON()`/`fromJSON()` already serialize — so they inherit the existing layout
  debounce with **zero new plumbing**, and are capped as a flat record of primitives
  under a stated byte budget because they are written on every drag; (3) the backing
  resource — PTY, SDK session, Office HWND, Monaco model — is owned by the server or by
  one module-level registry keyed by its own id, and `params` merely *names* it. Tier 3
  is what makes "ten agents in a grid", "close the pane and keep the agent working" and
  "hibernate an off-screen pane" expressible instead of individually built. **The
  load-bearing discovery that made this cheap**: `.workbench/layouts.json` already stores
  `panels: { <panelId>: { contentComponent, params } }` and `pruneLayout()` already vets
  by `contentComponent` while treating the panel key as opaque — so the persisted format
  needs no change and every saved layout keeps working; only the presets, which name tool
  ids as panel ids, must change. Rejected alternatives: a second registry for "multi
  panels" (two authorities over one window); a per-tool ad-hoc tab strip, which is what
  Terminal and EditorArea do today and is exactly why a terminal cannot be split beside a
  code pane; and unbounded plurality with no caps, hibernation or reaping, which measures
  out at 1.5–5 GB for ten talking agents and would ship a memory leak with a UI.
  **Enforcement, because a standard that cannot fail does not bind**: `registry.test.ts`
  asserts no derivation keys on a tool id where an instance id belongs; every plural tool
  ships a test opening two instances and asserting independence through a save/restore
  round trip; the perf lane gains an instance-count budget gating the grid layouts; and
  an unscoped `page.locator` on a pane-internal class fails review. "It works with one" is
  not evidence.

- 2026-08-05 — **Plan usage meters have an honest source — with four caveats we show
  rather than hide.** Investigated before promising the feature, because the alternative
  was inventing numbers. The installed `claude_agent_sdk` (0.2.129) defines
  `RateLimitEvent`/`RateLimitInfo` in `types.py` and parses it in
  `_internal/message_parser.py`, carrying `rate_limit_type` (`five_hour`, `seven_day`,
  `seven_day_opus`, `seven_day_sonnet`, `overage`), `utilization`, `resets_at` and
  `status` — the 5-hour and weekly windows are named by the SDK itself, so M5 item 11 is a
  read, not an integration. What does **not** exist: any query API. `claude --help` lists
  no `usage` subcommand, `/usage` is an interactive-TUI slash command only, and
  `~/.claude.json` caches no utilization. Consequences encoded in the item: the event
  fires on *transition* and only on a live session's stream, so meters are last-known and
  labelled "as of"; a cold app reads "not reported yet", never "0%"; Workbench discards
  the event today (`_handle_sdk_message()` dispatches on four type names with no `else`)
  and reads messages only inside a turn, so a between-turns path is part of the work; and
  weekly granularity is the SDK's three types, with **no synthesized per-model
  breakdown**. If it turns out an account never emits, the surface degrades to per-turn
  `total_cost_usd`/`model_usage` labelled as session cost — and says so. A meter that
  guesses is worse than no meter.

- 2026-08-05 — **Plan usage: the SDK gives one window per event, and that shapes the
  whole surface** (M5 item 9). The research pass described a usage event "carrying
  five_hour, seven_day, per-model seven_day figures and any overage state". Read against
  the installed SDK (claude-agent-sdk 0.2.129, bundled CLI 2.1.221), that is not the
  shape: `RateLimitEvent` carries a **single** `RateLimitInfo` with one
  `rate_limit_type` and one `utilization`, plus the `overage_*` fields riding along;
  each window transitions — and is reported — on its own. So the snapshot is
  *accumulated*, one window at a time, a window that has not transitioned is **absent**
  rather than zero, and the per-model weeklies appear only if they arrive. Confirmed
  with the rest of the research: there is no `usage` subcommand on the bundled CLI,
  `/usage` is TUI-only, and nothing in the SDK reads current utilization — the
  transition event is the entire supply.

  **Consequence, taken as a product decision rather than a limitation to hide:** the
  four caveats are rendered, not documented. Every figure is stamped with its own age
  (server-measured, plus local elapsed — never a subtraction between two clocks); there
  is no refresh button, because there is nothing to ask; the never-emitted account gets
  a designed empty state that degrades to *session cost* under its own heading; and a
  missing `utilization` renders as an em dash, because "not reported" and "0% used" are
  different facts. The only judgement on our side is where a bar starts looking
  alarming (75% / 90%), and it defers to the SDK's own `allowed_warning`/`rejected`
  first. Nothing is persisted: live state about an account is not workspace data.

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

- 2026-08-05 — **"No limits to the imagination"** (user), in their own words because the
  plan has to answer them rather than approximate them: *"All of this and also having the
  possibility to have a tab where I work on Word and Excel side by side in full screen
  sharing mode - not just the small code editor. Think that I want to have no limits to
  the imagination of what I need when working. If I want only code over my full screen I
  should have that. If I want a code where I can live see everywhere Claude is editing, I
  need to be able to have that. If I want to have a 'browser' with 10 Claude agents, I
  need to be able to have that. For Claude I also need to have an overview like we do in
  Claude Code with which chats belong to which folder. You see the big picture, it should
  be so much more modular than what it is. It is also good you say many functionalities
  are waiting, but I am afraid we are not thinking grand enough and not modular enough."*
  On the same theme, earlier: *"I want something super modular like TMUX"* and *"it still
  feels like a cheap VS Code editor. This is not what I wanted."* Also asked: the Claude
  plan's usage meters (5-hour, weekly per model) shown natively, the way Claude Code
  shows them. → **Product principle 4** (composable surfaces) as the general answer, and
  five specific ones: panes (M5 item 9, in flight), live agent activity (item 10), usage
  meters (item 11), session browser by folder (item 12), pop-out to a second monitor
  (item 13), and Office as ordinary composable panes — Word beside Excel, full screen —
  folded into the M4 Office sequence as its acceptance demo. Read honestly, only two of
  those are new *capabilities*; the rest were planned but stated one arrangement at a
  time, which is the actual complaint. What is deliberately **not** promised in response:
  a per-model weekly breakdown finer than the SDK reports, an unbounded fleet with no
  caps or reaping, native Office in a popped-out window before the parent-chain question
  is decided, and any part of the visual redesign moving out of M7.
