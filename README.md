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
- [ ] **M4** — PowerPoint, bundled agent skills, packaged desktop app (Tauri)

## Requirements

- Windows 11 (first target; the architecture is portable)
- Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22+
- A Claude subscription (the machine's existing Claude Code login is used) **or** an `ANTHROPIC_API_KEY`
- Optional, for Office editing: [OnlyOffice Docs Community](https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-windows.aspx) installed locally

## Development

```bash
uv sync --dev
uv run pytest
uv run workbench-server
```

Frontend (once `ui/` lands in M1): `cd ui && npm install && npm run dev`.

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
