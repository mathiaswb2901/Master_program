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
it, its chord and its tab icon, in one file:

```tsx
export const scratchpadTool: WorkbenchTool = {
  id: "scratchpad",
  title: "Scratchpad",
  icon: PadIcon,
  panel: {
    component: ScratchpadPanel,
    defaultLocation: { area: "right", size: 380 },
    openByDefault: false,          // not in the startup layout
  },
  commands: [
    { id: "scratchpad.open", title: "Open Scratchpad", run: () => openPanel("scratchpad") },
  ],
  shortcuts: { "scratchpad.open": ["Alt+S"] },
};
```

and in `ui/src/tools.ts`:

```ts
export const TOOLS: readonly WorkbenchTool[] = [
  filesTool, editorTool, agentTool, terminalTool, officeTool, scratchpadTool,
];
```

That is the whole diff. The QuickBar lists the command, the keymap binds the chord, and
the panel docks where the descriptor says — none of which is written down anywhere else.

## What you can contribute

| Field | Effect |
|---|---|
| `panel` | A dockview panel. `defaultLocation.area` is `center` \| `left` \| `right` \| `bottom` (`size` = initial width, or height for `bottom`). `openByDefault: false` keeps it out of the startup layout until a command opens it. `singleton: false` allows more than one instance. |
| `documentView` | Renders one `OpenFile` kind inside the editor area (`kind`, `component`, `hostClassName`, `keepMounted`). This is how Office panels attach — the editor area asks the registry, not a list of extensions. |
| `commands` | The `Command` shape from `commands.ts`, minus `keys`. `when` hides a command from the QuickBar and makes its chords inert; `detail` is the right-hand text on the row. |
| `shortcuts` | `{ commandId: ["Alt+X", …] }` — the tool's whole keymap in one block. A key that names no command of that tool fails `registry.test.ts` rather than silently binding nothing. |
| `statusContributions` | `{ region: "left" \| "center" \| "right", component }`. Rendered in registry order inside the region. |
| `when` | Takes the whole tool out: no panel, no commands, no status items, no agent tools. |
| `agentTools` | `{ name, description, outputFormat }` — what this capability adds to an agent's context. |
| `icon` | Optional glyph in the panel tab. |

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

## Agent-facing tools

If your capability also gives agents an MCP tool, declare it in the descriptor's
`agentTools` **and** in the server registry, `server/src/workbench_server/services/
agent_tools.py`, which is what the SDK reads. `output_format` is required there, so
`mypy --strict` fails an omission, and `server/tests/test_agent_tools.py` binds the
budget: a ceiling on the description (it is loaded into *every* session's context, so
you pay for it on every request) and on the serialized size of a representative result.
Prefer compact JSON or plain text over pretty-printed JSON; prefer a thin call over a
wrapped API.

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
