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
   the echo; a clean buffer reloads silently; a dirty buffer prompts. Views may
   update *incrementally* from those events — the file tree does — but never
   without a path back to a fresh reading of the disk (see *The file tree*).
2. **Every wire payload is a Pydantic model** (`models/`). WebSocket protocols are
   discriminated unions on `type`. The UI mirrors these in `ui/src/types.ts`.
3. **Routers thin, services own logic.** A router validates, delegates, maps domain
   errors to HTTP codes. Everything interesting is testable without HTTP.
4. **Platform code is quarantined.** The PTY sits behind the `PtyLike` `Protocol` in
   `services/pty_manager.py`, whose `_spawn_backend` picks by `sys.platform`: pywinpty
   (ConPTY) on Windows, `services/pty_posix.py` (stdlib `pty`) elsewhere. Neither
   backend's imports run on the other's platform, and the router never learns which
   one it got.
5. **The Agent SDK is injected.** `services/agent_sessions.py` takes a client factory;
   the real one (`services/sdk_factory.py`) is the only module importing
   `claude_agent_sdk`. Tests script a fake client through the same seam.
6. **The shell holds only what a browser tab cannot.** The UI runs unchanged in
   a Vite tab and in the Tauri window; every native capability goes through
   `ui/src/shell.ts`, which no-ops outside the shell. See below.

## The desktop shell (`desktop/`)

Not packaging polish — a **requirement**. Real Word/Excel/PowerPoint windows are
reparented into panels (`SetParent`), and a browser tab has no HWND to parent
them to. The shell (`desktop/src-tauri/`, Rust + Tauri 2) owns five things:

| Concern | Why it cannot live in the UI |
|---|---|
| The native window | `dragDropEnabled: false` — an OS drag-drop handler over the whole window would fight the native children hosted there from PR 3 on |
| Backend supervision (`backend.rs`) | Nothing in a webview can start or outlive a process |
| Close guard | WebView2 ignores `beforeunload`, so a native close silently discarded dirty buffers |
| Attention badge | `document.title` never reaches a native title bar or the taskbar |
| Caption tint (`caption.rs`) | The frame is drawn by the window manager, outside the document and outside the process — no stylesheet can reach it |

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

**Caption tint** (`caption.rs`, `ui/src/captionTint.ts`). The frame was the one
part of the product that looked like every other Windows program — a stock grey
caption above a graphite app — and it is unreachable from CSS, being drawn by
the window manager in another process entirely. Windows 11 lets an app change
what that frame is painted *with* while leaving it to paint it:
`DWMWA_CAPTION_COLOR` (35), `DWMWA_TEXT_COLOR` (36) and `DWMWA_BORDER_COLOR`
(34). **Tinting rather than replacing is the whole decision.** Dragging,
snapping, double-click-to-maximise, Aero Shake, the snap-layouts flyout, the
system menu and the three window buttons keep working because they are still
the real ones; a custom frame reimplements every one of them (M7 — see
`ROADMAP.md`, and note that *what* such a frame would carry depends on a visual
direction not yet chosen).

Three consequences the design turns on:

- **The colours belong to the UI.** `tokens.css` is the single source of truth
  for colour, so the shell is *told* three resolved tokens — `--surface-app`,
  `--text-secondary`, `--border-strong` — through the same `shell.ts` seam that
  carries the attention badge, on startup and on every theme flip. A hex
  constant in Rust would be a second palette no theme toggle could reach.
- **The window buttons are not text.** `DWMWA_TEXT_COLOR` reaches the title and
  nothing else; DWM draws the minimise/maximise/close glyphs from the window's
  immersive light/dark mode. So the shell derives that flag from the caption's
  own **relative luminance** — from the colour, not from the theme's name, so a
  palette change carries it — and the title/caption pair is held to DESIGN.md
  §7's 4.5:1 by a test that reads `tokens.css` itself
  (`ui/e2e/captionContrast.test.ts`: 8.3:1 dark, 6.5:1 light today).
- **"On startup" has to mean startup.** The UI cannot send a tint until its
  bundle has mounted, so the caption was stock for the first moments of every
  launch — the exact pixel this exists to fix. The last tint is therefore
  cached in the app config dir and re-applied from `RunEvent::Ready`, the first
  instant a window exists. It is a cache of the UI's own value, never a second
  authority: a first-ever run has none, and anything but three parseable
  colours means no pre-tint. `Ready` runs on the event loop, so the *read* is
  on a thread of its own and only the painting is posted back — the config
  directory can be a roaming profile or a network share, and a window that will
  not repaint while one resolves is worse than the flash being removed.

Degrading is silent in every direction. Windows 10 has no attributes 34–36 and
refuses them (`E_INVALIDARG`) — one log line, and the caption stays the
system's; the older immersive-dark-mode flag is still attempted, because a dark
system caption over a dark app beats a light one. `DwmGetWindowAttribute` cannot
read these back (measured: `E_INVALIDARG` on 26200), so the evidence a tint
landed is the set's `HRESULT` plus the shell log, and the evidence it broke
nothing is the window state read back afterwards — style bits, `WM_NCHITTEST`
over the caption, the system menu — which is what `caption.rs`'s window tests
assert against a real window.

**Both hosts, always.** `ui/src/shell.ts` is the only module importing
`@tauri-apps/api`, dynamically and only after `isTauri()` passes, so a browser
build never fetches the chunk and every call is inert in a tab.

## The launch path

What the browser must do before the window is usable is a design surface, not a
build artifact, and it has one rule: **nothing that is not needed to paint may be
statically imported from `main.tsx`.** A module script blocks
`DOMContentLoaded` until it is downloaded, parsed *and evaluated*, so every byte
reachable from the entry is paid on every cold start whether or not the feature
is used.

Monaco is the whole reason the rule is written down. `import * as monaco from
"monaco-editor"` pulls the barrel — the editor plus ~90 basic-language
contributions plus four language services — and `main.tsx` imported it
statically, which made the eager chunk 88% editor and 1,030 kB gzipped, on a
launch where the user may never open a file.

```
main.tsx ──► App ──► EditorArea (panel: tabs, empty state, provenance bar — instant)
                          │
                          └─ React.lazy ──► CodeEditor ──► @monaco-editor/react
                                   ▲                  └──► monacoBundle.ts ──► monaco-editor
                                   │                          (its own chunk + its own CSS)
                          prefetchMonaco() on requestIdleCallback,
                          armed when the workspace tree lands
```

- **`ui/src/monaco.ts` is the seam and imports no Monaco** (`import type` only,
  erased). It holds the pure half — the mono font, the extension→language table,
  the theme name — and memoizes one dynamic `import("./monacoBundle")`. The model
  helpers the store calls (`setModelContent`, `disposeModel`) no-op until the
  editor exists, which is the same answer as "no model for that path" and is what
  the store already handled.
- **Anything read from a CSS token is read when the bundle lands, never when the
  load is armed.** Loading late puts a window between "the prefetch is scheduled"
  and "Monaco exists" in which `defineWorkbenchTheme` can do nothing — so a theme
  toggle inside it is dropped, and a `Theme` captured at arming time is stale by
  the time it is used while the tokens it reads are live. The two then describe
  different themes, and the name `<Editor theme=…>` asks for is never registered:
  Monaco substitutes a built-in theme for a name it does not know, silently, so
  the first file opens in neither palette. Hence `loadMonaco()` takes no theme and
  calls `documentTheme()` (`ui/src/theme.ts`) inside its `.then()` — the DOM
  attribute, because `setTheme` flips it before it sets the store and the tokens
  follow the attribute. Reproduced in `ui/e2e/theme.spec.ts`, pinned in
  `ui/src/monacoLoad.test.ts`.
- **The split is inside the panel, not at the registry.** The editor panel is in
  the startup layout, so lazy-loading its registered `panel.component` would defer
  nothing and would hand dockview a component that suspends with no boundary. The
  chrome renders at once; only the Monaco surface (`panels/CodeEditor.tsx`) is
  behind `React.lazy` + `Suspense`. The registry needed no change to allow it.
- **The bundle is hand-picked, and the two lists are one list**
  (`ui/src/monacoBundle.ts`): `edcore.main` for the editor with all its
  contributions, plus exactly the language grammars `languageForPath` can return.
  A grammar not in the extension table is bytes nobody can ask for; an extension
  with no grammar opens as plain text with no error anywhere, which is why
  `monaco.test.ts` checks both directions. JSON is the only language *service*
  kept — it has no basic-language grammar, so its service *is* its highlighting.
- **The prefetch is armed by the tree, not by mounting.** Between first paint and
  the workspace listing arriving the main thread is idle, so an unconditional
  `requestIdleCallback` fires straight into the launch it was meant to stay out
  of. It is also given no `timeout`: an idle callback that fires anyway is the
  long task this change exists to remove, in a new place.
- **`main.tsx` renders immediately.** It used to await `window.load` so surfaces
  computing colors from CSS tokens could not read them early — a wait on the last
  webfont to protect a read that was never at risk, since script execution already
  waits for the stylesheets above it and both surfaces are built later anyway. The
  layout-shift ceiling in `ui/e2e/perf/launch.spec.ts` is what keeps the fonts
  arriving late from becoming a reflow; the fix if it is ever breached is
  `font-display`/`size-adjust`, not the gate.

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
| `panel.instances` | What a *second* pane of a plural tool is bound to — the rows the pane picker offers, and what such a pane calls itself. See "Panes" below |
| `onDockReady` | The live `DockviewApi`, for a tool that operates on the dock rather than living in it. Two do: the layout system and the pane system |
| `groupActions` | One control at the right end of every pane's tab strip, for a tool that acts on panes rather than living in one. The split affordance is the only one, and it is why `App.tsx` can mount it without knowing what it is |
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

