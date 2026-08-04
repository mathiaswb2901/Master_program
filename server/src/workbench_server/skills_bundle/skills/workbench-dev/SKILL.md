---
name: workbench-dev
description: Contributor standards for developing Workbench itself — the FastAPI server/, React ui/, Tauri desktop/ repo. Use when writing, reviewing or reasoning about code in that repository (routers, services, models, panels, stores), before opening a PR against it, or when unsure of its conventions. Not for other codebases.
---

# Working on Workbench

Applies when the workspace *is* the Workbench repo (`server/`, `ui/`, `desktop/`,
`ROADMAP.md`, `DESIGN.md`). Read `CLAUDE.md` in the repo root — it is the
authority; this skill is the short form. `ARCHITECTURE.md` explains the layout,
`ROADMAP.md` the plan and the decisions log.

## Commands

- `uv sync --dev` — environment; `uv run workbench-server` — backend on 8787
  (workspace = the CWD it starts from)
- `uv run pytest` · `uv run mypy` · `uv run ruff check .` · `uv run ruff format .`
- UI: `cd ui && npm run dev` (Vite, 5173)

All four gates must be green before you push. `mypy` is `--strict`.

## Backend rules

- **Every REST/WS payload is a Pydantic model** in
  `server/src/workbench_server/models/`, mirrored in `ui/src/types.ts`. No
  ad-hoc dicts on the wire, ever. WebSocket protocols are discriminated unions
  on `type`.
- **Routers stay thin**: validate, delegate, map domain errors to HTTP codes.
  The logic lives in `services/` and must be testable without HTTP.
- **structlog only** — `log.info("event.name", key=value)`. Never `print`.
- **Disk is the single source of truth.** File changes reach clients through the
  watcher bus, not by return values. Writes are atomic and jailed to the
  workspace root.
- **`pathlib` everywhere**, Windows-first. Platform code stays quarantined
  behind a `Protocol` (see `services/pty_manager.py`).
- The Agent SDK is injected: `services/sdk_factory.py` is the *only* module that
  imports `claude_agent_sdk`. Tests script a fake client through the same seam.
- **No new dependencies** without justification, and never bump versions as a
  side effect.

## Frontend rules

- `DESIGN.md` is binding. Use its tokens — no raw hex, no one-off spacing.
- zustand is the only state store. dockview owns the layout.
- Types in `ui/src/types.ts` mirror the Pydantic models exactly.

## Tests ship with behavior

New behavior lands with tests, and a milestone lands with Playwright E2E.
Layers: unit (jail, hashing, protocol parsing) → integration (real app
in-process: API write → watcher → WS event) → live smoke
(`WORKBENCH_LIVE_AGENT=1`) → E2E.

**Bug fixes open with a reproduction**, end to end, as close to how a user hits
it as possible. A failing unit test is a guess about where the bug lives, not a
repro. The E2E repro becomes the regression test.

## Agent-facing tools carry a budget

Every tool description is loaded into every session's context, so it is a cost
paid on every request. Prefer a thin call over a wrapped API; return compact
text over pretty-printed JSON; keep descriptions short. Each tool's own tests
assert a ceiling on its description length and on the serialized size of a
representative result — a budget that lives outside the quality gate does not
bind.

Skills entering the bundle are **vetted, not adopted on popularity**: read every
line (a skill can run anything on the user's machine) and keep it only if it
measurably helps.

## Workflow — PR only

Direct pushes to `master` are rejected by a branch ruleset.

1. Feature branch off an up-to-date `master`.
2. Commit; `gh pr create`.
3. Quality gate green (it requires the branch to be up to date — rebase when
   master moves).
4. `gh pr merge --squash --auto`.

Subagents that write to this repo run in an **isolated git worktree**, never in
the checkout the main session uses. One writer per checkout, always. Use
`--amend`/`reset`/`rebase` only after `git log -1` shows the expected HEAD, and
never put `git commit` at the tail of a long `&&` chain.

## Danger zones

- **Never** write secrets or personal paths into tracked files — this is an OSS
  repo. That includes the OnlyOffice JWT secret and absolute user paths.
- Office file operations must preserve fidelity. When in doubt keep a `.bak` and
  verify the round-trip.
- Do not weight development cost heavily when choosing an approach ("think big",
  standing directive). Spike a capability before ruling it out.
