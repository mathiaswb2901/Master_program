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
  demoted to preview/diff/fallback. Requires the **Tauri shell** (a browser tab cannot
  host native windows) — pulled forward from packaging into M4 core.
- **Visual plan artifacts** as a typed product primitive: `present_plan` MCP tool →
  Pydantic `PlanArtifact` → native clickable plan cards in chat (options, steps, file
  refs); decisions return to the agent as typed JSON; pending-plan replay on reconnect
  (fix the identical PermissionRequest replay gap while there).
- **Flow layer**: typed command registry replacing the 3-item QuickBar (panel focus
  Ctrl+1..4, tab cycle/close, Alt+1..9 session jump), `SessionStatusEvent` fan-out on
  `/ws/events`, status bar with live session chips + `document.title` attention badge,
  toast layer for currently-silent failures, chat markdown/code rendering with per-tool
  settle + expand, terminal tabs (kill the single-instance remount), file-tree CRUD
  wiring the endpoints that already exist, dirty-close confirmation + beforeunload guard,
  real session titles + live/disk dedupe. *Known gap*: the beforeunload guard and the
  `document.title` attention badge are browser-only — WebView2 honors neither on native
  window close/title, so the Tauri shell task above must re-wire both natively
  (`onCloseRequested` → dirty-close modal; window `setTitle` for the badge).
- **shortcuts.md**: workspace `.workbench/shortcuts.md` + global file, merged, watched
  live; entries drive QuickBar commands, terminal snippets, chat prompt templates, and
  custom keybindings; agents get a skill to add entries on request.
- **Tool registry**: panels/commands/skills register in one place instead of hardwiring
  in `App.tsx` (product principle 1).
- Committed carryover: pptx E2E fidelity pass, bundled skills (Anthropic
  xlsx/docx/pptx; OfficeCLI after vetting; plan-visual, validate, remember,
  loop-objective, workbench-dev), provenance badges.
- UI quality tooling starts here: eslint, vitest, Playwright E2E (standing bar).

### M5 — Parallel (worktrees + Mission Control)

- Managed worktree pool: backend `WorktreeService` (acquire/release/reap, dirty-slot
  `needs_review` protection, per-slot watchers), multi-root file/terminal access through
  a root registry (path jail preserved per root), worktree-bound agent sessions.
- Mission Control board: all sessions as cards (status, current activity, cost), inline
  permission chips answerable from the board; orchestrator session kind with a
  mission-control MCP toolset (spawn/list/read/send/wait/stop workers), worker budget +
  cost ceiling, escalate-to-board permission policy (never auto-allow shell).
- **Security hardening pulled forward** (OSS bar item 1): per-launch auth token injected
  into the UI + strict WS Origin checks — agent-spawned workers and multi-root access
  widen the unauthenticated localhost surface unacceptably.

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
