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
uv run workbench-server        # backend on :8787
cd ui && npm run dev           # UI on :5173 (proxies to backend)
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
  is needed). First run: `npx playwright install chromium`
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
