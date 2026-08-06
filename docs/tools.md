# Adding a tool

A **tool** is one Workbench capability. It declares what it contributes — a panel,
commands, chords, status items, agent-facing tools — in a single descriptor that lives
next to its own code, and joins the app through one line in `ui/src/tools.ts`.

Nothing else changes. In particular, **do not** edit `App.tsx`, `commands.ts` or
`StatusBar.tsx` to add a capability: those files name no panel, no panel-specific
command and no status item, and keeping them that way is what lets several people land
panels at once without fighting over the same diff.

The type is `WorkbenchTool` in `ui/src/registry.ts`; the derivations it feeds are pure
functions in the same file, so everything below is unit-testable
(`ui/src/registry.test.ts`).

## The smallest useful tool

`ui/src/panels/Scratchpad.tsx` is the worked example — a panel, the command that opens
it and its tab icon, in one file:

```tsx
export const scratchpadTool: WorkbenchTool = {
  id: "scratchpad",
  title: "Scratchpad",
  icon: PadIcon,
  panel: {
    component: ScratchpadPanel,
    defaultLocation: { area: "right", size: 380 },
    openByDefault: false,          // not in the startup layout; its tab closes it
  },
  commands: [
    { id: "scratchpad.open", title: "Open Scratchpad", run: () => openPanel("scratchpad") },
  ],
};
```

and in `ui/src/tools.ts`:

```ts
export const TOOLS: readonly WorkbenchTool[] = [
  filesTool, editorTool, agentTool, terminalTool, officeTool, scratchpadTool,
];
```

That is the whole diff. The QuickBar lists the command, the panel docks where the
descriptor says, and its tab gets a close button because it is not in the startup
layout — none of which is written down anywhere else.

**On chords.** This example claims none, on purpose. A registered chord wins every
collision with the user's `shortcuts.md`, and `Alt` is the only modifier that file may
bind — so every `Alt` chord a tool takes is one the user can no longer choose, silently.
`Alt+T` for a new terminal earns that; a demo does not. Add a `shortcuts` table when the
command is one a user would reach for without looking.

## What you can contribute

| Field | Effect |
|---|---|
| `panel` | A dockview panel. `defaultLocation.area` is `center` \| `left` \| `right` \| `bottom` (`size` = initial width, or height for `bottom`). `openByDefault: false` keeps it out of the startup layout until a command opens it — and gives its tab a close button, since that tab is its only way back. `singleton: false` allows more than one pane of it — see **Plural tools** below. `badge` is one component rendered after the tab title (dot-only, DESIGN.md §6.4). |
| `panel.instances` | What a *second* pane of a plural tool is bound to: `options()` (the rows the pane picker offers) and `titleFor(key)` (what such a pane calls itself). A row may set `disabled: true` with the reason in `detail` — a cap the tool knows about before the gesture. See **Plural tools**. |
| `groupActions` | One control at the right end of every pane's tab strip, for a tool that acts on panes rather than living in one. The pane system's split glyphs are the only one (DESIGN.md §6.11); there is room for one. |
| `documentView` | Renders one `OpenFile` kind inside the editor area (`kind`, `component`, `hostClassName`, `keepMounted`). This is how Office panels attach — the editor area asks the registry, not a list of extensions. |
| `commands` | The `Command` shape from `commands.ts`, minus `keys`. `when` hides a command from the QuickBar and makes its chords inert — this one *is* live, re-read on every keystroke; `detail` is the right-hand text on the row; `category` puts it in its own QuickBar section. |
| `dynamicCommands` | `{ key, build }` for commands whose *set* changes while the app runs — one row per saved layout, and one per recent workspace later. `build` is called only when `key()` changes, because the merged command list is read on every keystroke. **Dynamic commands never carry a chord**: a chord has to be static to be pinned by a test and to lose a `shortcuts.md` collision deterministically. |
| `shortcuts` | `{ commandId: ["Alt+X", …] }` — the tool's whole keymap in one block. A key that names no command of that tool fails `registry.test.ts` rather than silently binding nothing. Take an `Alt` chord only if the command earns it (see above). |
| `statusContributions` | `{ region: "left" \| "center" \| "right", component }`. Rendered in registry order inside the region. |
| `shortcutKinds` | `["shell"]` or `["prompt"]` — the `shortcuts.md` kinds this panel hosts. An entry of that kind brings this panel forward before it is inserted. |
| `shortcutActions` | `{ layout: (body) => … }` — a `shortcuts.md` kind this tool *carries out* instead of inserting. Only one kind uses it today (`layout`), and only because moving panels is all it can do; if you are adding a kind that touches a file, a shell or an agent, you are breaking the rule that a workspace file may add rows and never actions (`docs/shortcuts.md`). |
| `onDockReady` | The live `DockviewApi`, once, when the dock exists (and `null` when it goes away) — for a tool that operates on the dock rather than living in it. The layout system is the one; anything else should be reaching for `openPanel`. |
| `when` | Takes the whole tool out: no panel, no commands, no status items. **Asked once**, when the registry is first derived, and remembered — gate on build- or boot-time facts (a flag, `isTauri()`). Anything that changes while the app runs belongs on a command's own `when` or inside the panel. |
| `icon` | Optional glyph in the panel tab. |

