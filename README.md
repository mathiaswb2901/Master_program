# Workbench

**One window for the analyst: code, terminals, real Office documents, and Claude agents.**

Analysts live in five programs at once — an editor for code, a terminal, Excel, Word,
PowerPoint, and an AI chat on the side. Workbench folds them into a single desktop app:

- **Code** — Monaco editor with file tree and tabs
- **Terminals** — real PowerShell terminals (ConPTY), as many as you need
- **Office files** — open and edit `.docx`, `.xlsx`, `.pptx` at full fidelity
  (OnlyOffice engine; tracked changes in Word included), right next to your code
- **Claude agents** — multiple concurrent [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview.md)
  sessions, each bound to a folder, with chats grouped per folder. Agents edit the same
  files your editors show; open buffers reload live.
- **QuickBar** — a keyboard-first command line (Ctrl+K) with fuzzy file open and
  user-defined aliases

## Status

Early development. Milestones:

- [ ] **M0** — foundations (this repo: typed FastAPI core, CI, design system)
- [ ] **M1** — IDE-lite shell: terminal, files, Monaco, file-sync engine, multi-session agent core, QuickBar
- [ ] **M2** — Word editing (OnlyOffice, native Windows install — no Docker)
- [ ] **M3** — Excel editing
- [ ] **M4** — PowerPoint, bundled agent skills, the Tauri desktop shell, real Office
      window hosting

## Requirements

- Windows 11 (first target; the architecture is portable)
- Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22+
- A Claude subscription (the machine's existing Claude Code login is used) **or** an `ANTHROPIC_API_KEY`
- For the desktop shell: a Rust toolchain (`rustup`, MSVC target) and the Visual Studio
  Build Tools; WebView2 ships with Windows 11
- Optional, for Office editing: [OnlyOffice Docs Community](https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-windows.aspx) installed locally

## Development

```bash
uv sync --dev
uv run pytest
uv run workbench-server        # backend on 8787; the workspace is its CWD
```

Frontend in a browser tab: `cd ui && npm install && npm run dev` (Vite, port 5173).
`ui/` and `desktop/` have separate dependency trees — install in both, and re-run
`npm install` after pulling a branch that adds one. A stale `node_modules` announces
itself as a Vite `Failed to resolve import` error rather than as a missing dependency.

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
a close guard for unsaved buffers, and the needs-attention badge on the window title.

`WORKBENCH_PORT`/`WORKBENCH_HOST` move the server, the shell and the dev proxy together.
Which backend the shell chose, and why, is in `shell.log` under
`%LOCALAPPDATA%\dev.workbench.app\logs\`.

## Design principles

- **Disk is the single source of truth.** Editors and agents both act on files; a watcher
  reconciles views. No hidden databases of document state.
- **Typed everything.** Every REST/WS payload is a Pydantic model; `mypy --strict` in CI.
- **Office fidelity is non-negotiable.** Files must survive open → edit → save → reopen
  in Microsoft Office. No lossy markdown round-trips.
- **Keyboard-first.** Every action has a shortcut; the QuickBar makes them discoverable.

## License

MIT. The OnlyOffice Document Server is a separate, optional, locally-installed service
(AGPL) that Workbench talks to over HTTP; it is not bundled.