**The registry is also what the app knows about itself.** Because every command,
chord, panel and status item is declared rather than written down, "what can this
app do" is a *derivation* and not a document: `ui/src/keyref.ts` groups every
command under the tool that owns it (`commandOwners`, which covers the dynamic
sets too) and `ui/src/panels/Keyboard.tsx` renders that as the welcome card and
the keyboard reference (DESIGN.md §6.12). Tooltips that name a chord ask the same
registry for it rather than spelling it out. This is the difference between a
discovery surface and a cheat sheet: a cheat sheet is wrong the day a tool moves
a chord and nothing says so, while `ui/src/keyref.test.ts` fails the build when a
registered command is unreachable from the surface, or when the chord it shows is
not the chord that runs.

## Panes

The window is tiled, not fixed: any pane splits in two, anything registered goes
in the new one, and there may be several panes of the same tool — four agent
sessions, two shells, two files. The system is one capability
(`ui/src/panels/Panes.tsx`, with the pure half in `ui/src/panes.ts`) that
contributes no panel of its own.

**A pane's identity is its dockview panel id, and that is the whole design.**
The id is `toolId` or `toolId#instanceKey`, split on the first `#`. dockview
serializes panel ids into `.workbench/layouts.json` and nothing else about a
panel's contents, so the id *is* the persistence: whatever a pane is bound to has
to be expressible in that string or it does not come back. `agent#<session_id>`
restores that conversation, `editors#<workspace-relative path>` that file,
`terminal#<n>` that pane and its number (with a fresh shell — the PTY dies with
the socket and the server releases it, which no layout file can undo). There is
no second store, no id map and no migration, and an instance key is therefore a
**contract** exactly as a tool id is.

`pruneLayout` vets pane ids as well as components: a pane carrying an instance
key for a tool that is a singleton *today*, or one whose id and
`contentComponent` disagree, is unaddressable by every pane command and is
dropped with its own message. It deliberately does not vet the key itself —
sessions and files load long after the layout does, so a pane bound to something
that has not arrived yet must not be dropped for being early; the panel says so
itself instead.

Two seams keep the shell capability-free. The picker is the **QuickBar in pick
mode**: a capability hands `store.ts` a list of rows, so there is one overlay
language in the app and `QuickBar.tsx` still names no capability. The split
affordance reaches the tab strip through `groupActions`, so `App.tsx` mounts a
component without knowing what it draws.

Focus is the selector: the focused pane is the session `Enter` and *Interrupt*
mean, the file `Ctrl+S` saves, and the shell a `shortcuts.md` `shell` entry types
into. That is why `Chat.tsx` could be mounted four times without a change — each
pane makes its own session the active one when it takes focus.

