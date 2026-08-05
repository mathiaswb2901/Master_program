# Contributing

Thanks for your interest! Workbench is early; the fastest way to help is to pick an
issue labeled `good first issue`, or open an issue describing what you want to change
before writing code.

## Setup

Prereqs: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22+. Windows is the
primary target today; macOS/Linux support is tracked in ROADMAP.md.

```bash
uv sync --dev
cd ui && npm install && cd ..
cd desktop && npm install && cd ..   # only for the native shell; `npm run tauri dev`
                                     # fails with "tauri is not recognized" without it
uv run workbench-server        # backend on :8787
cd ui && npm run dev           # UI on :5173 (proxies to backend)
```

Re-run `npm install` in a package after pulling a branch that adds a dependency there.
A stale `ui/node_modules` does not report itself as stale — it surfaces as a Vite
`Failed to resolve import ...` error that reads like a broken import path.

Working in a `git worktree`? Run `python scripts/dev/warm_worktree.py` inside the new
worktree instead of installing from scratch: when its lockfiles are byte-identical to
the main checkout's it links `node_modules` across (a Windows directory junction — no
admin rights needed) and syncs Python. It records what it linked in
`.warm-worktree.json`; the module docstring has the full rationale.

The linked tree is **shared with the main checkout, not copied**. Reading it is safe;
writing to it is not, and neither is deleting the worktree — `git worktree remove`
recurses through the junction and takes the main checkout's `node_modules` with it. So
before `npm install` **and** before removing the worktree:

```bash
python scripts/dev/warm_worktree.py --unlink
```

Optional: agent features need a Claude subscription login (`claude login` /
`claude setup-token`) or `ANTHROPIC_API_KEY`. Office editing needs a local
[OnlyOffice Docs](https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-windows.aspx) install.

## Quality bar (CI enforces all of it)

- `uv run ruff check . && uv run ruff format --check .`
- `uv run mypy` — strict, no `Any` smuggling
- `uv run pytest` — new behavior ships with tests; cross-module behavior gets a
  full-pipeline test (see `server/tests/test_watcher.py` for the pattern)
- UI: `npm run lint`, `npm run test`, `npm run build` clean; follow `DESIGN.md` —
  colors/spacing only via `tokens.css` variables
- `cd ui && npm run e2e` — Playwright drives the built UI against a real server in a
  throwaway workspace (agent journeys use `WORKBENCH_FAKE_AGENT=1`, so no Claude login
  is needed). First run: `npx playwright install chromium`. To keep the workspace a run
  happens in, point `WB_E2E_WORKSPACE` at an empty or nonexistent directory — the suite
  seeds it, and refuses to reuse one that already has files in it
- Read `ARCHITECTURE.md` before moving module boundaries

## Conventions

- Every REST/WS payload is a Pydantic model (`server/src/workbench_server/models/`)
  mirrored in `ui/src/types.ts`. Change both or neither.
- Routers stay thin; logic lives in `services/`.
- structlog only; never `print`.
- No new runtime dependencies without a clear justification in the PR description.
- Windows/macOS/Linux differences belong behind a `Protocol` in `services/`.

## Commit / PR

- Small, focused PRs review faster.
- Describe the user-visible behavior change and how you verified it (test name or
  manual steps + screenshot for UI).
- CI must be green.
