# Workbench

**One window for the analyst: code, terminals, real Office documents, and Claude agents.**

Analysts live in five programs at once — an editor for code, a terminal, Excel, Word,
PowerPoint, and an AI chat on the side. Workbench folds them into a single desktop app,
and it runs entirely on your own machine.

> **"Workbench" is a placeholder name.** A real, searchable one has not been chosen yet
> (`ROADMAP.md`, open-source product bar, item 7), so the placeholder is used everywhere —
> the window title, the bundle identifier, this file — and renaming stays one checklist
> rather than an archaeology exercise.

## What it does

- **Code** — Monaco editor with a file tree and tabs. Disk is the source of truth: a file
  changed by an agent or by another program reloads live, and a dirty buffer prompts
  instead of losing your edit.
- **Terminals** — real terminals, as many as you want, in tabs. ConPTY on Windows
  (pywinpty), the standard-library `pty` elsewhere, behind one interface.
- **Office files** — on Windows, in the desktop shell, a **real Word or Excel window is
  docked inside a panel**: Workbench starts a private Office instance, proves the window
  is one it started, and reparents it into the pane, where it follows every resize, tab
  switch and close. Agents read and write the *live* open document over COM. PowerPoint
  is preview-only for now, and OnlyOffice is the preview/diff/fallback path everywhere a
  native window is not available — no shell, no Office installed, hosting turned off.
- **Claude agents** — multiple concurrent [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview.md)
  sessions, each bound to a folder, with conversations grouped per folder and resumable.
  Agents edit the same files your editors show; open buffers reload live, and the tree
  marks who changed what. Plans arrive as clickable cards, permission requests as
  prompts, and plan-usage meters read from the account's own rate-limit events.
- **A fleet, not a chat box** — Mission Control over the running sessions, a managed
  git-worktree pool so parallel agents do not fight over one checkout, objectives that
  close only against evidence, and a review pane that shows the proof behind a result.
- **Search** — workspace-wide content search (`Ctrl+Shift+F`), for you and for agents.
- **QuickBar** — a keyboard-first command line (`Ctrl+K`) with fuzzy file open, every
  registered command (`Ctrl+Shift+P`), and user-defined shortcuts that are *inserted*,
  never executed.
- **Panes** — split anything, focus-mode any pane full-screen (`Alt+M`), and save the
  arrangement as a named layout.

## Zero telemetry

**Workbench collects nothing and phones home to nothing.** This is a product position,
not a default that a future release might quietly flip:

- **No analytics, no usage metrics, no crash reporting, no update ping, no product
  account.** There is no server operated by this project for anything to be sent to.
- **Your files never leave your machine.** Documents are edited in place on disk. There
  is no cloud copy and no hidden database of document state — the watcher reconciles
  views against the disk, and that is the whole model.
- **The backend is a local process, not a service.** It binds `127.0.0.1`, and every REST
  and WebSocket call — bar a couple of bootstrap endpoints that cannot require one — needs
  a bearer token minted fresh for that launch, with a strict Origin allowlist on the
  WebSocket handshake. Loopback is reachable from a web page you happen to have open, and
  that is a real attack rather than a hypothetical one.
- **The one thing that leaves the machine is the agent conversation, and it goes where
  you would expect**: to Anthropic, over **your own** Claude subscription or API key, when
  you talk to an agent — the same path the Claude Code CLI uses. Nothing is sent when you
  are not talking to one. Agent sessions load *workspace* settings only and write nothing
  to `~/.claude`; bundled skills are handed to a session scoped to that session. The
  conversation browser *reads* the transcripts Claude Code already keeps under
  `~/.claude/projects`, to group your chats by folder — reads them, on your disk, and
  sends them nowhere.
- **OnlyOffice, if you use it, is yours too** — a Document Server you install on your own
  machine, reached over loopback. It is optional and not bundled.
- **The UI loads nothing from the internet.** Fonts and assets ship with the bundle; there
  are no CDN links. One end-to-end journey asserts that rendering a visual artifact issues
  *zero* network requests, because that is a safety property only a real browser can
  prove.
