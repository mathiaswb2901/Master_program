# Architecture

One Python process, one webview window, one optional local Office engine.

```
┌───────────────────────────── Desktop window (Tauri) ─────────────────────────────┐
│  React + dockview UI:  FileTree │ Monaco tabs │ Doc/Sheet/Slides │ Chat │ Term   │
└──────────────┬────────────────────────────────────────────────────┬─────────────┘
               │ REST + WebSockets (localhost)                      │ iframe
┌──────────────▼──────────────────────────────┐      ┌──────────────▼─────────────┐
│  FastAPI backend (server/)                  │ HTTP │  OnlyOffice Docs (native   │
│  files · terminal · watcher · agents · office◄──────►  Windows service, optional)│
└──────┬───────────┬──────────────┬───────────┘      └────────────────────────────┘
       │           │              │
   workspace   pywinpty      Claude Agent SDK ──► claude CLI (machine's login)
   files on    (ConPTY        one client per session, cwd = session folder
   disk        PowerShell)    transcripts in ~/.claude/projects/<encoded-cwd>/
```

## Principles

1. **Disk is the single source of truth.** Editors and agents both act on files. The
   watcher (`services/watcher.py`) turns every filesystem change into a
   `FileChangedEvent` on the in-process bus, fanned out over `/ws/events`. Views
   reconcile via content hashes: a client that recognizes its own write's hash ignores
   the echo; a clean buffer reloads silently; a dirty buffer prompts.
2. **Every wire payload is a Pydantic model** (`models/`). WebSocket protocols are
   discriminated unions on `type`. The UI mirrors these in `ui/src/types.ts`.
3. **Routers thin, services own logic.** A router validates, delegates, maps domain
   errors to HTTP codes. Everything interesting is testable without HTTP.
4. **Platform code is quarantined.** Windows-only bits live in
   `services/pty_manager.py` behind a `Protocol`; a POSIX implementation slots in
   without touching the router.
5. **The Agent SDK is injected.** `services/agent_sessions.py` takes a client factory;
   the real one (`services/sdk_factory.py`) is the only module importing
   `claude_agent_sdk`. Tests script a fake client through the same seam.

## Module map (server/src/workbench_server/)

| Module | Owns |
|---|---|
| `config.py` | pydantic-settings; env prefix `WORKBENCH_` |
| `models/` | REST/WS schemas: files, terminal, agents |
| `routers/files.py` | tree/read/write/create/rename/delete; jail + conflict mapping |
| `routers/terminal.py` | `/ws/terminal` bridge |
| `routers/events.py` | `/ws/events` fan-out |
| `routers/agents.py` | session REST + `/ws/agent/{id}` |
| `services/workspace.py` | path jail, atomic writes, hashing, tree |
| `services/watcher.py` | watchfiles -> bus |
| `services/event_bus.py` | in-process pub/sub |
| `services/pty_manager.py` | ConPTY sessions (Windows) |
| `services/agent_sessions.py` | session state machines, streaming, permissions |
| `services/session_index.py` | per-folder history from Claude Code's storage |
| `services/sdk_factory.py` | real SDK client + context-bridge MCP server |

## Agent sessions

Each live session wraps one `ClaudeSDKClient` with `cwd` bound to a workspace folder.
Streaming uses `include_partial_messages`; the session translates SDK messages into
typed events (text deltas, tool-use notes, status, turn-done) consumed by any number
of WebSocket listeners. Permissions: file tools inside the folder are auto-allowed;
everything else (Bash, web) raises a `PermissionRequest` event and blocks on an
asyncio future until the UI answers (10-minute timeout -> deny). The context-bridge
MCP tool `get_workspace_state` lets agents see the active/open/dirty files so they
avoid editing buffers with unsaved user changes.

Session history is not ours: Claude Code and the SDK persist transcripts under
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. We read that storage
(`session_index.py`), so CLI sessions and Workbench sessions share one history,
grouped per folder.

## Office editing (M2+)

OnlyOffice Docs Community runs as a native local service (port 8880). The backend
builds a PyJWT-signed editor config; the Document Server pulls the file from
`GET /api/office/files/{id}` and posts saves to `/api/office/callback/{id}`, which
writes to disk and re-enters the normal watcher flow. `document.key` derives from the
content hash so external changes (e.g. agent edits) force a reopen instead of serving
a stale cached copy. Absent OnlyOffice, documents degrade to read-only preview +
"Open in Word".

## Testing layers

1. Unit: jail, hashing, protocol parsing, key derivation.
2. Integration: real app in-process — API write -> watcher -> WS event; PTY round-trip;
   scripted-fake agent turns incl. permission flow.
3. Live smoke (`WORKBENCH_LIVE_AGENT=1`): real SDK + machine's Claude login.
4. E2E (Playwright, per milestone): drive the built UI against the real backend.