**A restored pane is a claim, and it acquires nothing until the claim is
checked.** A layout outlives the resources it names — `SessionManager` holds its
sessions in memory, so every `agent#<id>` in a saved layout is unknown to the
next server process — and the pane that renders "this session is not running any
more" must not be opening a socket behind that note. So the Agent pane waits for
the session to appear *live* in the listing before it attaches, the discipline
`openSession`/`openLiveSession` already followed, and `ReconnectingSocket`
(`ui/src/ws.ts`) stops retrying when the server answers a close code in the
4400s — a refusal, as against the dropped connection it exists to survive. The
same rule in the other direction: a ceiling the tool can know *before* the
gesture belongs on the picker row, so `New agent session` reads its cap from
`GET /api/agents/limits` and greys itself with the number and the setting rather
than spending a split on a round trip the server refuses.

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
| `models/` | REST/WS schemas: files, terminal, agents, plans, visuals, shortcuts, provenance, activity, layouts, office host, usage, worktrees, orchestrator |
| `routers/files.py` | dir listing/tree/read/write/create/rename/delete; jail + conflict mapping |
| `routers/terminal.py` | `/ws/terminal` bridge |
| `routers/events.py` | `/ws/events` fan-out (file changes + session status) |
| `routers/agents.py` | session REST + `/ws/agent/{id}` |
| `routers/conversations.py` | `GET /api/conversations` (Claude Code's whole transcript store, grouped by folder) |
| `routers/shortcuts.py` | `GET /api/shortcuts` (merged shortcuts.md state) |
| `routers/provenance.py` | `GET /api/provenance` + acknowledge |
| `routers/activity.py` | `GET /api/activity` (the fleet's live tool calls) |
| `routers/orchestrator.py` | `GET /api/orchestrator` (crews + budget), stop an orchestrator's crew |
| `routers/usage.py` | `GET /api/usage` (the account's plan limits, as last reported) |
| `routers/layouts.py` | `GET`/`PUT /api/layouts` (this workspace's saved arrangements) |
| `routers/workspaces.py` | `GET /api/workspace`, `POST /api/workspace/switch` (the root, and the recent list) |
| `routers/sessions.py` | `GET/POST/PUT/DELETE /api/sessions`: named detachable working sessions (M5 item 15), queried by workspace |
| `routers/worktrees.py` | list/acquire/release/renew/prune the managed worktree pool |
| `routers/office_host.py` | open/list/move/detach/close a hosted document; `GET /api/office/capabilities` |
| `services/workspace.py` | path jail (root is mutable — see below), atomic writes, hashing, `list_dir` (one listing), `top_level_dirs`, `tree` (the search index's walk) |
| `services/workspaces.py` | switching the root at runtime: validation, the one list of everything that re-roots, the recent list |
| `services/app_data.py` | where machine-local state lives (`%LOCALAPPDATA%\Workbench`) — the pool root, the recent workspaces (`workspaces.json`) and the named sessions (`sessions.json`), never a `.workbench/` folder |
| `services/watcher.py` | watchfiles -> bus |
| `services/ignore.py` | what the tree and watcher skip: noise names, plus `CACHEDIR.TAG` build caches |
| `services/event_bus.py` | in-process pub/sub |
| `services/pty_manager.py` | PTY sessions: the `PtyLike` seam, the per-platform factory, ConPTY (Windows) |
| `services/pty_posix.py` | the POSIX PTY backend — stdlib `pty.fork`, behind an injectable syscall surface |
| `services/terminal_stream.py` | batching PTY reads into WebSocket frames (below) |
| `services/agent_sessions.py` | session state machines, streaming, permissions, plan artifacts |
| `services/session_index.py` | per-folder history from Claude Code's storage; the one transcript line parser |
| `services/conversations.py` | the whole store, browsable: enumerate + mtime-cached reads, lossy-key resolution, the workspace jail |
| `services/agent_tools.py` | the agent-facing tool registry + its ergonomics budget |
| `services/sdk_factory.py` | real SDK client + context-bridge MCP server |
| `services/skills_bundle.py` | locates `skills_bundle/`, the bundled skills plugin shipped as package data |
| `services/shortcuts.py` | shortcuts.md parser + merge + live reload |
| `services/layouts.py` | `.workbench/layouts.json`: atomic write, and a read that never raises |
| `services/sessions.py` | `<app data>/sessions.json`: named working sessions, CRUD, version-stamped, atomic write, a read that never raises; GLOBAL (queried by workspace, not re-rooted) |
| `services/worktrees.py` | the managed worktree pool: borrowed detached checkouts, leases, dirty protection |
| `services/provenance.py` | correlates agent tool calls with watcher events; who changed a file |
| `services/activity.py` | the same tool calls one moment earlier: what every session is touching right now; bounded, coalesced, in-memory only |
| `services/usage.py` | plan limits from the SDK's rate-limit events; per-turn and per-session cost; in-memory only |
| `services/orchestrator.py` | Mission Control's crews: spawn/read/send/stop workers in pooled worktrees, the budget that binds, the reaping |
| `services/office_host/` | hosting real Office windows: `backend.py` (the Protocol the native implementation must satisfy), `fake_backend.py` (in-process stand-in), `state.py` (the lifecycle), `service.py` (hosts by id, events, reaping) |

## The file tree

Three properties, and each one is a budget in the perf lane rather than a claim
here: it reads **one directory at a time**, it **patches itself** from the
events it already receives, and it **renders only what is on screen**.

```
GET /api/files/dir?path=…   one os.scandir  ──►  dirs[path] = DirEntry[]
/ws/events file_changed     one row edit    ──►  applyFileChange(dirs, event)
                                                       │
                              visibleRows(dirs, expanded) → flat row array
                                                       │
                              slice(first, last) → ~40 <button>s in the DOM
```

**Lazy, per directory.** `list_dir` is one `os.scandir` and returns childless
`DirEntry` rows, so the cost of a listing is the size of *that* folder and
nothing else — the root of the 5,005-file fixture and its 2,000-file directory
are both exactly one listing. Expanding a folder always re-reads it, which is
what makes an incrementally patched tree self-healing: every folder the user
opens is verified against disk at the moment they open it. `GET /api/files/tree`
— the full recursive walk — still exists, but it is now only the **search
index** behind the QuickBar's fuzzy find and the chat's tool-row file links, and
the UI fetches it *on demand*: when the QuickBar opens, or when an agent
announces a tool call. Nothing fetches it at launch and nothing fetches it on a
file change.

**Delta updates, client-side.** A `file_changed` event names a path; the only
listing that can have changed is its parent's, and the client already holds
that. So the tree is patched in place (`ui/src/filetree.ts`, pure and
unit-tested) instead of the server being asked to walk the workspace again.
Twenty file changes used to cost twenty full walks and 9.4 MB of JSON; they now
cost **zero requests** (`ui/e2e/perf/watcher.spec.ts`, which was shipped as an
xfail in PR #35 for exactly this PR to convert). The alternative — a server-side
index emitting deltas — was rejected: it is a second authority on what is on
disk, it has to be invalidated by these same events anyway, and principle 1
above says the answer to "what exists" is a listing, not a cache.

**Disk stays the source of truth**, so the incremental path is bounded by three
convergence rules rather than trusted forever:

- **Expanding re-lists.** The one interaction that reveals rows also refetches
  them. Anything a patch got wrong is corrected by the user's own next click.
- **Reconnect re-lists.** Every (re)connect of `/ws/events` re-reads the root
  and every expanded folder — one `scandir` per directory *on screen*, not per
  directory that exists — which covers everything missed while offline.
- **`tree_invalidated`.** A `CACHEDIR.TAG` appearing makes a whole subtree
  vanish from what the tree may show, and no per-file event can say so: the
  events from inside it are precisely the ones now being suppressed. The watcher
  publishes this frame when it invalidates its own ignore memo, and the client
  reconciles. Directories are also reported as `added` now (they never were),
  because otherwise a folder created by `mkdir`, an agent or a build stayed
  invisible — and so did every file created inside it, since the client had no
  row to hang them on.

**Virtualised, without a dependency.** A tree with everything collapsed is a
list: flatten the expanded directories into an array of rows once, and rendering
becomes indexing. A spacer holds the full scroll height; the rows the scroller
can show, plus 8 of overscan at each end, are absolutely positioned by index.
Expanding the 2,000-child directory used to take the DOM from 10 rows to 2,010
in one commit — a single 400–500 ms main-thread task, the only interaction the
audit found that visibly froze the window. It now mounts ~40 buttons whatever
the folder holds. The arithmetic depends on two design tokens (`--row-height`,
`--space-2`), so their agreement with the constants in `FileTree.tsx` is its own
test (`ui/e2e/perf/rowGeometry.test.ts`) — a token edit that misaligned every
row below the fold would otherwise be invisible to a suite that counts rows.

Virtualisation ended "Tab through every row", so the panel implements the
**WAI-ARIA tree keyboard model** instead: roving tabindex, Up/Down to move,
Right to open or descend, Left to close or step out, Home/End, and the row's own
`<button>` for Enter/Space. `aria-level`/`aria-posinset`/`aria-setsize` are
explicit, because the DOM no longer holds the rows a screen reader would count.

**One rule about what exists.** `services/ignore.py` is consulted by the
listing, the walk and the watcher, and the client's sorted insert restates the
server's order (`kind`, then lowercased name) so a row that arrives from an
event lands where a refetch would have put it. That last one is not theoretical:
the panel used to re-sort with `localeCompare`, which under the author's own
`nb-NO` locale collates "Aaa" after "deep" — the tree and the server disagreed
about order for anyone whose locale has its own alphabet.

## Terminal throughput

ConPTY is the floor and it is not ours: PowerShell writes line-at-a-time, so a
1.48 MB flood arrives as ~20,000 reads of ~73 chars at ~620/s, and a raw
pywinpty loop with no Workbench code in it measures the same shape and the same
~30 s. What *was* ours is that the pump sent one Pydantic model, one
`model_dump_json()` and one `send_text()` per read — 18,827 frames, mean 79
chars. `services/terminal_stream.py` batches them: **the first chunk after a
quiet stream goes out immediately** (a keystroke echoed at an idle prompt never
waits on a timer — that is the whole interactive feel), a busy stream emits at
most one frame per window, unbroken output widens that window, and a size cap
keeps a fast producer streaming rather than buffering. Measured over the real
socket: 1,347 frames at mean 1,100 chars, with echo latency unchanged
(median 0.5 ms). The policy is a clock-free object so the measured ConPTY
arrival trace can be replayed in virtual time and the frame budget asserted
deterministically (`test_terminal.py`).

The in-flight read is a task that is never cancelled by the window timer; a
plain `wait_for` would drop the chunk ConPTY had already handed over. And the
policy runs on `time.perf_counter`, not `loop.time()`: on CPython 3.11 the
latter is Windows' tick counter (15.6 ms granularity), which made the idle test
fire at random — and asyncio arms its own timers off it, so a wake-up is only a
hint and `perf_counter` decides whether the frame is really due.

On the client, `ui/src/terminalRenderer.ts` puts xterm on the GPU
(`@xterm/addon-webgl`) with a fallback to the default DOM renderer on every
failure path — the context refusing to be created, the lazy chunk failing to
arrive, and the context being lost mid-session. A dead canvas is worse than a
slow one, so the last of those is forced for real in `e2e/terminal.spec.ts`
(`WEBGL_lose_context` on the live canvas) and asserted on the rendered rows
rather than on xterm's buffer, which keeps filling whether or not anything is
drawing.

The addon is **imported dynamically**, and that is a deliberate seam: panels are
all statically imported, so a static addon import lands in the entry chunk and
every user pays for it on first paint. Built both ways, that is +102 kB raw /
+25.9 kB gzip — a real bill, for a renderer this PR measured as neutral on the
hardware available. Loading it on demand puts it in its own 26 kB gzip chunk
that only the first terminal to open fetches, leaving the entry chunk within
0.3 kB gzip of never having taken the dependency. Consequence for tests: under
the GPU renderer there is no `.xterm-rows` to scrape, so the E2E suite reads
xterm's buffer through a reader `panels/Terminal.tsx` hangs on the host element
(wrapped lines rejoined — strictly better than the old `textContent`), and the
renderer assertions poll, because the swap now happens a tick after open.

## Agent sessions

Each live session wraps one `ClaudeSDKClient` with `cwd` bound to a workspace folder.
Streaming uses `include_partial_messages`; the session translates SDK messages into
typed events (text deltas, tool-use notes, status, turn-done) consumed by any number
of WebSocket listeners. Permissions: file tools inside the folder are auto-allowed;
everything else (shell, web) raises a `PermissionRequest` event and blocks on an
asyncio future until the UI answers (10-minute timeout -> deny).

That promise is kept by **two** mechanisms, because one was not enough.
`can_use_tool` is only reached for calls the CLI resolves to *ask* — anything
already resolved to *allow*, whether by `allowed_tools`, by `permission_mode` or
by a `permissions.allow` rule in a workspace settings file, never reaches the
callback. The server used to run with `permission_mode="acceptEdits"`, and that
mode allows a `Bash` call outright when its base command is one of
`mkdir touch rm rmdir mv cp sed`: measured against the bundled CLI, `mkdir` in
the workspace executed with no card, no provenance claim and no activity row.
The mode is now `default`, and the shell launchers (`Bash`, and `PowerShell`,
which the CLI substitutes on Windows) are additionally brokered by a `PreToolUse`
hook — `services/permission_broker.py`. A hook is dispatched for every tool call
and its deny binds even in permissive modes, so a mode a subagent inherited or an
allow rule in a folder the user did not write cannot walk around it. The hook
resolves the *same* `ask_permission` future the chat card and the board answer,
and its explicit allow suppresses the callback, so a brokered request still costs
exactly one prompt — both directions are driven through the real bundled CLI by
`TestTheEndToEndRepro` in `server/tests/test_permission_broker.py`
(`WORKBENCH_PERMISSION_REPRO=1`), which counts the answers a single shell call
costs. The context-bridge
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
lists, questions, markdown and *visuals* (`models/plans.py`), never free-form markup —
which the UI renders as a native card; the user's choices, annotations (each carrying
an *anchor* — see below — so a note can point at one cell rather than a whole node) and
verdict come back to the agent as a typed `PlanResponse` through the same
future-and-timeout discipline as permissions. A timeout or an interrupt resolves to verdict
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

**The scene graph** (`models/visuals.py`, `ui/src/visual/`) is the fifth node kind,
and the answer to "let the agent draw" without letting it ship markup. A `visual`
node is a typed, **depth-2, non-recursive** graph: blocks (`single`/`row`/`grid`/
`split`) holding *leaves* — `table` (typed columns, so a numeric column renders in
tabular figures because the schema says it is numbers), `chart` (typed series on
typed axes), `diagram` (nodes and edges only), `code_diff`, `metrics`. The model
sends structure and numbers; Workbench computes every coordinate (`visual/layout.ts`,
`visual/timeAxis.ts`) and emits every pixel from its own React/SVG components in
`tokens.css` colours. Two domain types are first-class because a generic renderer
lies about them: a **time axis** that is a regular grid in *absolute* time labelled
in a market's IANA zone — so a 23-hour and a 25-hour day draw and label correctly,
asserted on real Nordic clock-change dates in both `test_visuals.py` and
`timeAxis.test.ts` — and **`step`**, because a dispatch schedule holds its value
across a settlement period and a line between hour centres claims ramps that never
happened.

*Threat model.* The rejected alternative was a third-party artifact bridge that
failed vetting on undisclosed telemetry, an unpinned `npx` with sandbox-evasion
fallbacks, and — worst — a channel letting a script inside a model-authored
artifact write into the agent's instruction channel with no user gesture. The
scene graph closes all three by construction rather than by policy:

- **Nothing is executable.** There is no HTML, SVG, CSS, script or event-handler
  field, and every payload string becomes a React *text node* — a cell reading
  `<script>…` renders those characters (`Visual.test.tsx`, and the E2E journey
  asserts nothing was added to the document).
- **Nothing reaches the network.** No URL, no image source, no font, no fetch. The
  E2E journey records every request the page makes while an artifact renders and
  asserts the list is **empty**.
- **Nothing addresses the agent.** The only channel back is the existing typed
  `PlanResponse` — the user's own choices, notes and verdict.
- **Nothing is unbounded.** Rows, columns, series, points, nodes, edges, diff
  lines, leaves per node and visual nodes per card are all capped in the schema, so
  a runaway artifact is a tool error the agent can fix, not a wedged renderer.
  Those caps bound one leaf each, and their *product* is the number that decides
  whether a card renders: eight leaves inside the chart cap is 19,200 marks, and a
  card holds three such nodes. So a visual node also carries an aggregate cap
  (`MAX_VISUAL_MARKS`), and both halves are measured rather than argued —
  `test_visuals.py::TestRenderBudget` builds the payload at every cap at once and
  watches it be rejected, and `visual/budget.test.tsx` renders a whole card at the
  ceiling (18,000 marks → 20,619 elements, ~0.2 s) to prove the ceiling is one the
  renderer can draw.
- **Nothing recurses.** Depth stops at the leaf, which is what keeps rendering cost
  bounded in the payload *and* keeps `plan_input_schema()`'s ref-inlining
  terminating. It is also a token budget: the leaf union is inlined once, and
  `AgentToolSpec.max_schema_bytes` fails the gate if that stops being true.

A safety property asserted only in prose is a property until someone edits the
file, so each bullet above has a test named after it.

**Annotation anchors** (`models/plans.py`, `services/plan_anchors.py`,
`ui/src/plan/anchors.ts`) are what make a drawn card answerable at the
resolution it is read at. A `PlanAnnotation` is `{anchor, text}`, and the anchor
has four kinds: the **plan**, a whole **node** (what every annotation used to
be, and still the fallback), a **part** of a drawn leaf, or a **range** of
characters in a `markdown`/`question` text.

*An anchor is a semantic path the renderer emits, never a CSS selector.* That is
the design decision, and it is worth stating as a rejection, because recording
what the user clicked — an element id, a selector, a bounding box — is the
obvious implementation and is wrong three times over. A selector is our
*stylesheet* leaking into the agent's input: it breaks when a class is renamed,
it is meaningless to a model reading it, and it addresses the page rather than
the payload. `["leaf", 2, "row", 14, "col", "Price"]` names **data** — it
survives a re-render, a restyle and a theme change (`Visual.test.tsx` asserts
the same cell emits the same path across a state change *and* across a
highlight landing on that very cell); it is directly actionable, because it is
the agent's own payload addressed in the agent's own vocabulary; and it is
**validated against the artifact before it travels**, so an anchor naming a row
that does not exist is a malformed decision the session refuses (and says so
over `agent_error`), not a note about nothing. Positional where the payload is
ordered (leaves, rows, points, edges), named where the payload carries a name (a
column label, a series label, a diagram node id) — and a name that is empty or
ambiguous falls back to the position, because an address resolving to two things
is not an address. The grammar is validated in the model (pairs, a closed set of
keys, a non-negative index where an index belongs — a `selector` key is not a
key), the *target* in the service, which is the only half that needs the plan.

**Annotate mode** is a per-card mode (`Alt+A`, or the QuickBar's "Annotate the
plan"), contributed through the Agent tool's descriptor rather than as a tool of
its own — a plan card has no panel, no status item and no resource, so a second
registry entry would be a second name for the Agent. In the mode every
anchorable part becomes a real `<button>` (inside SVG, a `<g role="button">`
answering Enter and Space), so the whole mode is keyboard-operable, which
DESIGN.md §7 makes a requirement rather than a nicety; a chart's points are
reached in two steps (series, then a point strip labelled in the market's own
clock) because six series of four hundred points may not be 2,400 tab stops.
Cost is deliberate: outside the mode a part with no note renders exactly what it
rendered before anchors existed — no wrapper, no attribute, no handler — so the
18,000-mark render budget is untouched. Notes accumulate on the card and travel
**with the verdict, in the one `PlanResponse`**; sending notes without deciding
would be a second channel to the agent and is deliberately not built here (M5
item 3, PR 5).

Session history is not ours: Claude Code and the SDK persist transcripts under
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. We read that storage
(`session_index.py`), so CLI sessions and Workbench sessions share one history,
grouped per folder. **Read-only, and browsable whole** — `services/conversations.py`
walks the entire store rather than the one folder a session is bound to, which is
what the Conversations panel renders (M5 item 12, half A).

Three properties make that safe to open on a whim, and each is a constraint rather
than a nicety. **Nothing writes.** A test watches every byte and mtime under the store
across a browse and fails on any change; there is no delete path at all. **The cheap
pass and the expensive one are separate.** Enumeration is one `os.scandir` per project
directory — 17 directories and 80 transcripts measured at 1.8 ms — and yields
everything ordering and counting need; reading a transcript for its title and turn
count is a full pass (1.28 s for all 80, 398 MB, cold) and is therefore both bounded by
`?limit=` (the newest N are *read*; every conversation is still listed, with the unread
ones carrying `read: false` — a limit that dropped rows would drop whole folders) and
cached against each file's `(mtime_ns, size)`, so a second browse of an unchanged store
is enumeration only (93 ms including folder resolution). Append-only transcripts are
what make that pair a sound invalidation. The scan runs in a thread; the router does
nothing else. **What cannot be known is named.** `encode_project_dir` is lossy and not
reversible (`C:\a\b` and `C:/a-b` collide), so a display path is *matched* against
directories that exist — descending from the workspace, then home, then the filesystem
anchors, pruned by the file tree's own ignore rules — and the raw encoded key is shown
whenever nothing matches, never a reconstructed path. A transcript that will not parse
keeps its row and carries the reason.

Half A opens only what is inside the workspace jail. A conversation from a folder
outside it is listed with its reason on screen rather than hidden — opening one needs
the multi-root roots (M5 item 5/6), and until then knowing the conversation exists is
most of what the browser is for.

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

## Live agent activity

**Provenance answers "who wrote this file", after the fact and conservatively;
this answers "what is the fleet doing this second", across every session at
once — the same signal, one moment earlier, with a different promise.** They are
built to stay separate: provenance may never be wrong, so it stays silent
without an exact match; activity may be incomplete (a window that forgets), so
it is allowed to be cheap. Neither consumes the other.

`services/activity.py` + `models/activity.py` + `GET /api/activity`, rendered by
a registered tool (`ui/src/panels/ActivityPanel.tsx` + `ui/src/activity.ts`) —
one panel, one status reading, one QuickBar command, one line in `tools.ts`.

**The signal already existed; only its reach is new.** A session announces a tool
call (`ToolUseNote`) and settles it (`ToolSettled`), and both go down
`/ws/agent/{id}` — a socket only the windows that opened *that* conversation
have. `AgentSession` now offers the same two moments to an `ActivityObserver`
(the third observer on that seam, after `ToolUseObserver` and `UsageObserver`),
which republishes a bounded row as a `SessionActivityEvent` on the shared bus,
following the `SessionStatusEvent` precedent. So a window with no socket for a
session still sees it working — which is the whole feature.

Four properties, each with a rendering rather than a footnote:

1. **Bounded by construction, and it says by how much.**
   `MAX_ENTRIES_PER_SESSION` (8) per session, `MAX_SESSIONS` (16) sessions,
   both served in the snapshot so the panel can state the caps it is showing
   under. Everything evicted is *counted* (`dropped`, `dropped_sessions`) and
   rendered — a view that silently forgets is not one you can read a number off.
   A **running** call is the last thing a full window gives up (eviction takes
   the oldest *settled* entry first), because "what is this agent doing now" is
   the headline and eight quick calls must not blank it.
2. **Coalesced, on the policy `terminal_stream.py` proves.** The first change
   after a quiet fleet publishes immediately; after that, at most one frame per
   `ACTIVITY_WINDOW_S` (250 ms), with every change in between folded into it and
   only the latest state per session surviving. 250 ms rather than the
   terminal's 8 ms because a person reads this. Measured (2026-08-06): a 40-call
   `tool storm` is 80 changes and reaches the socket as **2 frames totalling
   1,656 bytes** — asserted in `test_activity.py` and again in the browser
   (`ui/e2e/perf/activity.spec.ts`: 4 frames end to end, 0 of 10 sampled frames
   over 50 ms), because "a burst must not become a re-render storm" is a count,
   not a claim. The client half of that promise is a **memo boundary on the
   row**: a frame replaces the whole snapshot object, so `ActivityBody` re-runs
   on every one of them, and without the boundary one busy session would
   re-render every *other* session's card four times a second — the storm
   arriving one layer after the socket that was coalesced to prevent it. It
   works because `mergeSessions` keeps the identity of the rows a frame did not
   carry; both halves are pinned by tests, since either one alone is inert.
3. **Jailed, and no results.** This feed is wider than the socket its frames came
   from, so every path argument is normalized workspace-relative with the same
   function provenance uses, and one that escapes the workspace is redacted
   rather than printed (`Read: (outside the workspace)`). Summaries are rebuilt
   here rather than passed through from the chat frame, which carries the raw
   argument. Tool *results* never cross at all — only `ok`.
   One deliberate widening, added with Mission Control: `extra_roots` is a list
   of **server-owned** directories the feed may *name* — today exactly one, the
   worktree pool root, so a worker reads `Read: slot-01/server/main.py` instead
   of a row of `(outside the workspace)`. The jail itself is unchanged: the
   clickable `target` stays workspace-only, and a path in neither the workspace
   nor a named root is still redacted. **Naming and opening are separate
   answers**, and only the first one widened.
4. **Idle is a designed state.** A session is a row from the moment it is
   created, so an idle fleet reads "three sessions open, none touching anything"
   rather than as an empty panel, and the status-bar reading renders *nothing*
   when nothing is in flight (§6.7: counts hide at zero). That reading is
   deliberately not the Agent tool's working count next to it: this one counts
   sessions with a **tool call in flight**, and names what they are on.

State is **in memory only**, deliberately, on the same reasoning as the usage
meters: live state about processes, not workspace data. Nothing is written to
`.workbench/`, and a restart reports an empty fleet — which is the truth.

## Plan usage

Your Claude plan's own limits, inside the app (`services/usage.py`,
`models/usage.py`, `routers/usage.py`, `ui/src/usage.ts`,
`ui/src/panels/UsagePanel.tsx`).

**The source, verified against the installed SDK** (claude-agent-sdk 0.2.129,
bundled CLI 2.1.221), not assumed: the CLI emits a `rate_limit_event` frame
which the SDK surfaces as `RateLimitEvent`, a member of the `Message` union — so
it arrives on the same `receive_response()` stream as assistant text and tool
results. It carries one `RateLimitInfo`: `status`
(`allowed`/`allowed_warning`/`rejected`), `rate_limit_type`
(`five_hour`/`seven_day`/`seven_day_opus`/`seven_day_sonnet`/`overage`, or
`None`), `utilization` (0.0–1.0), `resets_at`, the three `overage_*` fields, and
`raw`. `AgentSession._handle_sdk_message` translates it at the same seam it
translates every other SDK message and hands it to `UsageService`, which
publishes a whole `UsageSnapshot` on the shared bus (`usage` on `/ws/events`);
`GET /api/usage` serves the same snapshot for initial load and reconnect.

**One event describes one window.** This is the shape that decides the design:
the five-hour figure and the weekly figures arrive as *separate* events, each
when that window transitions. The snapshot is therefore accumulated a window at
a time, and a window nobody has transitioned in is absent.

**Four caveats, each with a rendering rather than a footnote:**

1. **Stale until you talk to an agent.** The figures ride a live session's
   stream, so they are as old as your last turn. Every bucket carries
   `observed_at`, the snapshot carries a server-measured `age_s`, the panel
   stamps both ("Updated 4m ago", "2h old" per meter), and past
   `STALE_AFTER_S` (15 min) it says outright that the numbers are old. Age is
   server age **plus local elapsed time** — never `Date.now() - observed_at`,
   which would report clock skew as staleness.
2. **It fires on transition, not on demand.** There is no query API: no `claude
   usage` subcommand on the bundled CLI, `/usage` is TUI-only, and nothing in
   the SDK reads current utilization. Hence no refresh button — the panel
   explains the source instead of implying one would work.
3. **An account may never emit it.** `buckets == []` is a first-class state with
   its own designed surface, and the fallback is `UsageSessionCost` — what this
   *process* has spent, from `ResultMessage.total_cost_usd`/`model_usage` —
   labelled "Session cost — not plan usage", because it answers a different
   question. The status-bar reading renders nothing at all in this state (§6.7:
   counts hide at zero); the QuickBar command is how the panel stays reachable.
4. **We report the buckets we are given.** No synthesized per-model weekly, no
   extrapolated burn rate. A missing `utilization` renders as an em dash, never
   as a zeroed bar; an event that named no window is reported as `unspecified`
   rather than guessed at; an unrecognized `rate_limit_type` is logged and lands
   there too, rather than being dropped. The only judgement on this side is the
   display threshold at which a bar starts looking alarming (`WARN_AT` 75%,
   `CRITICAL_AT` 90%) — and it defers to the SDK's own `status` first.

State is **in memory only**, deliberately: this is live state about an
*account*, not workspace data, so nothing is written to `.workbench/` and a
restart reports "not known yet" — the same honest state as an account that never
emits. Non-finite figures are dropped on the way in (NaN is not JSON, and one
would fail the whole `/ws/events` fan-out), and the per-model cost map is
bounded (`MAX_MODELS`).

**The log counts as disk.** Writing no file of our own is only half of "in
memory only": the desktop shell runs the backend as a child process and copies
its stdout into `shell.log`, appended across restarts (`pump()` and `open_log()`
in `desktop/src-tauri/src/backend.rs`), and structlog's default factory prints to
stdout. Anything logged at or above `Settings.log_level` — `info` by default — is
therefore on a packaged user's disk. Utilization and reset times are logged at
`debug` only; the regression test asserts that at fd 1, which is the stream the
shell actually reads.

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
   REST                       state machine        ├── FakeHostBackend  (CI, no Office)
   /ws/events ◄── OfficeHostEvent on the bus       └── ShellHostBackend (the real one)
                                                        │
                                     office_com.py ◄────┤ the process: COM launch,
                                     (COM + Job Object)  │ ownership, poll, reap
                                                        │
                                   shell_channel.py ◄───┘ the window: embed, move,
                                    (/ws/office-host)      hide, detach, release
                                            │
                          ui/src/officeHost.ts ──► Tauri IPC ──► host/ (Rust)
```

**The real backend is split down the middle, and the split is the honest one.**
The *process* is the server's: it launched Word, it holds the pid and the Job
Object, and it is the thing that has to reap on shutdown. The *window* is the
shell's, because `SetParent` has to run on the thread that owns the Tauri
window — in another process. A `#[tauri::command]` can only be called from the
page, so the server pushes typed `HostCommand` frames down `/ws/office-host`
and the webview turns each one into a Tauri call and acks it. The page is a
courier, never a decision-maker: it never decides *whether* to embed, which is
what keeps one authority over a window that outlives any given page load (a
reload drops the socket and finds everything still docked).

**Units cross exactly once.** Every rectangle on the wire is physical pixels
(`PanelRect`); the Rust commands take CSS pixels and multiply by the window's
own scale factor. `ui/src/officeHost.ts` divides by `devicePixelRatio` on the
way through — the same number, so the two cancel and the physical rectangle the
server asked for is the one that lands. Sizes are derived from rounded edges on
both sides, so two adjacent panels cannot leave a one-pixel seam.

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

**Starting Word is the dangerous part, and the order below is why it is safe.**
`DispatchEx` yields a *private* instance (a pid that did not exist), and a new
frame window appears about 0.8 s later — measured — **before any document is
opened**, and it is the same `HWND` `doc.ActiveWindow.Hwnd` reports afterwards.
So the process is identified and put in its Job Object *first*, and only then is
`Documents.Open` called. That matters because Office stops to ask questions:
"the last time you opened this, it caused a serious error" blocked an open for
three minutes during this PR's testing, and a document another instance already
has open blocks it indefinitely. Both would wedge the single COM apartment
thread; instead the launch has a 30 s ceiling and the instance behind it can be
ended from another thread (`office_com.abandon`) because the job was taken
before the risky call. The "already open elsewhere" question is answered before
the open, from the **Running Object Table** — stale-proof, unlike the `~$` owner
file, which survives a crash and (measured) cannot be told from a live one by
opening it.

**Ownership is checked twice over**: the frame we host must be a window that did
not exist, belonging to a process that did not exist. One new window can still
belong to an old process — a single Word owns several frames — and reparenting
one of those would take over the user's session.

**And "new" is never allowed to be ambiguous.** A second, genuinely new instance
can appear *during* a launch — the user double-clicks a `.docx` at the wrong
second — and then two frames are new behind two pids that were not running
before, so "the new one" no longer names a single window. Picking either is a
coin toss whose losing side puts the user's own window in a Job Object with
`KILL_ON_JOB_CLOSE`. So Excel is asked which frame it owns (`Application.Hwnd`,
a correlation rather than an inference) and Word — which has no such property
before a document is open — must produce exactly one new pid across two looks,
or the launch fails with nothing contained. The self-check after `Documents.Open`
carries the same rule the other way: a document that lands in a frame belonging
to a *different* process proves the contained pid was never ours, and that job is
released with its kill flag cleared instead of being terminated.

**Closing never kills first, and never discards at all.** `close` saves the
document, asks the application to quit, and waits; an instance that is
gone-or-saved may then be killed through its job. A document whose save
**failed** is never closed — `Close` here means `wdDoNotSaveChanges`, and the
"keep your changes?" prompt that would normally stand in the way was silenced at
launch, so closing would destroy the edit both certainly and invisibly. (Nor is
letting Word prompt an option: that modal blocks the COM call with no timeout,
which is the hang the whole apartment design is shaped around.) The save is
retried, because the measured failures are transient; if it still will not
write, the instance is deliberately let go — its own alerts turned back on, the
job's kill flag cleared before the handle closes — so the user keeps their
unsaved work as an ordinary window on their desktop. Same for one that will not
close after a save that worked. Either way the host record carries
`close_failed` and the sweep re-asks until the window is really gone.

**Two owner decisions are encoded, not just documented** (2026-08-05).
PowerPoint is **preview-only**: it is single-instance and exposes no
`Application.Hwnd` to prove a window is ours, so the service refuses a
PowerPoint host (`powerpoint_preview_only`) rather than risk reparenting the
user's own open presentation. And native hosting stays behind
`WORKBENCH_OFFICE_NATIVE`, where **`auto` now resolves to hosting natively
wherever the machine can** — Windows, an Office to launch, and the desktop shell
attached — because the hang isolation it was conditional on is built and
measured (see below, and the decisions log).
`GET /api/office/capabilities` says all of this out loud (mode, whether hosting
is available at all, whether Office was detected, whether a shell is attached,
whether the *fake* backend is answering, and whether the fallback is OnlyOffice
or read-only preview) so the UI degrades from a fact rather than a guess.

**The fake backend** (`WORKBENCH_OFFICE_FAKE=1`, off by default, warned about at
startup — the `WORKBENCH_FAKE_AGENT` precedent) walks the same lifecycle in
process and starts nothing: its pids are counters. Failures are chosen
programmatically or by the *name of the document* (`…-refuse-embed.docx`,
`…-crash-after-embed.docx`, `…-already-open.docx`, …), so every branch is
reachable from a test and, later, from the UI.

**The panel** (`ui/src/panels/OfficeHostPanel.tsx`, registered in `tools.ts`)
claims the `office` open-file kind and renders OnlyOffice *itself* wherever it
cannot dock a real window — so OnlyOffice became the thing it falls back to
rather than the thing it replaced. Two tools now offer a view for the same kind
and the registry resolves it (earliest wins, deduplicated in `documentViews` so
a `keepMounted` kind cannot be mounted twice). The panel measures its own
rectangle every animation frame and reports it: a hosted window is not laid out
by CSS, and a zero-sized measurement is how "this tab went behind another one"
arrives — no coupling to dockview, no guessing from the active path — which
becomes `set_visible(false)`, because a real window does not disappear when its
`div` does. Refusals render as sentences about the user's document with the
preview path one click away, never as errors.

**Deliberately deferred:** the COM bridge that lets agents read and write the
*live* open document (and with it the Office skills), and Excel beyond the
launch path. Nothing here changes the OnlyOffice path below.

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

**The close paths obey the same thread rule as everything else, and two of them
used not to.** Releasing a guest is `SetWindowLongPtrW` + `SetParent` on another
process's window and `DestroyWindow` on ours, and Win32 only lets the creating
thread destroy a window — so the teardown is *asked for* through
`host/main_thread.rs` rather than performed by whoever noticed. The caller that
made this real is the close-ack watchdog in `lib.rs`, a thread spawned precisely
because the UI has stopped answering, which used to run the whole teardown
itself. It now marshals it with a two-second bound and, if the main thread does
not service it, closes without touching a guest: the work stays queued rather
than being cancelled, and a guest left docked is recoverable where a Win32 call
from the wrong thread is undefined. Symmetrically, the part of closing a panel
that is *not* a window call went the other way — killing a guest is a job handle
closing (instant, stays on the main thread), but *waiting* for the process to
actually die is bounded at two seconds and used to run inside `host_close`, so
closing one docked document froze the window for as long as the instance took to
go. `host/reaper.rs` takes the wait, and hands the one step that depends on its
answer — reclaiming a stranded keyboard — back to the main thread when it knows.
Measured on the synthetic guest with its job object detached so it cannot die:
the close body went from **2.02 s to 19 ms**, against a control that still spends
its full 400 ms bound waiting.

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

**Hang isolation was the open risk. It is now contained, and measured after the
fix** (`host/mover.rs`). A wedged guest still leaves the host window pumping its
own messages (50/50 posted messages dispatched), painting, and not judged hung
by Windows. What changed is the cost of a resize frame. Ten frames against a
hung guest, through the production path:

| ten resize frames, guest hung | time |
|---|---|
| our panel + clip child alone | 69 µs |
| including the guest, via `host::mover` | **187 µs** |
| one direct `SetWindowPos` from the main thread (the old path) | **9.98 s** |

The control is kept deliberately: without it the containment number would be
indistinguishable from "the guest was not actually hung". The mechanism is a
worker thread that owns **no window in the parent chain** and is therefore
attached to no input queue, which is the condition `SWP_ASYNCWINDOWPOS` needed
all along. It carries latest-wins coalescing (a drag storm collapses instead of
queueing) and a settle barrier, so a queued move can never land *after* a window
has been handed back to the desktop at a rectangle that only meant something
inside a clip child. A frame issued during the hang really lands once the guest
recovers — asserted, so a worker that dropped moves could not pass by being
fast. This is what unblocked `WORKBENCH_OFFICE_NATIVE=auto`.

### OnlyOffice (preview/diff/fallback)

OnlyOffice Docs Community runs as a native local service (port 8880). The backend
builds a PyJWT-signed editor config; the Document Server pulls the file from
`GET /api/office/files/{id}` and posts saves to `/api/office/callback/{id}`, which
writes to disk and re-enters the normal watcher flow. `document.key` derives from the
content hash so external changes (e.g. agent edits) force a reopen instead of serving
a stale cached copy. Absent OnlyOffice, documents degrade to read-only preview +
"Open in Word".

## Mission Control (the board, and the orchestrator seam)

Two halves, separable on purpose: a **board** that renders the fleet, and an
**orchestrator** session kind that can put agents in it.

**The board composes; it does not derive.** `ui/src/panels/MissionControl.tsx` +
`ui/src/mission.ts` join four services that already own their halves — the live
activity feed says what each session is *doing*, the usage service what it has
*cost*, the worktree pool which *slot* it holds, and the `session_status` map
every window already tracks what *state* it is in. The only thing computed in
this capability is the join (`buildCards`). That is ROADMAP M5 item 7's own
re-scope, and the reason for it is not tidiness: two live-fleet views that
disagree is worse than one, and a second cost accumulator would be a second
authority to keep honest with a budget enforced against it.

The one figure that genuinely had to be added is **per-session cost**, and it was
added *to the usage service* (`UsageSessionEntry`, `UsageService.session_entry`)
rather than beside it — so the number the orchestrator's budget refuses on and
the number the card renders are the same number by construction.

**What is new is the fleet-wide permission channel**, and it is the point of the
feature rather than decoration on it. A `PermissionRequest` reaches the human
over `/ws/agent/{id}`, a socket that exists only for conversations a window has
*opened*; so a blocked agent in a session nobody opened is invisible until it
times out ten minutes later — and once an orchestrator is spawning workers, the
session that blocks is *by definition* one the user never opened.
`AgentSession.ask_permission` therefore also publishes a `SessionPermissionEvent`
on the shared bus (with `GET /api/agents/permissions` for load and reconnect),
and `POST /api/agents/sessions/{id}/permission` is a **second door onto the same
future** — not a second mechanism. Three properties are load-bearing: the frame
always carries a session's *whole* pending set (a delta would leave a card
offering Allow for a future already resolved); the description is **not**
redacted the way the activity feed's paths are, because an approval you cannot
read is one you cannot give informedly; and a stale answer comes back **404**
rather than 200, so a click that changed nothing is never reported as a decision.

### The orchestrator (`services/orchestrator.py`)

An orchestrator is an ordinary `AgentSession` with `kind="orchestrator"`,
carrying five extra MCP tools built exactly the way the context bridge is
(`services/agent_tools.py` → `sdk_factory.build_context_bridge`): `spawn_worker`,
`list_workers`, `read_worker`, `send_to_worker`, `stop_worker`. A worker is an
ordinary session it started, bound to a **borrowed worktree slot** so two of them
cannot write to one checkout. Nothing about a worker is special downstream —
its status, activity, cost and permission prompts travel the channels every
session already travels, which is why the board sees a worker without knowing it
is one.

Four constraints, each *enforced* rather than described (`test_orchestrator.py`
names a test after each):

1. **Never auto-allow shell.** There is no code path in the service that
   resolves a permission — asserted by absence, because that absence is the
   whole threat model. No shell launcher is in any session kind's
   `allowed_tools`, so a worker's shell request blocks on the same future a chat
   session's does and is answered by a human on the board. An orchestrator that
   could approve its own workers' shell access is a different product.
   Absence from the allow-list is necessary but was never sufficient: a
   permission mode auto-approves without consulting the callback, and a subagent
   inherits its parent's mode with no way to override it per-worker. The
   `PreToolUse` broker above is what makes the guarantee hold for a worker
   rather than only for the config we happened to ship.
2. **A budget that binds, checked before spawning.** Per-worker and per-fleet
   ceilings on turns and dollars (`WORKBENCH_ORCHESTRATOR_*`), read from the
   usage service. Every refusal is a `SpawnRefusal` carrying the limit, the
   observation and **the environment variable that raises it**, so a hit cap
   renders with its way out and never as a dead button. A stopped worker's spend
   still counts, or the ceiling could be spent repeatedly by restarting.
3. **Bounded concurrency, honouring caps this service does not own.**
   `max_concurrent_sessions` belongs to the session manager and the pool size to
   `services/worktrees.py`; both are *asked* rather than duplicated, and either
   one binding produces its own named refusal. An exhausted pool is a **refusal,
   never a quiet fallback to the workspace** — two workers in one checkout is
   exactly the failure the pool exists to prevent.
4. **A worker's death is not the orchestrator's, and stopping the orchestrator
   is not an orphaning.** A turn that raises inside a worker becomes that
   worker's `failed` outcome (noticed by the per-worker pump that also feeds
   `read_worker`) and never propagates. `stop()` closes every worker of an
   orchestrator *and releases their leases* — a slot held by a process that is
   no longer working in it is what the pool's two idle signals exist to bound,
   and the honest way to avoid needing them is to give it back. `stop_all()`
   runs in the lifespan **before** `close_all()`, because once the sessions are
   gone the roster has nothing left to release with.

Two seams worth knowing. The service is constructed *before* `SessionManager`
(the client factory closes over it, since an orchestrator's toolset is built with
its client) and receives the manager through `bind()`. And `SessionManager` gained
`create_at(folder, label, …)`, which skips the workspace jail: the jail exists to
stop a *client-supplied* folder escaping, and this is reached from one place with
a path the pool just handed it under a lease this process holds. Nothing reaches
it from HTTP — `CreateSessionRequest.kind` is narrowed to `chat | orchestrator`
at the schema, which `test_orchestrator.py` asserts rather than assumes.

The orchestrator toolset is a **separate tuple** from `AGENT_TOOLS`, not a flag on
each spec, and the reason is the ergonomics budget: a description is paid once per
session but a schema is paid on **every request**, so five tools a chat session
can never call would be a cost every user pays on every message.

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

- **The layout file follows a workspace switch.** `layouts.json` is *in* the
  workspace, so a different project is a different document: the Layouts tool
  implements the registry's `onWorkspaceChanged` hook, which disarms
  persistence, drops the saved list and re-reads. Disarming first is the
  load-bearing half — persistence is armed at that point, so the next sash drag
  would otherwise write the old project's window into the new project's file.

## The workspace is not fixed at launch

Until M5 the workspace root was `Settings.resolved_workspace()`, read once in
`create_app`, so opening another project meant restarting the server with an
environment variable. `services/workspaces.py` makes the root something the
running server changes (`POST /api/workspace/switch`).

**The jail does not move; the root it asks about does.** Every path from the wire
still becomes a filesystem path through `Workspace.safe_path`, and it still
answers exactly one question — inside the root or not. What a switch changes is
*which* root, so a path from the workspace you just left is refused by the rule
that has always refused `../`. `Workspace.root` is therefore **mutable**, and
deliberately expressed as one attribute on one object: every router reaches the
jail through `app.state.workspace`, so re-pointing that object re-roots all of
them at once and none can be left behind by not being told.

**Everything that copied the root owes a `set_workspace_root`, and the list of
them lives in exactly one place.** `create_app` hands `WorkspaceService` the
services that kept their own copy — shortcuts, layouts, provenance, the session
manager (sync), then the watcher and the worktree pool (async, because they
restart something). A service added later that copies the root and is not on
that list is one that keeps serving the project the user left, and the symptom
is data from the wrong workspace rather than a crash.

**And that whole sequence is held under one lock.** `WorkspaceService.switch`
writes the jail synchronously and then *awaits* the watcher and the pool, so
without serialization two switches interleave — both jail writes land, then both
restarts do, in an order nothing makes agree. Two windows on one server is a
supported arrangement (it is why `workspace_changed` exists), so this is
reachable from the UI, and what it produces is a server whose jail and whose
watch point at different projects plus a watch on a workspace nobody is in that
`stop()` can no longer reach. Serialized rather than refused with a 409: the
second window asked for something legitimate, and it is re-validated against the
root that actually won.

- **The watcher is a real restart**, and it is awaited. `awatch` is bound to the
  directory it was opened on, so the old watch has to be *gone* before the new
  one publishes — overlapping them would put two projects' relative paths on one
  bus, and a client patching its tree from those would build a listing out of
  both. Its stop event and `IgnoreIndex` are rebuilt with it.
- **Provenance is cleared, not re-rooted.** Its map is keyed by
  workspace-relative paths, so carrying it across would attribute `src/main.py`
  in the new project to an agent that wrote `src/main.py` in the old one.
- **The worktree pool is a different pool.** Its root is keyed by the workspace
  it serves and its slots are checkouts of *that* repository, so a switch is a
  full stop/start: the cross-process lock is released, the slot table dropped,
  and `start()` rediscovers the repo. Slots in the old pool are left as they
  are — a lease is not cancelled by the user opening another folder.
- **Terminals and live agent sessions keep running.** A PTY is a shell that was
  never inside the path jail (it can `cd` anywhere by design), and an agent may
  be mid-turn holding an edit it has not written; killing either because the
  user opened another folder is silent loss of running work. New terminals and
  new sessions are created under the new root — `routers/terminal.py` now reads
  `app.state.workspace.root` rather than the launch setting, which is where a
  new shell was quietly still opening in the old project. A live session whose
  folder is no longer inside the root is **relabelled with its absolute path**,
  so the session list cannot file it under a same-named folder of the project it
  has nothing to do with.
- **Recent workspaces are the user's data, not a project's.** They live in
  `<app data>/workspaces.json` (`services/app_data.py`, the same home the pool
  root uses), never in a `.workbench/` folder: a list of the projects you work on
  would otherwise be written into one of them. Most recent first, capped, deduped
  case-insensitively because Windows paths are, and a folder that is gone is
  listed as unavailable rather than forgotten. An unreadable or foreign-version
  file costs the history and nothing else.
- **Named sessions live beside the recents, and are global for the same reason —
  but are *not* re-rooted by a switch.** `<app data>/sessions.json`
  (`services/sessions.py`, M5 item 15 PR2) holds the detachable working sessions:
  each is a manifest tying a workspace + a dockview arrangement + references to
  its live agents and worktree leases into one thing a user can leave and return
  to. It copies the recents file's discipline exactly (version-stamped, atomic
  tmp-then-`os.replace` with the Windows-lock retry, utf-8-sig read, never raises
  on a bad file). Unlike `layouts.json` it is machine-local, not per-project: one
  project can host more than one named session, and a session is a thing you
  return to from anywhere. So the store is **queried by** workspace rather than
  **rooted at** one — it owns no `set_workspace_root` and is deliberately absent
  from `WorkspaceService`'s rootables. Switching the current project changes which
  sessions a window asks to see, never which sessions exist. (PR2 is the headless
  persistence + REST surface; the attach/detach UI is PR3.)
- **The UI adopts, it does not reload.** `store.adoptWorkspace()` throws away
  everything keyed to the old root (open editors, the tree, provenance, session
  groups) and re-reads; then `notifyWorkspaceChanged(TOOLS, root)` tells every
  tool that keeps workspace-scoped state. A dirty buffer is the one thing kept:
  the switcher blocks on its own unsaved work with the dirty-close confirm, and
  a buffer left dirty by *another* window's switch stays on screen as an orphan
  that cannot be saved — the same relative path in the new workspace is a
  different file.
- **A docked Office window is unsaved work that is not a buffer**, and it gets
  the mirror-image hook. An `office` open file is never marked dirty — the
  unsaved paragraph is inside Word — so the dirty check is blind to it, and the
  panel that renders `close_failed` is the very one a switch unmounts. Tools
  holding something like that declare a `workspaceSwitchGuard` (`registry.ts`):
  `held()` names it for the confirm dialog, `settle()` closes it **before** the
  root moves, and anything that would not settle *cancels* the switch, with the
  window still on screen to say so. `hosts` in `officeHost.ts` is keyed by
  workspace-relative path, so `onWorkspaceChanged` then drains the closes that
  are still in flight before clearing it — clearing first would let a late
  response write `report.docx` from the old project back into the map that backs
  `report.docx` in the new one.
- **Only the shell can show a folder dialog.** There is no web API returning a
  filesystem *path* for a directory, so `pick_directory` is a shell command
  (`tauri-plugin-dialog`, registered in Rust and reached only through our own
  command) and a browser tab gets an honest typed-path prompt instead of a dead
  button — `ui/src/shell.ts`, as with every other native capability.

## The managed worktree pool

`CLAUDE.md` has required "one writer per checkout, always" since M4, enforced by
discipline. `services/worktrees.py` makes it a feature: a small pool of git
worktrees a caller **borrows** a slot from, works in, and gives back. It is also
the substrate Mission Control's workers need.

```
routers/worktrees.py ──► WorktreeService ──► git (asyncio.create_subprocess_exec)
   REST                    slots by name          worktree add --detach
   /ws/events ◄── WorktreeChangedEvent             status --porcelain
                                                   reset --hard / clean -fd
%LOCALAPPDATA%\Workbench\worktrees\<workspace-key>\
   pool.json      the state document (atomic write, Windows-lock retry)
   slot-01/ …     detached checkouts, kept warm, never removed
```

**Four decisions, implemented rather than described.**

- **Detached HEAD.** Every slot is `git worktree add --detach`, so a pooled
  worktree carries no branch and *"already checked out at …"* cannot happen —
  the wall every fix-stage agent in this repo's own workflow hits when two lanes
  want one branch. What a holder does inside its slot (branch, commit, push) is
  the holder's business; what the pool hands out is a commit.
- **Pool, never destroy.** There is no `git worktree remove` and no
  `shutil.rmtree` in the module, asserted by watching every git argv a full
  acquire→release→discard→prune cycle runs. A finished slot is reset and
  returned, so `node_modules`, `.venv` and build caches stay with it: they are
  *ignored* files and the only cleaning is `git clean -fd`, never `-x`. A cold
  install is paid once per slot, not once per task. It also avoids a hazard
  measured here — `git worktree remove` recurses through a Windows junction, so
  any design that *links* dependencies into a slot can empty the checkout they
  point at.
- **Two idle signals.** A lease carries an `owner_pid` *and* an `expires_at`,
  and `prune()` reclaims only when **both** say idle. The deadline holds a slot
  for an agent working unattended with nothing of ours running; the pid holds it
  past the deadline for an owner that is demonstrably still there. The liveness
  probe is `OpenProcess` + `GetExitCodeProcess` and never `os.kill(pid, 0)` —
  which on Windows CPython is `TerminateProcess`, i.e. a probe that kills what it
  asks about. Its two imprecisions (a process that exited with code 259 reads as
  alive, and so does a recycled pid) both hold a slot *longer*, which is the
  direction that costs a wait rather than an agent's work.
- **Fail safe on corrupt state.** A `pool.json` that is truncated, unreadable,
  not JSON or from another version is not repaired — the pool is rebuilt from
  what git reports on disk and **every** slot comes back `leased` under a
  `recovered` lease. Assume in use; never assume free. A directory in the pool
  root git does *not* know as a worktree becomes `needs_review` and is left
  exactly where it is.

**Dirty is sacred, and it outranks all four.** A slot whose `git status
--porcelain` is non-empty is never handed out and never reclaimed without an
explicit `force`; a status that *fails* is read as dirty, never as clean; and
the disk beats the state file, so a slot recorded free with work in it is
re-parked as `dirty` rather than given away. The one thing dirty protection is
*not* is a one-way door: every sweep re-asks, and a slot git now reports clean is
freed — which discards nothing, because there is nothing left to discard.

**What Windows actually does, measured** (`test_worktrees.py`, with a real
`CreateFile` share-mode-0 handle — Python's own `open()` shares everything and
would prove nothing):

| with an exclusive handle on a tracked file | result |
|---|---|
| `git status --porcelain` | reports it `M` — git cannot open it to compare, so it says changed |
| `git reset --hard <other commit>` | `error: unable to unlink old 'model.py'` |
| after the handle closes | status clean again, reset succeeds |

So the dirty guard fires *before* any reset is attempted, which is the safest
place for it to fire, and the reset failure sits behind it. Neither costs a
byte. A reset that keeps failing is retried on a short bounded budget (the same
shape `services/layouts.py` uses for `os.replace`) and then becomes
`needs_review` — never `--force`, because git's reset is already forceful and
the failure is the filesystem's.

**The pool root is outside the workspace, and that is load-bearing.** It lives
under `%LOCALAPPDATA%\Workbench\worktrees\<name>-<digest of the workspace path>`
— *not* under `.workbench/`, because a worktree inside the workspace would be
walked by `Workspace.tree()`, watched by the watcher and indexed as N more
copies of the project: every file would appear `pool_size` times in the tree and
every checkout would arrive as a watcher storm. The tests assert the property
rather than assume it — `safe_path` refuses a slot, the tree does not list one,
and a real `git worktree add` through the API produces no file event on
`/ws/events`.

**When the reset happens.** A clean slot is returned *as it is*; the reset that
repurposes it runs at **acquire** time. Acquire is the only moment the pool
knows which commit to reset *to*, and it is the moment nothing is running in the
slot — whereas a release fires exactly as the holder's own processes are letting
go of their handles, which is when a Windows reset is most likely to fail.
Commits a holder made and did not push survive that reset: nothing here runs
`gc`, `worktree remove` or `clean -x`, so they stay in the object database,
reachable through the slot's own `HEAD` reflog.

**And why that reset is `--keep`.** The dirty check and the reset behind it are
two git processes, so there is a gap between them, and a slot that was clean
when it was asked can be written to before the reset lands — by a build daemon
the previous holder left running, a language server, an indexer: the same class
of background writer the lock table above is about. Under `reset --hard` that
write was overwritten with no `dirty`, no `needs_review`, no event and no log
line, which is the one failure this service is not allowed to have. `--keep`
refuses to overwrite a locally-modified file, so the decision and the
destruction happen inside *one* git process rather than across a gap:

| a write that lands in the gap | what happens now |
|---|---|
| to a file that **differs** between `HEAD` and the base | `--keep` aborts; status is re-read, the slot is parked `dirty`, the work is intact |
| to a file the two commits **agree** on | `--keep` keeps it; the post-reset status check sees it and parks the slot `dirty` |
| nothing raced | status is empty after the reset, and *only* then is the slot leased |

The failed-reset path re-reads `git status` rather than matching a substring of
git's stderr, because the two causes need different answers: a racing writer
leaves the slot dirty and heals itself when the writer stops, a held handle
leaves it clean and unresettable and wants a human. `--hard` survives only on
the two paths where destruction is what the caller asked for by name —
`release(discard_changes=True)` and `prune(force=True)` — and those now verify
that the slot really is empty before reporting it `free`, since the `clean -fd`
behind the reset is a second process with a gap of its own.

**One writer per pool, enforced by the OS.** The service's `asyncio.Lock` is
exactly as wide as one interpreter: it serialises concurrent requests to *one*
server and nothing else. Two `workbench-server` processes pointed at the same
workspace (a crashed server not yet reaped, two dev instances on one folder)
share a pool root and a `pool.json`, and both could read one slot as free, both
prepare it, both lease it, with the last save winning the state file — one
checkout, two writers, the invariant the whole feature exists to provide. So
`PoolLock` takes an exclusive byte-range lock on `<pool root>/pool.lock` for the
life of the process (`msvcrt.locking` on Windows, `flock` elsewhere). A server
that cannot take it serves no pool: `GET /api/worktrees` carries the reason,
acquire is a 503, and everything else in that server starts normally. The lock
is held by a **handle**, not by the existence of a file, so a killed server
cannot leave a pool permanently unopenable — the OS drops it when the process
dies, and a clean shutdown releases it explicitly so the next server does not
have to wait to be lucky.

**Not built yet** (ROADMAP M5 item 6 carries them): multi-root file and terminal
access through a root registry, so a slot can be *opened* in the UI with the path
jail preserved per root; worktree-bound agent sessions; and per-slot watchers.
The pool is the substrate those need, and it stands alone without them — the
endpoints are usable today by anything that can make an HTTP call.

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
     prompt, a fixed `PlanArtifact`, and three `RateLimitEvent`s (one per window,
     the shape the CLI really sends) — through the *same* factory and `SessionBridge` seams the
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
   launched in a per-run temp workspace with fake-agent mode on. Eleven journeys: file
   CRUD + save + watcher round-trip + conflict + dirty-close, terminal tabs against real
   ConPTY, QuickBar/shortcuts (including the never-executed rule, and a registered
   tool reaching the user through the registry alone), chat streaming and
   tool settling, plan cards, **visual artifacts** (every leaf kind drawn, the
   25-hour day labelled in its market's zone, markup rendered as text, and *zero*
   network requests during the render — the safety property that can only be proven
   in a real browser), status chips and the attention badge, office degraded
   mode, and provenance (an agent write is marked, attributed, opened, acknowledged,
   and links back to its session — *and* the negative half: an announced-but-failed
   write followed by the user's own change, and the user's own saves, mark nothing),
   and **plan usage** (the empty state *first*, because it is the likeliest one an
   account really shows, then the meters a rate-limit transition produces, their
   stamps, the words next to the near-cap colour, the status reading that was
   silent a moment earlier, and a reload proving the load path agrees with the
   socket), and **live agent activity** (an idle fleet that says so, a session
   appearing before it has done anything, a call asserted *while it is in
   flight* — the `slow tool` trigger exists for exactly that — the line replaced
   in place when it settles, the file target opening the file, three
   conversations changing at their own rhythms, and a Grep-heavy turn that
   leaves a capped row rather than a growing one). Journey 11 adds the
   **conversation browser**: the suite seeds `.claude-projects` with one
   transcript per case the browser has to tell apart (inside the workspace,
   outside it, a folder that no longer exists, one that will not parse), then
   asserts the folder groups render, that search filters across titles *and*
   folder names, that a refused folder is *shown* with its reason, that a row
   resumes into an agent pane carrying its history, that opening the same row
   twice focuses that pane rather than cloning it, and that two browser panes —
   one scoped to a folder, one not — stay independent through a reload. The two
   states no browser journey can reach in time — a quarter-hour-old usage
   snapshot, and four sessions all mid-tool-call at the same instant — are
   `renderToStaticMarkup` tests instead (`ui/src/panels/UsagePanel.test.tsx`,
   `ui/src/panels/ActivityPanel.test.tsx`).
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

   **The bundle budget** (`ui/e2e/perf/bundle.spec.ts`) is the third shape: bytes,
   which are as deterministic as counts and so also block. It reads rollup's own
   per-chunk module attribution — written to `dist/bundle-metafile.json` by a
   plugin in `vite.config.ts`, aggregated by package so it stays a few kB — and
   asserts a gzipped ceiling on the eager entry chunk *and* that **no module under
   `node_modules/monaco-editor` is in it**. The second assertion is the one that
   holds: a size ceiling alone lets an editor creep back a defensible import at a
   time and only fails when it is far too late. Presence is the invariant, size is
   the symptom, and a file listing cannot answer either — only what is inside a
   chunk can.

   **One workspace, walked in file order**, is the other cost of a single worker.
   The app persists the window arrangement *into* the workspace it is being
   measured in (`.workbench/layouts.json`, on a 500 ms debounce), so without a
   reset a spec starts in whatever window the spec before it left and a budget
   becomes a function of the alphabet: `panes.spec.ts` builds a 13-pane fleet on
   purpose and sorts immediately before `watcher.spec.ts`, whose row then waited
   30 s below the fold of a file tree a fraction of its former height — a spec
   that passes in 4.5 s alone. Every perf spec therefore takes its `test` from
   `ui/e2e/perf/window.ts`, which puts the empty layouts document back before the
   page navigates; one PUT on an API context, so the launch timeline it must not
   disturb still begins at `goto`, and an eslint rule makes that import the only
   way to get a `test` in the directory. What the reset does **not** cover is
   server-side state — agent sessions and PTYs outlive the page that made them,
   and `launch.spec.ts`'s layout-shift budget can still read a session
   `activity.spec.ts` created (see its docstring).

   The lane is **disk-neutral**, which is not free when the fixture is 5,105 files:
   a bare run builds it in a `mkdtemp` directory and removes it — along with the
   `-projects` sibling the backend puts next to it — from a `process.on("exit")`
   hook in `ui/e2e/perf/fixture.ts`. Not a Playwright `globalTeardown`: those run
   *before* the `webServer` processes are stopped, and on Windows a directory that
   is a live process's CWD, with a watcher handle on every folder inside it, cannot
   be deleted. Ownership is the other half of the rule and lives in
   `ui/e2e/perf/workspace.ts` — a `WB_PERF_WORKSPACE` a developer named is kept, a
   temp directory this run created is not, and leftovers from a run killed before
   its hook are swept by the next bare run once they are a day old.