- **Anything that would change this ships off by default and behind explicit consent**,
  and says so in plain words. That is why the planned voice input transcribes locally
  rather than using the browser speech API, which streams your microphone to a cloud
  vendor.

You do not have to take this on faith. There is no analytics, telemetry, or updater
dependency in `pyproject.toml`, `ui/package.json` or `desktop/src-tauri/Cargo.toml`;
`grep` them, and point a network monitor at the running app.

## Requirements

- **Windows 11** for the whole product — native Office hosting reparents a real Office
  window into the app, which exists on no other OS. The backend and the UI build and test
  on Windows, macOS and Linux (CI runs both on all three), and degrade to OnlyOffice
  where a native window is not available.
- Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22
- A Claude subscription (the machine's existing Claude Code login is used) **or** an
  `ANTHROPIC_API_KEY` — only for the agent features
- For the desktop shell: a Rust toolchain (`rustup`, MSVC target) and the Visual Studio
  Build Tools; WebView2 ships with Windows 11
- Optional: [OnlyOffice Docs Community](https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-windows.aspx)
  installed locally, for the preview/diff/fallback path

## Getting started

```bash
uv sync --dev
uv run pytest
uv run workbench-server        # backend on 127.0.0.1:8787; the workspace is its CWD
```

Frontend in a browser tab: `cd ui && npm install && npm run dev` (Vite, port 5173).

The real thing — a native window:

```bash
cd desktop && npm install      # the Tauri CLI
npm --prefix ../ui install     # the shell starts Vite from ui/ — skip it and the
npm run tauri dev              # window opens on a dev server that never started
```

That starts Vite itself and then either **attaches** to a backend already listening on
8787 (so `uv run workbench-server` in your workspace keeps owning it) or **spawns** one
from the repo root and reaps it when the window closes. The window opens straight away
and shows that it is starting; nothing connects until the backend answers. Everything
works in both hosts; the shell only adds what a browser tab cannot do — a native window,
native Office hosting, a close guard for unsaved buffers, and the needs-attention badge
on the window title.

`WORKBENCH_PORT`/`WORKBENCH_HOST` move the server, the shell and the dev proxy together.
Which backend the shell chose, and why, is in `shell.log` under
`%LOCALAPPDATA%\dev.workbench.app\logs\` — the bundle identifier is a placeholder too,
and moves with the name.

The workspace starts as the folder the server was launched from (or
`WORKBENCH_WORKSPACE_ROOT`) and is switchable while it runs — the status-bar chip and the
QuickBar's *Switch workspace…* re-root the running server.

## Design principles

- **Disk is the single source of truth.** Editors and agents both act on files; a watcher
  reconciles views. No hidden databases of document state.
- **Typed everything.** Every REST/WS payload is a Pydantic model, mirrored in the UI's
  types; `mypy --strict` in CI.
- **Office fidelity is non-negotiable.** Files must survive open → edit → save → reopen in
  Microsoft Office. No lossy markdown round-trips.
- **Keyboard-first.** Every action has a shortcut, and the QuickBar makes them
  discoverable.
- **Local-first, and honest about it.** See *Zero telemetry* above. Where something is not
  available — no Office, no shell, no login — the app says which and why rather than
  failing quietly.

## Status

Early development, and the plan of record is [`ROADMAP.md`](ROADMAP.md): what has landed,
what is in flight, and what is deliberately deferred. Foundations, the IDE-lite shell and
Office editing are done; the instrument, parallel-agent and premium/public milestones are
where the work is now. It is not packaged yet — you run it from source.

## Documentation

| File | What is in it |
|---|---|
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, the exact gate commands, what CI runs where, the PR workflow |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Module boundaries, the launch path, the testing layers |
| [`DESIGN.md`](DESIGN.md) | The design system — tokens, motion, contrast. Binding for UI work |
| [`ROADMAP.md`](ROADMAP.md) | Milestones, decisions log, deferred ideas |
| [`docs/tools.md`](docs/tools.md) | How a capability registers itself |
| [`docs/shortcuts.md`](docs/shortcuts.md) | The `shortcuts.md` format |

## License

MIT. The OnlyOffice Document Server is a separate, optional, locally-installed service
(AGPL) that Workbench talks to over HTTP; it is not bundled.
