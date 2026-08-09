# Contributing

Thanks for your interest. Workbench is early and moving fast; the fastest way to help is
to open an issue describing what you want to change *before* writing code — the
[issue templates](.github/ISSUE_TEMPLATE) ask for the few facts that make a report
actionable on a project this platform-specific.

Read these before your first PR:

| File | What it decides |
|---|---|
| [`ROADMAP.md`](ROADMAP.md) | What is being built, in what order, and what is deliberately deferred |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Module boundaries, the launch path, the testing layers |
| [`DESIGN.md`](DESIGN.md) | The design system. Binding for all UI work — tokens, motion, contrast |
| [`CLAUDE.md`](CLAUDE.md) | The house rules, in full. This file is the contributor-facing summary of them |
| [`docs/tools.md`](docs/tools.md) | How a capability registers itself (panel, commands, chords, status items) |

## Prerequisites

- **Python 3.11+** and [uv](https://docs.astral.sh/uv/) — uv owns the `.venv`
- **Node 22** (what CI pins)
- **Windows 11** for the whole product. Native Office hosting (a real Word or Excel
  window docked in a panel) reparents an Office frame window into the desktop shell,
  which exists on no other OS, and terminals use ConPTY there. The **backend and the UI
  build and test on Windows, macOS and Linux** — see the CI table below.
- For the desktop shell: a Rust toolchain (`rustup`, MSVC target, 1.77.2+) and the
  Visual Studio Build Tools. WebView2 ships with Windows 11.
- Optional, for the OnlyOffice preview/diff/fallback path: a local
  [OnlyOffice Docs Community](https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-windows.aspx)
  install. Not needed for any gate.
- Optional, for talking to real agents: the machine's existing Claude Code login
  (`claude /login`) or an `ANTHROPIC_API_KEY`. **Not needed for any gate** — every agent
  test runs against the scripted fake agent (`WORKBENCH_FAKE_AGENT=1`).

## Setup

```bash
uv sync --dev                  # backend deps + the dev group (ruff, mypy, pytest)
npm --prefix ui ci             # UI deps (npm install also works locally)
```

## Running it

```bash
uv run workbench-server        # backend on 127.0.0.1:8787; the workspace is its CWD
cd ui && npm run dev           # …and in a second terminal: the UI on :5173
```

That is the browser-tab host. Everything works in it except native Office hosting and the
OS folder picker (a browser tab takes a typed path instead). The real thing is the native
window:

```bash
cd desktop && npm install      # the Tauri CLI
npm --prefix ../ui install     # the shell starts Vite from ui/ — skip this and the
npm run tauri dev              # window opens on a dev server that never started
```

The shell starts Vite itself and either **attaches** to a backend already listening on
8787 or **spawns** one and reaps it when the window closes.

Useful environment switches (all optional, all off by default unless noted):

| Variable | Effect |
|---|---|
| `WORKBENCH_WORKSPACE_ROOT` | The folder the server starts on (otherwise its CWD). The workspace is also switchable at runtime |
| `WORKBENCH_FAKE_AGENT=1` | Scripted agent replies, tool calls, permission prompts and plan cards — no Claude login, deterministic frames |
| `WORKBENCH_OFFICE_FAKE=1` | The whole Office-host lifecycle with no Office installed and no window |
| `WORKBENCH_OFFICE_NATIVE=off` | Turn native hosting off (`auto` is the default and resolves to *on* where it is actually possible) |
| `WORKBENCH_ENFORCE_AUTH=0` | Disable the per-launch token and the WebSocket Origin check. A debugging escape hatch — never a shipped configuration |
| `WORKBENCH_PORT` / `WORKBENCH_HOST` | Move the server, the shell and the dev proxy together |

## The gates

CI enforces every one of these. Run them before pushing; a piped command whose exit code
you did not check is not a green gate.

**Backend** (from the repo root):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy                    # strict; no Any smuggling
uv run pytest
```

**UI** (from `ui/`):

```bash
npm run lint                   # eslint
npm run test                   # vitest
npm run build                  # tsc -b && vite build — the type check is part of it
```

**End-to-end** (from `ui/`) — Playwright drives the *built* UI against a real server in a
per-run temp workspace:

```bash
npx playwright install chromium   # first run only
npm run e2e
```

Two things about this lane. It runs **one at a time**: the ports are fixed (8788/4173)
and a second run's teardown kills whatever holds them, so a concurrent suite dies with
`ECONNREFUSED 127.0.0.1:4173` and a screenful of failures that are all one cause. And
`WB_E2E_WORKSPACE` keeps the workspace a run happened in — point it at an empty or
nonexistent directory; the suite seeds it and refuses to reuse one that already has
files in it.

**Performance** (from `ui/`) — the same production build against a generated 5,005-file
workspace:

```bash
npm run perf
```

Budgets are work-shaped (how many directories a request lists, how many tree fetches
twenty file changes cost) so they mean the same thing on a laptop and on a shared
runner. Wall-clock tests carry the `@wallclock` tag and are *reported*, never blocking.
When comparing before/after, pin the fixture with `WB_PERF_WORKSPACE=<dir>` — a fresh
5,105-file fixture per run swamps a 100 ms difference in disk and antivirus noise, and a
directory you named is never one the lane deletes.

**Desktop shell** — only if you touched `desktop/`:

```bash
cd desktop/src-tauri
cargo fmt --check
cargo build
cargo test
```

`cargo test` includes the Office-host window tests: they create real windows and start a
synthetic guest process, so they need a desktop session (any normal Windows login). The
two that additionally need the *foreground* — the real-click and hang-isolation
measurements — are `#[ignore]`d; run them with `cargo test -- --ignored --nocapture`.

Optional but recommended: `uv run pre-commit install` wires ruff, trailing-whitespace and
a private-key detector into your commits.

## What CI runs, and where

`.github/workflows/ci.yml`. The matrix is honestly scoped — a job is not matrixed onto an
OS that cannot really run what it tests, because a green tick like that reads as coverage
it does not have.

| Job | Runs on | When |
|---|---|---|
| `server` — ruff, ruff format, mypy, pytest | **windows-latest, ubuntu-latest, macos-latest** | every PR |
| `ui` — lint, test, build | **windows-latest, ubuntu-latest, macos-latest** | every PR |
| `e2e` — Playwright against a real backend | windows-latest | every PR |
| `perf` — budgets (blocking) + timings (reported) | windows-latest | every PR |
| `desktop` — `cargo fmt --check`, `build`, `test` | windows-latest | only when `desktop/` changed |
| `quality-gate` | ubuntu-latest | aggregates all of the above; **the one required check** |

Why the split:

- **`server` and `ui` go cross-OS** because that is where cross-platform bugs actually
  hide: the PTY seam (`services/pty_manager.py` → `services/pty_posix.py`) only ever runs
  its stdlib `pty` backend on the ubuntu and macos legs, and case-sensitive imports and
  path separators in build config are invisible on a single-OS runner. Windows-only
  pieces (pywinpty, pywin32/COM, winreg) are platform markers in `pyproject.toml` and
  `skipif`s with honest reasons — never a silently narrowed assertion. `pytest -rs` prints
  every skip with its reason, so "green" always comes with "and here is what this
  platform did not run".
- **`e2e` and `desktop` stay Windows-only** because they exercise native Windows window
  hosting, which the other two runners cannot have.
- **`perf` stays on one pinned OS** because a fixture number is only meaningful against
  the same runner that produced the last one.
- `server` and `ui` use `fail-fast: false` on purpose: when one OS breaks, the other two
  legs are the diagnosis.

The workflow's shape is itself pinned by `server/tests/test_ci_matrix.py` — if you change
`ci.yml`, expect that test to have an opinion.

**Local development stays Windows-first.** The cross-OS legs prove the backend and the UI
are portable; they do not mean the product is. If you are on macOS or Linux you can build,
type-check and test `server/` and `ui/`, but you cannot run the desktop shell, native
Office hosting, or the E2E suite.

## Standards

These are non-negotiable and CI enforces most of them. `CLAUDE.md` has the full text and
the reasoning; this is the short list.

**Backend**

- Every REST/WS payload is a Pydantic model in `server/src/workbench_server/models/`,
  mirrored in `ui/src/types.ts`. Change both or neither. WebSocket protocols are
  discriminated unions on `type`.
- Routers validate and delegate; the logic lives in `services/`. Everything interesting
  is testable without HTTP.
- structlog only — never `print`. Paths via `pathlib`.
- Disk is the single source of truth for files; every change notification flows through
  the watcher bus.
- Platform code is quarantined behind a `Protocol` in `services/`, chosen at construction.

**UI**

- `DESIGN.md` tokens only. **Zero raw hex** in a component or a stylesheet —
  `ui/e2e/palette.test.ts` re-derives every published colour figure from `tokens.css` and
  fails when one drifts. A new colour is a token PR against `DESIGN.md`.
- Motion is spring-based (`ui/src/design/springs.ts`) and honours
  `prefers-reduced-motion`. `ui/e2e/perf/motion.test.ts` reads the built stylesheets and
  fails on an animated layout property, a `transition: all`, a static `will-change`, a
  hover that costs anything on the way in, or a reduced-motion path that leaves travel in.
  Both of these are `*.test.ts` under `e2e/`, which means they run in `npm run test` — the
  fast gate, not the Playwright one.
- **zustand is the only state library**, and `ui/src/store.ts` is the default home for
  state. A capability may own a second `create()` instance in its own module only if
  nothing outside that module reads it.
- **Nothing that is not needed to paint is statically imported from `main.tsx`.** A module
  script blocks first paint until it is downloaded, parsed *and* evaluated, so anything
  reachable from the entry is paid on every cold start whether the feature is used or not.
  Heavy, on-demand capabilities load behind a dynamic `import()` and are warmed on idle.
  `ui/e2e/perf/bundle.spec.ts` asserts what is *inside* the entry chunk, not only what it
  weighs.
- **A capability registers itself**: a `WorkbenchTool` descriptor in its own module plus
  one line in `ui/src/tools.ts`. Never add a panel or a panel-specific command by editing
  `App.tsx`, `commands.ts` or `StatusBar.tsx` — those files name no capability, and keeping
  it that way is what lets parallel work land panels without colliding. A tool's `id` is a
  stable contract: saved layouts reference panels by it.
- **Panes are instances, not panels.** A pane is a `(toolId, instanceId)` pair, and plural
  is the default for anything a user could plausibly want twice. Nothing assumes it is the
  only one of itself — not in its component, not in the store, not in a CSS selector, not
  in a test. Every plural tool ships a test that opens two instances and asserts they stay
  independent through a save/restore round trip; an unscoped `page.locator` on a
  pane-internal class fails review.

**Everywhere**

- New behaviour ships with tests. Cross-module behaviour gets a full-pipeline test —
  `server/tests/test_watcher.py` is the pattern.
- **Bug fixes start with an end-to-end reproduction**, as close to how a user hits it as
  possible, and *then* the fix. A unit test that fails is not a reproduction; it is a guess
  about where the bug lives. The repro becomes the regression test.
- **No new runtime dependency** without a justification in the PR description — what it
  buys, what it costs, and why the standard library or an existing dependency will not do.
- Agent-facing tools are judged on token cost, not just correctness: a short description
  (every description is loaded into every session's context), compact output, and the
  tool's own test asserting a ceiling on description length *and* on the serialized size of
  a representative result. Every result truncates with a stated size and a way to get the
  rest, says "none" explicitly when empty, and ends with the obvious next step.
- Read `ARCHITECTURE.md` before moving a module boundary.

## Testing layers

Match the layer to the claim (`ARCHITECTURE.md`, *Testing layers*, has the detail):

1. **Unit** — jail, hashing, protocol parsing, key derivation.
2. **Integration** — the real app in-process: an API write → the watcher → a WS event; a
   PTY round trip; scripted agent turns including the permission flow.
3. **Fakes as first-class seams** — `WORKBENCH_FAKE_AGENT=1` and `WORKBENCH_OFFICE_FAKE=1`
   are not mocks bolted onto tests; they are alternative implementations behind the same
   `Protocol` the real ones satisfy, which is why CI can drive chat, tool rows, permission
   prompts, plan cards and the whole Office-host lifecycle with no login and no Office.
4. **E2E** (`ui/e2e/`) — the built UI against a real backend in a temp workspace. Journeys
   wait on the app's own signals; no sleeps.
5. **Perf** (`ui/e2e/perf/` + `server/tests/test_perf_budgets.py`) — work-shaped budgets
   block, wall-clock numbers are recorded.

## Privacy and secrets

Two rules, and they are the kind that are cheap to follow and expensive to fix.

- **Never commit a secret or a personal path.** This is an open-source repo: no
  `C:\Users\<you>\…`, no OnlyOffice JWT secret, no tokens, no machine-specific config. The
  `.workbench/` directory in a workspace is the user's own data and is not committed.
- **Zero telemetry is a product position, not a default.** Workbench collects nothing and
  phones home to nothing (see the README). Do not add analytics, crash reporting, usage
  pings or an update check. A feature that would move any user data off the machine ships
  **off by default and behind explicit, informed consent** — that is why the planned voice
  input transcribes locally rather than using the browser speech API, which streams audio
  to a cloud service.

## Sending the change

Direct pushes to `master` are rejected by a branch ruleset. The loop is:

1. **Branch** off `master`. One writer per checkout — automated contributors work in an
   isolated `git worktree`, never in a checkout something else is using.
2. **Commit** in small, reviewable steps.
3. **`gh pr create`** and fill in the template. "How it was verified" is not optional and
   "CI is green" alone does not answer it: name the test, or give the manual steps and a
   screenshot for UI work. Say what you observed, including anything that did not work.
4. **Wait for `quality-gate`.** It is the single required check and it is green only when
   every job is. A matrix job contributes one aggregate result, so a single red ubuntu leg
   fails the gate exactly like a red single-runner job.
5. **Keep the branch up to date with `master`** — the gate requires it, so rebase (or
   merge `master` in) when `master` moves.
6. **`gh pr merge --squash --auto`** once review is done.

Small, focused PRs review faster, and a PR that owns a disjoint set of files can land in
parallel with everything else — which is most of why the file-ownership rules above exist.
