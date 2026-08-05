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
  isolation is proven. Still open here: the Rust window hosting behind that Protocol,
  the COM bridge (agents reading/writing the live document), the **host panel** — which
  waits for the tool registry so it registers itself instead of editing `App.tsx` — and
  packaging: the shell runs from source (`cd desktop && npm run tauri dev`); a bundled
  installer that carries its own Python needs `tauri build` work not done yet.
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
  failure). Eight journeys: files (create → Monaco → Ctrl+S → watcher reload → conflict
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

## Two tracks from here (2026-08-05)

M4's Office work and the modularity work below are **parallel tracks, not a sequence**.
They touch different parts of the codebase (Office: Rust host + Python COM bridge;
Modular: the UI shell and registry), so both run at once. The milestone table stays as
the record of scope; the tracks are how it gets built.

- **Moat track** — the Office host sequence (M4): ~~domain layer with a fake backend~~
  (**landed**) → Rust window hosting → Word docked → COM bridge + agent tools → Excel.
  What no competitor can copy quickly.
- **Modular track** — M5 below, reordered so the *seam* comes first. What the product
  feels like every day.

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
   capability any more; `Ctrl+1..4` is derived from the panels in the default layout,
   in registry order, rather than from four fixed ids. **Exit criterion demonstrated**,
   not claimed: the Scratchpad tool (`ui/src/panels/Scratchpad.tsx`) is a panel, a
   command, a chord, a tab icon and a file on disk, added in one new module plus one
   line in `tools.ts` — asserted end-to-end in the QuickBar journey. Server side,
   `services/agent_tools.py` is the matching registry the SDK reads (name, description,
   input schema, required `output_format`), and `test_agent_tools.py` binds the
   ergonomics budget: a ceiling on every description and on the serialized size of a
   representative result, plus compact JSON instead of the pretty-printed
   `get_workspace_state` payload we were paying for on every call. Deliberately
   deferred: registration is static — no dynamic plugin loader — but every derivation
   takes a tools array rather than reading `TOOLS`, which is the seam one plugs into
   (`ARCHITECTURE.md` §Tool registry, `docs/tools.md`).
2. **Layout system** — the "work full screen" gap. dockview already supports far more
   than we use: panel **maximize / focus mode**, floating and popped-out panels, and
   full serialization. Add named, savable layouts ("review", "writing", "three
   agents") switchable from the QuickBar and from `shortcuts.md`, plus **layout
   persistence across restarts** — today every reload throws your arrangement away,
   which is the single most anti-premium behaviour left in the app.
3. **Deeper shortcuts** — `shortcuts.md` grows beyond snippets and prompts: bind a
   layout, a registered tool, a workspace jump, or a saved agent objective to a chord.
   The file becomes the user's own control surface over everything the registry knows.
4. **Workspace switcher** — the workspace is currently whatever directory the server
   was launched from. Switch projects from inside the app (recent list, QuickBar,
   `shortcuts.md`), with per-workspace layout and session history. Supersedes the
   first-run picker in the OSS bar item 3.
5. **Managed worktree pool**: backend `WorktreeService` (acquire/release/reap,
   dirty-slot `needs_review` protection, per-slot watchers), multi-root file/terminal
   access through a root registry (path jail preserved per root), worktree-bound agent
   sessions. Parallel projects that cannot step on each other.
6. **Mission Control board** (registers as a tool, per item 1): all sessions as cards
   (status, current activity, cost), inline permission chips answerable from the board;
   orchestrator session kind with a mission-control MCP toolset
   (spawn/list/read/send/wait/stop workers), worker budget + cost ceiling,
   escalate-to-board permission policy (never auto-allow shell).
7. **Security hardening pulled forward** (OSS bar item 1): per-launch auth token
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
  distinctive welcome surface, branded empty states, micro-interactions, layout
  persistence, dockview floating/maximize, Monaco enrichment, content search
  (Ctrl+Shift+F), settings UI.
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

- 2026-08-05 — **Modularity is the plan, not a polish item** (user): after using the
  app, the gaps named as "we don't have it" — fixed layout with no full-screen/focus
  mode, no layout persistence, panels hardcoded in `App.tsx`, no workspace switching —
  are the difference between an app and an instrument, and belong in the plan rather
  than a later design pass. → M5 reordered as the **Modular track**, run in parallel
  with the Office moat track, registry first.
