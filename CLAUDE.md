# Workbench — agent conventions

One-window analyst workbench: FastAPI backend (`server/`), React+Vite UI (`ui/`), Tauri shell (`desktop/`).
Full plan and status: `ROADMAP.md`. Design system: `DESIGN.md` (binding for all UI work).

## Commands

- `uv sync --dev` — install/refresh env (uv manages `.venv`)
- `uv run pytest` — tests; `uv run mypy` — types; `uv run ruff check . && uv run ruff format .` — lint/format
- `uv run workbench-server` — run backend (port 8787)
- UI: `cd ui && npm install` once, then `npm run dev` (Vite, port 5173);
  `npm run lint` — eslint; `npm run test` — vitest; `npm run build` — type-check + bundle
- `cd ui && npm run e2e` — Playwright: builds the UI, then drives it against a real
  server in a temp workspace with `WORKBENCH_FAKE_AGENT=1` (`ui/e2e/`, chromium only)
- `cd ui && npm run perf` — the Feel perf lane: same production build against a
  **generated 5,005-file workspace** (`ui/playwright.perf.config.ts`, `ui/e2e/perf/`).
  A bare run builds its fixture in a temp directory and removes it (with the
  `-projects` sibling) on the way out; `WB_PERF_WORKSPACE=<dir>` reuses a fixture
  instead of rebuilding it, and a directory you named is never one the lane deletes.
  **Pin it when comparing before/after** — a fresh 5,105-file fixture per run swamps
  a 100 ms difference in disk and AV noise. The server-side budgets are plain pytest
  (`server/tests/test_perf_budgets.py`); the bundle budget reads the build's own
  module attribution (`ui/e2e/perf/bundle.spec.ts` + `dist/bundle-metafile.json`)