Agent-facing tools are *not* here — see below.

## Rules the tests enforce

- **Tool ids and command ids are unique**, and no chord is bound twice across every
  registered tool (`registry.test.ts`).
- **Chords go in `shortcuts`, not on the command.** The type enforces it; the point is
  that a tool's keymap is one readable table, and a user keymap file will later override
  exactly that layer.
- **A registered chord beats a `shortcuts.md` chord.** User entries merge on top and
  lose every id and chord collision — they keep their QuickBar row and lose the binding
  (`commands.test.ts`). File-supplied chords must carry `Alt`; see `docs/shortcuts.md`.
- **The default layout is pinned.** `registry.test.ts` asserts it is exactly Editor /
  Files / Agent / Terminal in the same places. A new panel either joins that list on
  purpose (and shifts nothing else) or ships `openByDefault: false`.
- **`Ctrl+1..N` is derived**, in registry order, from the panels in the default layout.
  Adding a fifth default panel gives it `Ctrl+5`; nothing is hardcoded.
- **Every shipped chord is pinned to the command it runs** (`registry.test.ts`). Moving
  one means editing that table — which is the deliberate act it should be, since the
  alternative is a reflex that quietly starts doing something else.
- **A panel your tool contributes may be missing when a saved layout names it.** Layouts
  persist per workspace, so a tool removed (or renamed, or `when`-gated) leaves its panel
  id behind in `.workbench/layouts.json`. The layout system drops it and keeps the rest
  (`ui/src/layouts.ts`); what you owe it is a **stable `id`** — renaming one is a rename
  of the user's saved arrangement too.
- **A tool id may not contain `#`** — it is the pane-id separator, and a tool called
  `a#b` would make every pane of it address a tool called `a` (`panes.test.ts`, against
  the real registry).

## Plural tools (more than one pane of the same thing)

Any pane in Workbench splits in two and anything registered goes in the new one
(`ui/src/panels/Panes.tsx`, DESIGN.md §6.11). A tool that is worth having **twice** —
four agent sessions on screen, two shells, two files side by side — sets
`singleton: false` and declares `instances`:

```ts
panel: {
  component: TerminalPanel,
  defaultLocation: { area: "bottom", size: 260 },
  singleton: false,
  instances: {
    options: () => [{
      id: "terminal.new",
      title: "New terminal",
      detail: "a shell in its own pane",
      category: "Terminal",
      key: () => useStore.getState().nextTerminalPaneKey(),
    }],
    titleFor: (key) => `Terminal ${key}`,
  },
},
```

Your panel component then decides how to render from **its own pane id**:

```tsx
export function TerminalPanel(props: IDockviewPanelProps) {
  const paneKey = paneInstance(props.api.id);   // ui/src/panes.ts
  return paneKey === null ? <TerminalTabs /> : <TerminalPane paneKey={paneKey} … />;
}
```

### The instance key is a contract, like the tool id

A pane id is `toolId` or `toolId#instanceKey`, split on the **first** `#`. dockview
serializes panel ids into `.workbench/layouts.json` and nothing else about a panel, so
**the pane id is the whole of what a restart gets back**. Three rules follow:

- the key must still mean the same thing after a restart — `agent#<session_id>`,
  `editors#<workspace-relative path>`, `terminal#<n>`,
  `conversations#<claude-project-key>`. Changing what a key means renames the user's
  saved arrangement, exactly as renaming a tool id does. The conversation browser is
  worth a look here: its key is *Claude Code's own encoded directory name* rather than a
  path, because that encoding is lossy and the path is the thing we cannot reconstruct —
  the key that persists is the one we know is true;
- `key()` is a thunk and may be `async`, because minting one sometimes takes a round trip
  ("New agent session" creates the session, then binds the pane to its id). Answering
  `null` abandons the split rather than binding a pane to nothing;
- **be honest about what does not survive.** `terminal#2` restores the pane, its number
  and a fresh shell: the PTY dies with the WebSocket and the server releases it. Say so
  in the module rather than implying the process comes back.

### What the rest of the app does for you

- **The picker** lists your rows automatically, in registry order, sectioned by
  `category` — no registration beyond the descriptor.
- **`pruneLayout`** drops an instance pane whose tool is a singleton today, or whose id
  and component disagree, and keeps the rest (`ui/src/layouts.ts`). It deliberately does
  *not* vet the key itself: sessions and files load long after the layout does, so a pane
  bound to something that has not arrived yet must not be dropped for being early. Handle
  that inside your panel — a one-line note bar, never an empty pane (DESIGN.md §6.11).
