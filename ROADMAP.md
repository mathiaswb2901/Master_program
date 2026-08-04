# Roadmap

The living plan. Milestone status is updated as work lands; user change requests get
logged under **Change requests** and pulled into milestones.

## Milestones

| # | Scope | Status |
|---|-------|--------|
| M0 | Foundations: typed FastAPI core, uv, ruff/mypy-strict/pytest, pre-commit, CI, design system (`DESIGN.md`, tokens) | **done** |
| M1 | IDE-lite shell: pywinpty terminals, jail-safe files API, Monaco tabs, watcher/sync engine, multi-session agent core (per-folder chat grouping, context bridge), QuickBar, Tauri dev window | **in progress** |
| M2 | Word: OnlyOffice native install, signed editor config + save callback, Doc panel, reopen-on-agent-edit, mammoth.js degraded mode | pending |
| M3 | Excel: Sheet panel (same OnlyOffice pattern), `.bak` safety, agent-side openpyxl analysis | pending |
| M4 | PowerPoint; bundled skills (plan-visual, validate, remember, loop-objective, workbench-dev, Anthropic xlsx/docx/pptx, OfficeCLI after vetting); provenance badges; Tauri sidecar packaging + release CI | pending |

## Decisions log

- 2026-08-04 — Prior-art check (GitHub + products): no existing tool combines IDE + full-fidelity
  Office editing + multi-session Claude agents. Closest: AionUi/OfficeCLI (agent-mediated docs, no
  direct editing), Nimbalyst (sessions + worktrees, no Office). Verdict: build our own; adopt
  OfficeCLI as an agent-side skill after vetting.
- 2026-08-04 — OnlyOffice Docs Community via **native Windows installer** (port 8880); no Docker
  anywhere. Univer rejected for v1 (xlsx import/export is Pro-only).
- 2026-08-04 — Auth: machine's existing Claude Code subscription login; `ANTHROPIC_API_KEY` as
  documented alternative.

## Open-source product bar (standing directive, 2026-08-04)

Build for real external users, not just the author. Consequences, tracked as work:

1. **Local-API security hardening** (before first public release): the backend listens on
   127.0.0.1, but any local process or malicious web page can reach localhost — add a
   per-launch auth token required on every REST/WS call (injected into the served UI),
   plus strict Origin checks on WebSockets (DNS-rebinding defense).
2. **Cross-platform**: PTY layer is behind `PtyManager` — add a ptyprocess-based POSIX
   implementation and a CI matrix (windows/ubuntu/macos) when M1 stabilizes. No
   Windows-only assumptions outside `pty_manager.py`.
3. **First-run experience**: workspace picker on first launch, graceful "Claude login not
   found" guidance (`claude setup-token` / API key), OnlyOffice detected-or-degraded.
4. **Contributor experience**: CONTRIBUTING.md, ARCHITECTURE.md, issue/PR templates,
   good-first-issue labels; keep module boundaries documented.
5. **Release engineering**: versioned GitHub releases with Tauri installers, changelog,
   signed artifacts if feasible.
6. **Privacy stance**: zero telemetry, stated in README. Files never leave the machine
   except through the user's own Anthropic account.
7. **Naming**: "workbench" is a placeholder — pick a unique, searchable name (check
   GitHub/PyPI/npm availability) before publishing.

## Change requests

- 2026-08-04 — **Frontend is too plain** (user): current UI reads as a generic VS Code
  clone; the design pass must go beyond token compliance. Redo with the ui-ux-pro-max
  skill used aggressively (style databases, component patterns, motion presets) to give
  the app a distinctive visual identity. Priority: after M2/M3 office integration.
