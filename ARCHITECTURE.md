# Architecture

One Python process, one webview window, one optional local Office engine.

```
┌───────────────────────────── Desktop window (Tauri) ─────────────────────────────┐
│  shell (desktop/src-tauri): window · backend supervision · close guard · title   │
│ ┌─────────────────────────────── WebView2 ────────────────────────────────────┐  │
│ │ React + dockview UI:  FileTree │ Monaco tabs │ Doc/Sheet/Slides │ Chat │ Term│  │
│ └─────────────────────────────────────────────────────────────────────────────┘  │
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
6. **The shell holds only what a browser tab cannot.** The UI runs unchanged in
   a Vite tab and in the Tauri window; every native capability goes through
   `ui/src/shell.ts`, which no-ops outside the shell. See below.

## The desktop shell (`desktop/`)

Not packaging polish — a **requirement**. Real Word/Excel/PowerPoint windows are
reparented into panels (`SetParent`), and a browser tab has no HWND to parent
them to. The shell (`desktop/src-tauri/`, Rust + Tauri 2) owns four things:

| Concern | Why it cannot live in the UI |
|---|---|
| The native window | `dragDropEnabled: false` — an OS drag-drop handler over the whole window would fight the native children hosted there from PR 3 on |
| Backend supervision (`backend.rs`) | Nothing in a webview can start or outlive a process |
| Close guard | WebView2 ignores `beforeunload`, so a native close silently discarded dirty buffers |
| Attention badge | `document.title` never reaches a native title bar or the taskbar |

**Backend supervision.** One probe of `GET /api/health` decides: if a *Workbench*
backend is already listening the shell **attaches** — a developer's own `uv run
workbench-server` keeps owning the workspace (its CWD *is* the workspace) and
outlives the window. The probe checks the response body, not just the status
line: any local proxy or file server can answer 200 on an unknown path, and
attaching to one gives a window that 404s every `/api/*` call under a log line
saying all is well. Otherwise the shell **spawns** one from the repo root with
`CREATE_NO_WINDOW` and pipes its output into the shell log.

The spawn is confined by a Windows **Job Object** with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` that *this process joins before spawning
anything*. Assigning the child afterwards loses a race: `uv run
workbench-server` is a launcher, and the uvicorn process holding the port is its
grandchild, which can be created before `AssignProcessToJobObject` runs — an
orphan that keeps 8787 and gets adopted by every later launch. Job membership is
inherited, so joining first makes that window zero-width. The handle is held for
the process lifetime and never closed, so every descendant dies when this
process does — including a crash or a kill, where no shutdown code of ours would
run. (Measured: a graceful-quit path left orphans; the job object did not.)
Sidecars (`bundle.externalBin`, `tauri-plugin-shell`) are deliberately unused:
documented orphan bugs, and no equivalent guarantee.

Supervision runs on a worker thread and the window opens immediately. Tauri
creates the config's window only after `setup` returns, so waiting there meant
no window, no taskbar entry and no feedback for as long as a cold start took.
The UI is what must not race the backend — `/ws/events` reconnects, the terminal
does not — so it waits for `workbench://backend-ready` before opening any
socket, showing a starting-up gate until then.

Everything the shell decides goes to `shell.log` in the app log dir as well as
stderr. Release builds are `windows_subsystem = "windows"` and have no stderr at
all, which is exactly the build where "why is there no backend?" gets asked.

**Close guard.** `CloseRequested` → `prevent_close()` → `workbench://close-requested`
to the UI → the same confirm modal the editor tabs use, across every dirty
buffer at once → `confirm_close` (or `cancel_close`) back over IPC. The guard is
*armed by the UI* (`shell_ready`), never assumed: a webview that never ran our
code closes normally instead of leaving a window that cannot be closed. Two more
escape hatches exist because an unclosable window is a worse failure than the
buffer being protected — and killing one from Task Manager reaps the supervised
backend too:

- **Navigation disarms it** (`on_page_load`). A reload onto a dev server that
  has since died leaves a page that will never register a listener; the UI
  re-arms on every load.
- **A prompt must be acknowledged** (`close_ack`) within a few seconds or the
  shell stops holding the window. `emit` cannot report this: Tauri delivers
  events by evaluating a script in the webview, which succeeds on any live page
  — including one with no listener — so its `Err` is not the undelivered signal
  it looks like. Only the *ack* is on that clock; once the modal is up the user
  has as long as they like. The state machine and all of its transitions are
  unit-tested in `close_guard.rs`.

**Both hosts, always.** `ui/src/shell.ts` is the only module importing
`@tauri-apps/api`, dynamically and only after `isTauri()` passes, so a browser
build never fetches the chunk and every call is inert in a tab.

## Tool registry

A **tool** is one capability, declared in one descriptor next to its own code
(`ui/src/registry.ts` for the type, `ui/src/tools.ts` for the list). It may
contribute any of:

| Field | What registering gets you |
|---|---|
| `panel` | A dockview panel: component, where it docks, whether it opens with the app, whether it is a singleton, and the one badge its tab may carry. `App.tsx` names no panel — it renders `panelComponents(TOOLS)`, applies `defaultLayout(TOOLS)` and draws its tabs from `panelTabInfo(TOOLS, …)` |
| `documentView` | A renderer for one `OpenFile` kind inside the editor area. Office claims `office`; the native Office host will claim it back through the same field |
| `commands` + `shortcuts` | QuickBar rows and keymap entries. Commands are the same `Command` shape `commands.ts` already used, so the QuickBar, the pass-through policy and the `shortcuts.md` merge are unchanged; the chords live in one table per tool, which is the layer a user keymap file overrides later |
| `dynamicCommands` | Rows whose *set* changes while the app runs — one per saved layout today. Re-derived only when the tool's `key()` changes, since the merged list is read on every keystroke, and never chord-bearing: a chord must be static to be pinned by a test and to win a `shortcuts.md` collision deterministically |
| `statusContributions` | Items in the status bar's left/centre/right regions. The bar owns the regions and nothing that goes in them |
| `shortcutKinds` / `shortcutActions` | Which `shortcuts.md` kinds this panel *hosts an insertion for* (the Terminal claims `shell`, the Agent `prompt`) and which it *carries out* (Layouts claims `layout`) — so `commands.ts` names neither a panel nor a kind |
| `onDockReady` | The live `DockviewApi`, for a tool that operates on the dock rather than living in it. Exactly one does |
| `when` | A predicate that takes the whole tool out — panel, commands and status items together. **Boot-time**: it is asked once per tool and remembered, because the things it feeds are derived at different moments and a tool that enabled itself later would be half present |

A panel's tab is closable exactly when it is *not* in the startup layout: one
that a command opened must be dismissible by the tab it arrived on, while
closing a startup panel is not how you rearrange the window — the layout system
below is.

Agent-facing tools are **not** in this descriptor — `services/agent_tools.py` is
their single registry (below).

Derivation is what makes it a registry rather than a list: `Ctrl+1..N` focus
commands are generated from the panels *in the default layout*, in registry
order, so the four familiar chords are simply the first four registered panels
and a fifth would get `Ctrl+5` by existing. Every derivation in `registry.ts` is
a pure function of a tools array — never of `TOOLS` itself — which is what makes
them unit-testable with fixtures and what makes a second, differently-sourced
array possible.

**Registration is static**: `TOOLS` is an array assembled from per-tool modules
at build time, so the bundler sees every import and `tsc` type-checks every
descriptor. That is the deliberate stopping point for now. **The plugin seam**
is the shape, not a loader: because nothing reads `TOOLS` except the four call
sites that pass it in, a later loader can concatenate descriptors from
`.workbench/` (or an installed package) onto the same array and the shell will
host them unchanged. That is the M7+ endgame in `ROADMAP.md` — the difference
between a fixed app and an instrument.

**The exit criterion, demonstrated.** `ui/src/panels/Scratchpad.tsx` is a whole
capability — a panel that opens on demand and closes again, its command, its tab
icon, a file on disk — added in one new module plus one line in `tools.ts`. It
touches no file another work lane is likely to touch. It deliberately claims no
chord: a registered chord beats a `shortcuts.md` one and `Alt` is the only
modifier that file may use, so every chord a tool takes is one a user cannot
have — a price the Terminal's `Alt+T` earns and a worked example does not.
`docs/tools.md` is the walkthrough.

**Agent-facing tools** carry the ergonomics budget on both sides.
`services/agent_tools.py` is the server registry the SDK actually reads: name,
model-facing description, input schema, and a **required** `output_format`, so
`mypy --strict` fails an omission. `sdk_factory.py` builds the MCP server *and*
the session's allow-list from that list, so a tool is added in one place. The
budget is enforced where it can fail — `server/tests/test_agent_tools.py`
asserts a ceiling on every description (loaded into every session's context, so
paid for on every request), a **per-tool** ceiling on the serialized result,
sized from the measured representative payload plus a stated margin, and that
results are compact JSON rather than pretty-printed. Per-tool because one shared
number large enough for the chattiest tool is a number no other tool can exceed,
and a budget that cannot fail does not bind. The one result path that is not
ours to size — a pydantic validation error — is clamped to the ceiling rather
than trusted to fit.

There is no second copy of any of this in the UI. A capability declares its
panel and commands in `ui/src/registry.ts` and its agent-facing tools here, once:
a duplicate of the model-facing text on the client would be another authority to
keep honest with nothing reading it. Latency is not budgeted: these are
in-process calls where the model and the user dominate.

## Module map (server/src/workbench_server/)

| Module | Owns |
|---|---|
| `config.py` | pydantic-settings; env prefix `WORKBENCH_` |
| `models/` | REST/WS schemas: files, terminal, agents, plans, shortcuts, provenance, layouts, office host |
| `routers/files.py` | tree/read/write/create/rename/delete; jail + conflict mapping |
| `routers/terminal.py` | `/ws/terminal` bridge |
| `routers/events.py` | `/ws/events` fan-out (file changes + session status) |
| `routers/agents.py` | session REST + `/ws/agent/{id}` |
| `routers/shortcuts.py` | `GET /api/shortcuts` (merged shortcuts.md state) |
| `routers/provenance.py` | `GET /api/provenance` + acknowledge |
| `routers/layouts.py` | `GET`/`PUT /api/layouts` (this workspace's saved arrangements) |
| `routers/office_host.py` | open/list/move/detach/close a hosted document; `GET /api/office/capabilities` |
| `services/workspace.py` | path jail, atomic writes, hashing, tree, `top_level_dirs` (one listing, no walk) |
| `services/watcher.py` | watchfiles -> bus |
| `services/ignore.py` | what the tree and watcher skip: noise names, plus `CACHEDIR.TAG` build caches |
| `services/event_bus.py` | in-process pub/sub |
| `services/pty_manager.py` | ConPTY sessions (Windows) |
| `services/agent_sessions.py` | session state machines, streaming, permissions, plan artifacts |
| `services/session_index.py` | per-folder history from Claude Code's storage |
| `services/agent_tools.py` | the agent-facing tool registry + its ergonomics budget |
| `services/sdk_factory.py` | real SDK client + context-bridge MCP server |
| `services/skills_bundle.py` | locates `skills_bundle/`, the bundled skills plugin shipped as package data |
| `services/shortcuts.py` | shortcuts.md parser + merge + live reload |
| `services/layouts.py` | `.workbench/layouts.json`: atomic write, and a read that never raises |
| `services/provenance.py` | correlates agent tool calls with watcher events; who changed a file |
| `services/office_host/` | hosting real Office windows: `backend.py` (the Protocol the native implementation must satisfy), `fake_backend.py` (in-process stand-in), `state.py` (the lifecycle), `service.py` (hosts by id, events, reaping) |

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

## Provenance

Agents write to disk with their own tools, so a file can change under the user
with nothing saying who did it. `services/provenance.py` correlates the two
signals that already exist: a live session announces a tool call naming a path
(`Write`/`Edit`/`MultiEdit`/`NotebookEdit`, matched on the last name segment so
namespaced MCP tools count), and moments later the watcher reports that file
changed. A match inside `ATTRIBUTION_WINDOW_S` (10 s) — **exact path only**,
after normalizing backslashes, quotes, `..` segments and paths relative to the
session's own folder — becomes a `ProvenanceEntry` on the bus as
`FileProvenanceEvent`, and into the map `GET /api/provenance` serves for initial
load and reconnect. The UI shows it as a right-aligned dot in the tree, a dot on
a background editor tab, and a one-line bar above the buffer that names the
session and links back to that conversation — the return leg of the chat's
tool-row file links.

**What the heuristic can and cannot know.** It is a claim about the user's own
files, so it is built to be silent rather than wrong:

- No matching tool call → the change is reported **unattributed** (`agent` is
  `null`) and the UI shows nothing. A git checkout, an external editor, a build
  step and the user's own `Ctrl+S` all land here, correctly. The most recent
  session is *never* named as a guess.
- A claim is announced *before* the tool runs, so it is **withdrawn** when the
  tool turns out not to have written anything: a declined permission card
  (`note_tool_denied`, fired the moment the user clicks Deny) or an error result
  (`note_tool_result`, e.g. an `Edit` whose old string was not found). Otherwise
  a refused `Write` would keep a live claim for the rest of the window and be
  credited with whatever changed that path next — typically the user making the
  same fix by hand.
- Two sessions writing the same path inside the window is a genuine ambiguity;
  the rule is **most recent exact match wins**, and it is tested.
- A claim is not consumed by the first change it explains — one logical write
  routinely surfaces as several watcher events on Windows, and calling the
  follow-ups "the user" would be the false claim we are avoiding. Deletions are
  skipped for the same reason: order inside a watchfiles batch is not
  guaranteed.
- An unattributed change to a tracked path **clears** the entry, so a file the
  user has since rewritten stops being credited to an agent. Every write
  Workbench itself makes on the user's behalf says so explicitly
  (`note_user_write`): the editor's `PUT /api/files/content`, file create and
  rename, and the OnlyOffice save callback — that last one matters most, since a
  `.docx` is the file a user cannot diff for themselves.
- An entry the LRU evicts is **cleared on the wire** too. Clients hold their own
  copy of the map, and the clear branch above only fires for paths the server
  still tracks, so a silent eviction would strand a stale attribution on screen.
- A path the agent spelled differently than the watcher reports it (a different
  case on Windows), a write outside the workspace, or a file written by a shell
  command rather than a file tool: all unattributed.

Acknowledgment is explicit: opening the file (tree click or tab activation) or
dismissing the bar marks the entry `acknowledged`, which clears **the two dots**
— tree row and background tab — and keeps the attribution. A later agent change
to the same path reopens them. The editor bar is not an unread marker and is
deliberately not gated on `acknowledged` (DESIGN.md §6.1): it answers "who wrote
what I am reading", carries the only link back to that conversation, and stands
until the attribution itself is retracted or the user dismisses it — a dismissal
the UI persists in `localStorage`, since a reload undoing it would put the line
back above a buffer the user had already decided about. A tree marker on a file
inside a collapsed folder is only visible once that folder is expanded — a
folder-level rollup is not built; the editor bar and the chat's tool row are the
other two places the same change surfaces.

State is **in memory only** — a server restart forgets every attribution and
`GET /api/provenance` comes back empty — and bounded: `MAX_TRACKED_PATHS` (500,
LRU) entries and `MAX_PENDING_CLAIMS` (200) in-flight claims, so a long session
cannot grow it without limit.

## Office editing

**Direction (M4, decided 2026-08-04):** documents open in *real* installed
Word/Excel/PowerPoint, docked into Workbench panels via native window hosting
(launch → find HWND by class `OpusApp`/`XLMAIN`/`PPTFrameClass` → `SetParent` into a
host window + child styling; spike-proven). A COM automation bridge (pywin32) lets
agents read/write the live open document instead of fighting file locks. This requires
the Tauri shell — a browser tab cannot host native windows — which now exists (see
above). The OnlyOffice integration below remains as preview, document diffing for
review, and fallback when Office isn't installed.

### Office host (`services/office_host/`)

The domain layer for "a real document hosted in a panel" is built **fake-first**:
the lifecycle, the ownership rules and the whole REST/WS surface exist and are
tested on a machine with no Office and no Rust, and the native implementation
slots in behind one `Protocol`. That seam is the point — it is what makes the
risky part testable before it is written.

```
routers/office_host.py ──► OfficeHostService ──► HostBackend (Protocol)
   REST                       state machine        ├── FakeHostBackend   (today)
   /ws/events ◄── OfficeHostEvent on the bus       └── Win32/COM backend (later PR)
```

**Lifecycle.** `launching → embedding → embedded`, with `detached` (window given
back to the desktop, document still open) as the one live state you can return
from. `closed`, `crashed` and `failed` are terminal and never move again; the
record stays, because it is the answer to "what happened to that document", and
`GET /api/office/hosts` replays it to a client that reconnected. Every change is
published as an `OfficeHostEvent` on the existing bus, so hosting needed no new
plumbing and a window that never issued the request still tracks the state.

**The backend contract is deliberately small** (`backend.py`): launch, embed,
set_bounds, detach, close, poll — every one of them something a real
`SetParent` implementation can actually do, all `async` because launching Word
costs about a second. There is no synchronous screenshot and no document
mutation: reading and writing the *live* document is COM work arriving with the
bridge in a later PR, behind its own seam. Polling is the only crash signal
there is — nothing calls back when Word disappears.

**Never adopt a process we did not launch.** Reparenting is destructive: the
window moves into our panel and its chrome is restyled, so doing it to an
instance the *user* started would hijack their session, and closing our panel
would then close their work. A backend that admits it found the instance
(`HostHandle.adopted`) is refused outright, and the pid it *did* launch is bound
to the host for life, so no later handle can be substituted. "Already open
elsewhere" is a first-class refusal with a reason the UI can show, never a
silent takeover. An instance we launched is always reaped — including when the
embed is refused, when the user closes the panel mid-launch, and on server
shutdown. A close that is *refused* (a "Save changes?" modal eating `WM_CLOSE`)
does not count as one: the host still settles, but its record carries
`close_failed` and the poll sweep re-asks until the process is really gone,
because "closed" with a real Word still on screen is a claim the server cannot
make.

**Nothing waits forever.** A backend is asked to bound its own work and raise
(`LaunchTimeoutError` is exactly that), and the service does not take it on
trust: every backend call runs under a ceiling of its own and is cancelled when
it runs out. Without it, one implementation that forgets would hang the request
that started it — and, through it, the lifespan shutdown that would have reaped
the window. Where the panel puts the window is `host.rect` and only that: bounds
arriving while the launch or the embed is still in flight are written there and
read back by the embed itself, so a panel resized during Word's ~1s startup is
embedded where it *is*, not where it was when the request was sent.

**Two owner decisions are encoded, not just documented** (2026-08-05).
PowerPoint is **preview-only**: it is single-instance and exposes no
`Application.Hwnd` to prove a window is ours, so the service refuses a
PowerPoint host (`powerpoint_preview_only`) rather than risk reparenting the
user's own open presentation. And native hosting stays behind
`WORKBENCH_OFFICE_NATIVE`, where **`auto` currently resolves to *not* hosting
natively** — it becomes the default only once hang isolation is proven.
`GET /api/office/capabilities` says all of this out loud (mode, whether hosting
is available at all, whether Office was detected, whether the *fake* backend is
answering, and whether the fallback is OnlyOffice or read-only preview) so the
UI degrades from a fact rather than a guess.

**The fake backend** (`WORKBENCH_OFFICE_FAKE=1`, off by default, warned about at
startup — the `WORKBENCH_FAKE_AGENT` precedent) walks the same lifecycle in
process and starts nothing: its pids are counters. Failures are chosen
programmatically or by the *name of the document* (`…-refuse-embed.docx`,
`…-crash-after-embed.docx`, `…-already-open.docx`, …), so every branch is
reachable from a test and, later, from the UI.

**Deliberately deferred:** the pywin32 COM bridge (and with it the agent-facing
document tools) and the panel — which lands after the tool registry, so it can
register itself instead of editing `App.tsx`. Nothing here changes the
OnlyOffice path below.

### Native window hosting (`desktop/src-tauri/src/host/`)

The mechanism that puts a real child window inside a panel rectangle, built and
proven against a **synthetic guest** (`src/bin/workbench-guest.rs`) rather than
against Word. Every hard part — class registration, style stripping,
`SetParent`, geometry, focus, teardown — is independent of who owns the guest
window, so proving it against a window we wrote makes the whole thing runnable
in `cargo test` on any machine, with no Office installed. The guest is a
separate **process** on purpose: reaping, input-queue attachment and hanging are
all cross-process problems, and a second window on one of our own threads would
prove none of them. It is debug-only — its body is behind `debug_assertions` and
`lib.rs` keeps three explicit command lists so a release build cannot reach it.

```
Tauri window (main thread, owns the message loop)
└── panel window     WS_CHILD, one per hosted document, our class
    └── clip child   WS_CHILD, the viewport
        └── guest    another process's top-level window, restyled WS_CHILD and
                     offset up by its own caption height
```

Styles are stripped **before** `SetParent` (as the `SetParent` docs ask) and
`SWP_FRAMECHANGED` applied after, which is what makes the guest's client
rectangle match the panel exactly rather than approximately. Teardown restores
the original styles, parent and desktop position — an un-parented window still
wearing child styles has no caption, no border and no way to be closed. Guests
are launched into a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and
reaped by closing it, for the reason `backend.rs` documents: a graceful quit was
measured not to be enough. Because a guest is a *child* of the Tauri window and
children die with their parent, every path that closes the window releases the
guests first — otherwise closing Workbench would take a real Word down with it.

CSS pixels become physical pixels through **one** DPI authority, the window's own
`scale_factor()`; edges are rounded and sizes derived from the rounded edges, so
two adjacent panels cannot leave a one-pixel seam. Nothing scaled is ever cached —
the caption inset a panel hides is *stored* in CSS pixels and re-derived on every
layout, because dragging the window to a monitor at another scale arrives as a
plain resize and has to come back with different physical numbers for the same
rectangle. Mind the unit boundary at the seam: these Rust commands take **CSS**
pixels, while the Python `PanelRect` is documented in **physical** pixels. A
bridge between the two must convert, or the rectangle is scaled twice.

**Four things were measured that the documentation does not tell you**, each one
now a test in `host/hosting_tests.rs`:

| Claim | What actually happens |
|---|---|
| `WM_PARENTNOTIFY` tells a parent its child was destroyed or clicked | **It never arrives** for a window reparented in from another process — not on a graceful `WM_CLOSE`, not on a killed process, not on a real `SendInput` click. Destruction is therefore found by asking (`host/watchdog.rs`), which is exactly what the Python `HostBackend::poll` models |
| Click-to-focus needs the host to route the click | It does not. The guest's own window procedure focuses itself, and the keyboard follows. What is lost is only the *notification* to the webview |
| `SetFocus` across processes needs `AttachThreadInput` | It does not, for a parent/child pair: that relationship already attaches the input queues. No `AttachThreadInput` call exists in this crate |
| `SWP_ASYNCWINDOWPOS` keeps a hung guest from stalling the host | **It does not**, for the same reason: the flag only posts when the two threads are on *different* input queues, and being our child put them on the same one. `DeferWindowPos` also rejects the flag outright (`ERROR_INVALID_PARAMETER`), so our own two windows are batched and the guest is moved separately |

**Hang isolation is the open risk, and it is now quantified.** With the guest
deliberately wedged (`hosting_tests::hang_isolation_measurement`): the host
window keeps its own message loop (50/50 posted messages dispatched), keeps
painting, and Windows does not judge it hung — but a resize costs **~1 s per
frame** because the guest's `SetWindowPos` waits. Moving *our* two windows alone
stays at ~0.02 ms, and the same guest move issued from a thread that owns no
window in the parent chain — and so is attached to no input queue — takes
**~0.15 ms**. That is the containment path, measured rather than assumed, and it
is deliberately not implemented here: `WORKBENCH_OFFICE_NATIVE=auto` stays off
until it is.

### OnlyOffice (preview/diff/fallback)

OnlyOffice Docs Community runs as a native local service (port 8880). The backend
builds a PyJWT-signed editor config; the Document Server pulls the file from
`GET /api/office/files/{id}` and posts saves to `/api/office/callback/{id}`, which
writes to disk and re-enters the normal watcher flow. `document.key` derives from the
content hash so external changes (e.g. agent edits) force a reopen instead of serving
a stale cached copy. Absent OnlyOffice, documents degrade to read-only preview +
"Open in Word".

## Layouts

The window remembers its arrangement, per workspace, and one panel can take all
of it. Both are one registered capability (`ui/src/panels/Layouts.tsx`) that
contributes **no panel**: commands, a status chip, a `shortcuts.md` kind and an
`onDockReady` hook. `App.tsx` gained one reordered line for it and names nothing.

- **Focus mode** is dockview's `maximizeGroup` / `exitMaximizedGroup` on the
  active panel, bound to `Alt+M`. Restoring is dockview's own hidden-view
  bookkeeping, so the arrangement comes back exactly — including the sizes.
- **Persistence** is `dockview.toJSON()`, debounced onto
  `<workspace>/.workbench/layouts.json` through `PUT /api/layouts`. The file is
  *in* the workspace, which is what makes an arrangement a property of the
  project rather than of a browser origin — the same convention `shortcuts.md`
  and `scratch.md` already follow, and already gitignored. The server stores the
  document verbatim (`JsonValue`): its shape belongs to a UI library this
  process does not import, and every rule about which panels may be in it is a
  *client* fact, so a server-side schema would be a second authority going stale
  the moment a tool is added.
- **Named layouts** are presets built from the registry (`Review`, `Focus`,
  `Agents`) plus whatever the user saved. A preset names **tool ids**, not
  geometry, so one naming a tool that is gone simply builds without it.
- **Restore is vetted, always.** `ui/src/layouts.ts` prunes a persisted layout
  against `panelComponents(TOOLS)` before dockview sees it, because dockview
  restores a panel whose component is unregistered and hands React `undefined`
  as the element type — one stale entry would take the window down. Unknown
  panels are dropped, empty groups and branches collapse, a dropped active view
  is forgotten, and `grid.maximizedNode` (a *path*, not an id) is carried over
  only when nothing was dropped. Nothing usable left, a file that is not a
  layout, a `fromJSON` that throws: all three resolve to the default arrangement
  plus one toast. The floor is a working window, never a blank one.
- **A failed apply reports which failure it was.** Pruning vets panel ids, not
  dockview's grid algebra, so a file whose every panel is registered can still
  make dockview's deserializer throw — and dockview calls `clear()` before it
  validates, so the fallback rebuilds the *default* arrangement. That is a
  different outcome from "nothing usable, window left alone", and
  `applySerialized` returns `applied` / `unchanged` / `default` rather than a
  boolean so a caller cannot label the window with a layout it is not showing.
  The default arrangement is nobody's named layout: the chip goes unnamed, and
  that is what gets persisted.
- **Writes are serialized.** `PUT /api/layouts` replaces the whole document and
  the server persists whatever arrives last, so two requests in flight at once
  land in delivery order rather than in the order the user acted. Every write
  goes through one chain and reads the document at the moment it is sent, which
  makes a queued write send the *current* arrangement and makes a second queued
  write redundant. The debounce coalesces a drag; this is what covers two
  deliberate actions a moment apart.
- **The atomic write retries a Windows lock.** `os.replace` onto a path another
  process has open fails on Windows rather than waiting, and serialized writes
  land ~20 ms apart — close enough that the watcher, Defender or the indexer
  reacting to the *previous* write was losing the second one about half the
  time, leaving the file holding the arrangement the user had moved away from.
  `services/layouts.py` retries past it on a short bounded budget; a lock that
  outlasts the budget is a real one and still surfaces as a 500 and a toast.
- **Two conservative choices** worth knowing: the autosave is armed only after a
  successful read, so a backend that did not answer cannot let this session's
  default arrangement overwrite the user's file; and switching to a *preset*
  rebuilds the dock (`clear()` + placements) while switching to a *saved* layout
  uses `fromJSON(…, { reuseExistingPanels: true })`, which moves the panels that
  exist in both rather than recreating them.

## Shortcuts

`<workspace>/.workbench/shortcuts.md` merged over `~/.workbench/shortcuts.md` (workspace
wins per name). The workspace file rides the existing watcher — its `FileChangedEvent` on
the bus is the reload trigger — while the global one, living outside the workspace, gets
its own small `watchfiles` watch; a reload that changes the merged state publishes
`ShortcutsChangedEvent` and the UI refetches. Entries extend the command registry
(`ui/src/commands.ts`) dynamically, and everything the tool registry contributes wins
every id/chord collision — a file cannot shadow `Ctrl+S` or `Alt+T`. Parsing is
total: a bad entry becomes a `problem` in the payload, never an exception; markdown
inside any fence is example text, so a `##` line there registers nothing. **Nothing an
entry can do executes** — a shell body is typed into the active terminal with no
trailing newline, a prompt lands in the chat draft, and a `layout` body names one of the
user's own saved arrangements and moves panels (the one kind that acts, and the reason it
may is that moving panels is all it *can* do; its body is one line no longer than a
layout name, so it cannot carry a payload). Two invariants carry the rest, each
enforced on both sides of the wire: a shell body is a single line of *printable* text
(in a PTY the control bytes are key events — `\n` is Enter, `\x0f` is accept-line), and
a file-supplied chord must carry `Alt` (outside Monaco/xterm the app intercepts every
Ctrl chord, so `Ctrl+V` from a file would take paste away from every input).
Format spec: `docs/shortcuts.md`.

## Testing layers

1. Unit: jail, hashing, protocol parsing, key derivation.
2. Integration: real app in-process — API write -> watcher -> WS event; PTY round-trip;
   scripted-fake agent turns incl. permission flow.
   - **Layer 2.5 — fake-agent mode** (`WORKBENCH_FAKE_AGENT=1`):
     `services/fake_agent.py` is a `ClientFactory` that answers deterministically — a
     streamed markdown reply, a `Read` of a real workspace file, a `Write` that really
     lands on disk (announced before the bytes, as a real tool call is), a second `Write`
     that is announced and then *fails* with nothing written, a permission
     prompt, a fixed `PlanArtifact` — through the *same* factory and `SessionBridge` seams the
     real SDK plugs into. Nothing else changes: the session state machine, both
     WebSocket fan-outs and every typed frame stay production code. This is what lets
     layer 4 exercise chat, tool rows, permissions and plan cards with no Claude login
     and no tokens. Off by default; `main.py` logs a structlog warning when it is on.
   - The **fake host backend** (`WORKBENCH_OFFICE_FAKE=1`) is the same idea one layer
     down: the Office host lifecycle, its failure branches and its ownership refusals,
     driven in process with no Office, no windows and no real pid — see *Office host*
     above.
3. Live smoke (`WORKBENCH_LIVE_AGENT=1`): real SDK + machine's Claude login.
4. E2E (Playwright, per milestone — `cd ui && npm run e2e`): `ui/e2e/` drives the
   **built** UI (`vite preview` over `ui/dist`) against a real `workbench-server`
   launched in a per-run temp workspace with fake-agent mode on. Eight journeys: file
   CRUD + save + watcher round-trip + conflict + dirty-close, terminal tabs against real
   ConPTY, QuickBar/shortcuts (including the never-executed rule, and a registered
   tool reaching the user through the registry alone), chat streaming and
   tool settling, plan cards, status chips and the attention badge, office degraded
   mode, and provenance (an agent write is marked, attributed, opened, acknowledged,
   and links back to its session — *and* the negative half: an announced-but-failed
   write followed by the user's own change, and the user's own saves, mark nothing).
   Single worker (one backend, one workspace, one PTY host); no sleeps — journeys
   wait on the app's own signals.
5. **Perf lane** (`cd ui && npm run perf` — `ui/playwright.perf.config.ts`, its own
   config so it can run the backend in its own workspace and keep its own report).
   The same production build against a **generated 5,005-file workspace**
   (`server/tests/perf_fixture.py`: a 12-level deep tree, one flat 2,000-file
   directory, a 5,000-line source file, and a `CACHEDIR.TAG` build cache that must
   stay invisible). Collects `PerformanceNavigationTiming`, paint entries,
   `PerformanceObserver` on `event` (`durationThreshold: 0`), `longtask` and
   `long-animation-frame`, plus a rAF frame sampler for continuous interactions
   (`ui/e2e/perf/instrument.ts`).

   Budgets are **work-shaped wherever possible**: how many directories a request
   lists, how many full tree fetches twenty file changes cost — counts that are the
   same number on a laptop and on a throttled runner, so they can gate a merge. The
   server-side half of that is ordinary pytest (`server/tests/test_perf_budgets.py`,
   which patches `os.scandir`/`Path.iterdir` and counts). Wall-clock budgets exist
   too — cold launch, frame timing — but they carry the `@wallclock` tag and CI
   records them instead of blocking on them. A budget that fails for reasons other
   than the code gets switched off, and a switched-off budget defends nothing.
