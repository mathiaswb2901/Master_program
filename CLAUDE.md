# Workbench — agent conventions

One-window analyst workbench: FastAPI backend (`server/`), React+Vite UI (`ui/`), Tauri shell (`desktop/`).
Full plan and status: `ROADMAP.md`. Design system: `DESIGN.md` (binding for all UI work).

## Commands

- `uv sync --dev` — install/refresh env (uv manages `.venv`)
- `uv run pytest` — tests; `uv run mypy` — types; `uv run ruff check . && uv run ruff format .` — lint/format
- `uv run workbench-server` — run backend (port 8787)
- UI: `cd ui && npm run dev` (Vite, port 5173)

## Standards (non-negotiable)

- Backend is "no shortcuts": every REST/WS payload is a Pydantic model in `server/src/workbench_server/models/`;
  `mypy --strict` and ruff must pass; new behavior ships with tests (unit + integration; Playwright E2E per milestone).
- Routers stay thin; logic lives in `services/`. structlog only — never `print`.
- Disk is the single source of truth for files; all change notifications flow through the watcher bus.
- UI: follow `DESIGN.md` tokens; zustand is the only state store; no new dependencies without justification.
- Windows-first: paths via `pathlib`, PTYs via pywinpty, test on PowerShell.

## Danger zones

- Never write secrets or personal paths into tracked files (OSS repo).
- Office file operations must preserve fidelity — when in doubt, keep a `.bak` and verify round-trip.
