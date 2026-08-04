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
| `models/` | REST/WS schemas: files, terminal, agents, plans, shortcuts |
| `routers/files.py` | tree/read/write/create/rename/delete; jail + conflict mapping |
| `routers/terminal.py` | `/ws/terminal` bridge |
| `routers/events.py` | `/ws/events` fan-out (file changes + session status) |
| `routers/agents.py` | session REST + `/ws/agent/{id}` |
| `routers/shortcuts.py` | `GET /api/shortcuts` (merged shortcuts.md state) |
| `services/workspace.py` | path jail, atomic writes, hashing, tree |
| `services/watcher.py` | watchfiles -> bus |
| `services/event_bus.py` | in-process pub/sub |
| `services/pty_manager.py` | ConPTY sessions (Windows) |
| `services/agent_sessions.py` | session state machines, streaming, permissions, plan artifacts |
| `services/session_index.py` | per-folder history from Claude Code's storage |
| `services/sdk_factory.py` | real SDK client + context-bridge MCP server |
| `services/skills_bundle.py` | locates `skills_bundle/`, the bundled skills plugin shipped as package data |
| `services/shortcuts.py` | shortcuts.md parser + merge + live reload |

## Agent sessions

Each live session wraps one `ClaudeSDKClient` with `cwd` bound to a workspace folder.
Streaming uses `include_partial_messages`; the session translates SDK messages into
typed events (text deltas, tool-use notes, status, turn-done) consumed by any number
of WebSocket listeners. Permissions: file tools inside the folder are auto-allowed;
everything else (Bash, web) raises a `PermissionRequest` event and blocks on an
asyncio future until the UI answers (10-minute timeout -> deny). The context-bridge
MCP tool `get_workspace_state` lets agents see the active/open/dirty files so they
avoid editing buffers with unsaved user changes.

Two fan-outs, deliberately: `/ws/agent/{id}` carries the conversation (deltas, tool
calls and their `tool_settled` results, permission and plan cards) to clients that
opened that session, while every state change is *also* published as a
`SessionStatusEvent` on the shared bus and out over `/ws/events` — so a window with no
socket for a session still tracks its dot, chip and attention badge. Frames a client
may have missed while disconnected (open permission prompts, the pending plan, the last
settled plan verdict) are replayed on connect.

**Bundled skills:** Workbench's own skills are one local Claude Code plugin shipped as
package data (`skills_bundle/`) and passed per session as `--plugin-dir`, so they are
namespaced `workbench:*` (a user skill cannot shadow them), live only as long as that
CLI subprocess, and write nothing to `~/.claude`; a missing bundle degrades to no
skills rather than a failed session. `plan-visual` and `remember` carry a narrow
`Skill(workbench:<name>)` allow rule because the agent is told to reach for them
unprompted; every other skill invocation still raises the permission prompt.
Sessions load the workspace's own settings and nothing above it
(`setting_sources=["project", "local"]` — `.claude/settings.json` plus the
machine-local `.claude/settings.local.json`, so a folder behaves the same here as
in plain Claude Code), which makes what an agent can do a property of the
workspace. `WORKBENCH_INHERIT_USER_SETTINGS=1` adds the global `~/.claude` scope
back — its hooks and permission rules, not only its skills.

**Visual plan artifacts:** `present_plan` (the second context-bridge tool) takes a
`PlanArtifact` — a closed, size-capped discriminated union of option groups, step
lists, questions and markdown (`models/plans.py`), never free-form markup — which
the UI renders as a native card; the user's choices, annotations and verdict come
back to the agent as a typed `PlanResponse` through the same future-and-timeout
discipline as permissions. A timeout or an interrupt resolves to verdict
`no_decision`, never an implied approval, and an `approve` that leaves an option
group unchosen is dropped rather than passed on as one. `plan_id` is minted by the
tool body (the key is stripped from the agent's arguments, not merely absent from
the input schema) so a re-presented plan is always a fresh card. Every settlement
broadcasts a `plan_resolved` frame — that frame, not the click, is what makes a
card read-only, so a second client or a late returner can never assert a verdict
the agent never received. Both pending permissions and a pending plan are replayed
to any client that subscribes after they were emitted, and both are abandoned on
interrupt/close, so neither a reconnect nor a Stop leaves an unanswerable
`needs_attention` session. The factory seam (`ClientFactory`) passes the session
itself as a `SessionBridge` — the bundle of callbacks that must reach the human.

Session history is not ours: Claude Code and the SDK persist transcripts under
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. We read that storage
(`session_index.py`), so CLI sessions and Workbench sessions share one history,
grouped per folder.

## Office editing

**Direction (M4, decided 2026-08-04):** documents open in *real* installed
Word/Excel/PowerPoint, docked into Workbench panels via native window hosting
(launch → find HWND by class `OpusApp`/`XLMAIN`/`PPTFrameClass` → `SetParent` into a
host window + child styling; spike-proven). A COM automation bridge (pywin32) lets
agents read/write the live open document instead of fighting file locks. This requires
the Tauri shell — a browser tab cannot host native windows. The OnlyOffice integration
below remains as preview, document diffing for review, and fallback when Office isn't
installed.

### OnlyOffice (preview/diff/fallback)

OnlyOffice Docs Community runs as a native local service (port 8880). The backend
builds a PyJWT-signed editor config; the Document Server pulls the file from
`GET /api/office/files/{id}` and posts saves to `/api/office/callback/{id}`, which
writes to disk and re-enters the normal watcher flow. `document.key` derives from the
content hash so external changes (e.g. agent edits) force a reopen instead of serving
a stale cached copy. Absent OnlyOffice, documents degrade to read-only preview +
"Open in Word".

## Shortcuts

`<workspace>/.workbench/shortcuts.md` merged over `~/.workbench/shortcuts.md` (workspace
wins per name). The workspace file rides the existing watcher — its `FileChangedEvent` on
the bus is the reload trigger — while the global one, living outside the workspace, gets
its own small `watchfiles` watch; a reload that changes the merged state publishes
`ShortcutsChangedEvent` and the UI refetches. Entries extend the command registry
(`ui/src/commands.ts`) dynamically, and built-ins win every id/chord collision. Parsing is
total: a bad entry becomes a `problem` in the payload, never an exception. **Entries are
inserted, never executed** — a shell body is typed into the active terminal with no
trailing newline (and must be single-line, since a newline in a PTY is Enter), a prompt
lands in the chat draft. Format spec: `docs/shortcuts.md`.

## Testing layers

1. Unit: jail, hashing, protocol parsing, key derivation.
2. Integration: real app in-process — API write -> watcher -> WS event; PTY round-trip;
   scripted-fake agent turns incl. permission flow.
3. Live smoke (`WORKBENCH_LIVE_AGENT=1`): real SDK + machine's Claude login.
4. E2E (Playwright, per milestone): drive the built UI against the real backend.