- **Focus is the selector.** The focused pane is the one the app means: bind on
  `props.api.onDidActiveChange` and make your pane's session/file/terminal the active one
  there. That is what let `Chat.tsx` be mounted four times without a single change to it.
  Gate that binding — and anything it acquires — on the resource being *confirmed* rather
  than merely named: a restored pane is a claim, and `agent#<id>` after a restart names a
  session the server no longer has. The Agent pane waits for the session to appear live
  in the listing before it opens a socket, because a socket that reconnects behind a
  correct "this session is gone" note is a reconnect storm nobody can see.
- **Opening a pane is one call.** `revealPane(toolId, key)`
  (`ui/src/panels/Panes.tsx`) puts `toolId#key` on screen, or focuses the pane that
  already shows it; a `null` key means the tool's own default pane. That second half is
  not a nicety: the conversation browser opens agent panes, and "open this conversation"
  must never clone the pane it is already in. Use it rather than reaching for
  `dockApiHandle()` — placing panes belongs to the pane capability, and a second copy of
  `addPanel` is how the two drift. It is the *open* gesture; `placeChoice` is the *split*
  gesture, and the difference is what happens to a pane that already exists (focused
  where it is, versus moved into the split you asked for).
- **A plural tool's "open my panel" command must use it too.** `openPanel` deliberately
  mints a *second* pane when a plural tool's default panel is already open — correct for
  "give me another terminal", wrong for "show me the browser", which would hand the user
  a duplicate every time they ran the command and persist its throwaway
  `<tool>#<timestamp>` key into the saved layout as an instance binding that means
  nothing. `revealPane(toolId, null)` is the version that focuses what is already there.
- **A limit you know before the gesture belongs on the row.** `disabled: true` on an
  instance option greys it in the picker with `detail` as the reason, and the keyboard
  skips it (DESIGN.md §6.5). The agent's `New agent session` uses it: the server caps
  concurrent sessions, so offering the row and refusing after the round trip would spend
  the whole split before mentioning a limit. Read the cap from the server
  (`GET /api/agents/limits`) rather than re-deriving it — that ceiling counts sessions
  *working*, not sessions open, and a client that guessed would grey at the wrong moment.

## Agent-facing tools

If your capability also gives agents an MCP tool, it is declared in **one** place:
`server/src/workbench_server/services/agent_tools.py`, the registry the SDK reads. Not
in the UI descriptor — a copy of the model-facing text on the client would be a second
authority to keep honest with nothing reading it.

`output_format` and `max_result_bytes` are required fields, so `mypy --strict` fails an
omission, and `server/tests/test_agent_tools.py` binds the budget: a ceiling on the
description (it is loaded into *every* session's context, so you pay for it on every
request) and your tool's own ceiling on the serialized size of a representative result.
Size that one from the measured payload plus a margin you can state — a number with room
for anything is a test that cannot fail. Prefer compact JSON or plain text over
pretty-printed JSON; prefer a thin call over a wrapped API.

## State

zustand, always — it is the only state library, and adding a second one is not a call a
tool gets to make. **Where** the store lives is the call you do get to make, and it has
one rule: state nothing outside your module reads may live in a `create()` instance in
your own module; state another tool reads is app-wide and belongs in `ui/src/store.ts`.

This is the registration rule again, not an exception to it. A tool that puts its own
state in `store.ts` has put a capability back into a shared file — the same collision
between parallel lanes that keeping `App.tsx` and `commands.ts` capability-free exists to
prevent, arriving by a different door. The layout system (`ui/src/panels/Layouts.tsx`)
is the first tool to own one, and the usage meters (`ui/src/usage.ts`) the second —
which also shows the other half of the pattern: a tool that needs live server events
subscribes to `/ws/events` itself and filters for its own frames, rather than adding a
branch to the app store's dispatch. `useStore` is still what a tool reaches for to raise
a toast, because toasts genuinely are app-wide.

If your tool's state starts being read from outside, that is the signal to move it to
`store.ts` — not to export a getter from your module.

## Styling

Follow `DESIGN.md` tokens — no literal colours, ever. A one-file tool can use inline
styles that reference tokens (`var(--surface-panel)`), as `Scratchpad.tsx` does; a tool
big enough to need a stylesheet adds `ui/src/styles/<tool>.css` and imports it from its
own module, not from `main.tsx`.

## What is deliberately not here yet

Registration is **static**: `TOOLS` is resolved at build time. There is no dynamic
plugin loader, and this document is not yet a stable public contract. The descriptor is
shaped so one can be added — every derivation takes a tools array rather than reading
`TOOLS` — which is the seam user-authored tools will plug into (ROADMAP, M5 → M7).
