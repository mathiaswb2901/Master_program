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

## Change requests

_(none open — add new user instructions here with date + priority)_
