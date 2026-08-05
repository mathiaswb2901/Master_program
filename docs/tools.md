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
| `panel` | A dockview panel. `defaultLocation.area` is `center` \| `left` \| `right` \| `bottom` (`size` = initial width, or height for `bottom`). `openByDefault: false` keeps it out of the startup layout until a command opens it — and gives its tab a close button, since that tab is its only way back. `singleton: false` allows more than one instance. `badge` is one component rendered after the tab title (dot-only, DESIGN.md §6.4). |
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