- Desktop shell: `cd desktop && npm install && npm --prefix ../ui install` once
  (both: the shell's `beforeDevCommand` starts Vite from `ui/`), then
  `npm run tauri dev` — native window; starts Vite itself and either attaches to
  a backend already on 8787 or spawns one.
  Gates: `cd desktop/src-tauri && cargo fmt --check && cargo build && cargo test`.
  `cargo test` includes the Office-host window tests: they create real windows and
  start the synthetic guest process, so they need a desktop session (any normal
  Windows login). The two that additionally need the *foreground* — the real-click
  and hang-isolation measurements — are `#[ignore]`d; run them with
  `cargo test -- --ignored --nocapture`. `WORKBENCH_HOST_DEMO=1 cargo run` docks a
  synthetic guest in the running shell, and `=hang` wedges it a few seconds later.

## Think big (standing directive)

Model estimates of development cost are trained on human developer speed and run far
too high — they bias you toward cheap, unambitious designs. Do not weight development
cost heavily when choosing an approach; pick the ambitious, correct, premium option.
Never silently shrink scope because something "would take weeks" — it usually won't.
When a capability seems out of reach (e.g. embedding real Office), spike it before
ruling it out.

## Standards (non-negotiable)

- Backend is "no shortcuts": every REST/WS payload is a Pydantic model in `server/src/workbench_server/models/`;
  `mypy --strict` and ruff must pass; new behavior ships with tests (unit + integration; Playwright E2E per milestone).
- Routers stay thin; logic lives in `services/`. structlog only — never `print`.
- Disk is the single source of truth for files; all change notifications flow through the watcher bus.
- UI: follow `DESIGN.md` tokens; no new dependencies without justification.
- **Nothing that is not needed to paint is statically imported from `main.tsx`.** A
  module script blocks `DOMContentLoaded` until it is downloaded, parsed *and*
  evaluated, so anything reachable from the entry is paid on every cold start whether
  the feature is used or not — that is how the editor came to be 88% of the launch
  bundle. Heavy, on-demand capabilities load behind a dynamic `import()` and are
  warmed on idle once the launch is over (`ARCHITECTURE.md`, "The launch path"). The
  budget that enforces it is `ui/e2e/perf/bundle.spec.ts`, which asserts what is
  *inside* the entry chunk, not only what it weighs.
- **zustand is the only state library, and `ui/src/store.ts` is the default home for
  state.** A capability may own a second `create()` instance *in its own module* on one
  condition: nothing outside that module reads it. That is not a loophole — it is the
  same rule as the one above. State only one tool uses, living in a shared file, is
  exactly the coupling that makes parallel lanes collide, and `store.ts` naming a
  capability is `App.tsx` naming a capability by another route. State two tools share is
  app-wide by definition and belongs in `store.ts`; a tool that starts sharing moves
  there. Never a second state *library*, and never a store outside the module that owns
  it. The layout system (`ui/src/panels/Layouts.tsx`) is the first of these.
- A new capability **registers itself** — a `WorkbenchTool` descriptor in its own module
  (panel, commands, default chords, status items) plus one line in `ui/src/tools.ts`.
  Never add a panel or a panel-specific command by editing `App.tsx`, `commands.ts` or
  `StatusBar.tsx`: those files name no capability, and keeping it that way is what lets
  parallel lanes land panels without colliding. A tool takes an `Alt` chord only if the
  command earns it — registered chords beat `shortcuts.md`, which may bind nothing else.
  A tool's `id` is a **stable contract**: saved layouts (`.workbench/layouts.json`)
  reference panels by it, so renaming one renames the user's saved arrangement too.
  See `docs/tools.md`.
- **Panes are instances, not panels.** A pane is a `(toolId, instanceId)` pair — the
  instance id is the dockview panel id, and "what am I pointed at" is a small
  serializable `params` record (`{sessionId}`, `{ptyId}`, `{path}`) carried in the saved
  layout. A tool declares whether it is singular or plural, and **plural is the default
  for anything a user could plausibly want twice**; twice is the baseline, not a feature
  request. Three consequences you apply without asking. (1) *Nothing assumes it is the
  only one of itself* — not in its component, not in `store.ts`, not in a CSS selector,
  not in a test. An `activeX: X | null` field in the app store is the shape of a
  singleton assumption: it needs a comment saying why the window really has only one, or
  it is a bug. (2) *A pane is a view onto a resource it does not own.* The PTY, the SDK
  session, the Office window and the Monaco model live server-side or in one
  module-level registry keyed by their own id (`terminalInput.ts` is the shipped
  pattern); `params` names one. Closing a pane closes a view — whether the resource dies
  with it is the tool's explicit decision, written in its descriptor, never an accident
  of unmount. (3) *A restored pane is vetted before it is believed.* Layouts persist,
  resources do not: every plural tool implements `adopt(params)`, and a pane whose
  resource is gone renders a named tombstone with the one action that recovers it
  (Reconnect, Resume, Reopen, Open in Word). A cap that is hit shows the cap and the
  setting that raises it, never a dead button. Enforcement: every plural tool ships a
  test that opens two instances and asserts they are independent through a save/restore
  round trip, and an unscoped `page.locator` on a pane-internal class fails review.
  "It works with one" is not evidence. (ROADMAP product principle 4.)
- Windows-first: paths via `pathlib`, PTYs via pywinpty, test on PowerShell.
- The shell (`desktop/src-tauri/`) owns only what a browser tab cannot do: the
  native window, backend supervision, close guard, window title. Anything the UI
  can do in a browser stays in `ui/`, behind `ui/src/shell.ts` — the app must run
  unchanged in both hosts, and every shell call is a no-op in a browser tab.
- Bug fixes start by reproducing the bug end-to-end, as close to how a user hits it as
  possible — then the fix. A unit test that fails is not a reproduction; it is a guess
  about where the bug lives. The E2E repro becomes the regression test.
- Agent-facing tools are judged on token cost, not just correctness: prefer a thin call
  over a wrapped API, return token-efficient output (compact text beats pretty-printed
  JSON), and keep descriptions short — every tool description is loaded into every
  session's context, so it is a cost you pay on every request. Enforce it where it can
  fail: each tool's own tests assert a ceiling on its description length and a ceiling
  that tool declares on the serialized size of a representative result, sized from the
  measured payload plus a margin you can state. One shared number big enough for the
  chattiest tool is a test no other tool can fail. No separate benchmark harness — a
  budget that lives outside the quality gate does not bind.
- Bundled skills live in `server/src/workbench_server/skills_bundle/` (one local plugin,
  shipped as package data); each session gets them via `--plugin-dir` as
  `workbench:<name>` — session-scoped, nothing is ever written to `~/.claude`.
  Sessions load workspace settings only (`project` + `local`);
  `WORKBENCH_INHERIT_USER_SETTINGS=1` restores the *whole* global `~/.claude` scope —
  hooks and permission rules included, not just skills — so treat it as a security knob.
- Skills entering the bundle are vetted, not adopted on popularity: read every line
  (a skill can run anything on the user's machine), and keep it only if it measurably
  helps. Widely-starred skills have been shown to raise token use *and* worsen results.

## Workflow (enforced by branch ruleset — direct pushes to master are rejected)

- Feature branch -> PR (`gh pr create`) -> quality-gate green -> `gh pr merge --squash --auto`.
  The gate requires the branch to be up to date with master; rebase when it moves.
- Subagents that write to this repo run in an ISOLATED git worktree — never in the
  checkout the main session uses. One writer per checkout, always.
- `--amend`/`reset`/`rebase` only after verifying `git log -1` shows the expected HEAD;
  never put `git commit` at the tail of a long `&&` chain.
- Run the app: `uv run workbench-server` (workspace = CWD it starts from).
  **Native Office hosting** (a real Word docked in a panel) needs the desktop
  shell — `cd desktop && npm run tauri dev` — and nothing else: `auto` is on, and
  `GET /api/office/capabilities` says why when it is not available.
  `WORKBENCH_OFFICE_NATIVE=off` turns it off; `WORKBENCH_OFFICE_FAKE=1` walks the
  whole lifecycle with no Office and no window (it is what the E2E suite drives).
  OnlyOffice stays the preview/diff/fallback path and needs env:
  `WORKBENCH_ONLYOFFICE_URL=http://localhost:8880` and
  `WORKBENCH_ONLYOFFICE_JWT_SECRET` = `services.CoAuthoring.secret.session.string` from
  `C:\Program Files\ONLYOFFICE\DocumentServer\config\local.json` (native local install,
  services `Ds*Svc`, port 8880).

## Danger zones

- Never write secrets or personal paths into tracked files (OSS repo). `.workbench/` is
  the user's own data — `shortcuts.md` (format: `docs/shortcuts.md`) is never committed.
- Office file operations must preserve fidelity — when in doubt, keep a `.bak` and verify round-trip.
