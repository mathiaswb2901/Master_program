# shortcuts.md

Your personal shortcuts, in one markdown file you write like a note. Every entry shows
up in the QuickBar (`Ctrl+Shift+P`), can carry its own keybinding, and either inserts a
shell snippet or a prompt template into whatever you are working in, switches the window
to one of your saved layouts, or runs one of the commands the app already knows.

## Where it lives

| File | Scope |
|---|---|
| `<workspace>/.workbench/shortcuts.md` | this project |
| `~/.workbench/shortcuts.md` | every project |

Both are optional. They are merged, and the workspace file wins: an entry there with the
same name replaces the global one. Both are re-read the moment you save them — no
restart, no reload.

## Shortcuts never run anything

This is a rule, not a default:

- A `shell` entry is **typed into the active terminal without a trailing newline**. You
  read it and press Enter. In a live terminal the control bytes are key events rather
  than characters — a newline *is* Enter, `Ctrl+O` submits the line in bash and in
  PowerShell's Emacs edit mode, `Esc` opens an editing sequence — so a shell body must be
  a single line of printable text. Anything else is refused, not half-executed.
- A `prompt` entry is **appended to the chat box** of the active agent session. You press
  Send.
- A `layout` entry **switches the window to one of your saved layouts**, by name. It is
  one of the two kinds that act rather than insert, and the reason it may is that moving
  panels is the only thing it can do: no text reaches a shell or an agent, no file is
  touched, and a name you never saved is a message, not a guess. Its body is one line, no
  longer than a layout name — it cannot carry a payload.
- A `command` entry **runs one of the commands the app already knows**, named by its id
  (`layout.save`, `pane.split.right`, `view.toggleTheme`, …). This is the kind that lets
  your file reach *anything the registry knows* — a command is bindable the day it is
  added, with no change here. Its safety is the same bar `layout` clears, drawn one place
  tighter: a command that could **reach a file or move the workspace** — opening a folder
  and deleting a saved layout are the ones — declares itself off-limits to a shortcuts
  file, because your project's own
  `.workbench/shortcuts.md` is untrusted input. An entry naming such a command, or a
  command that does not exist, is refused with a message and never runs.
- The QuickBar shows a shell entry's **actual snippet** next to its name, a layout
  entry's **actual layout**, and a command entry's **actual command id**, not the
  `detail:` line from the file, so a row cannot describe itself as one thing and do
  another.

There is no `run: true` option and there will not be one. A `command` entry does run a
command — but only one the app itself registered and marked safe for a file, never a
shell line, a prompt, or a path. Opening someone else's workspace can add rows to your
QuickBar, rearrange your panels, and fire safe registered commands; it cannot type into a
shell, send a prompt, open a folder, or read a file.

## Format

An `##` heading names the shortcut. Optional `key: value` lines configure it. A fenced
code block is the body. Anything else in the file — a title, prose, notes — is ignored.

| Key | Meaning |
|---|---|
| `type` | `shell` (default), `prompt`, `layout` or `command`. A fence tagged with a kind selects it too — ` ```prompt `, ` ```layout `, ` ```command ` — while ` ```powershell ` stays a shell body. |
| `keys` | One chord, e.g. `Alt+G`. **Must include `Alt`.** Optional. |
| `detail` | One line shown next to the name in the QuickBar — `prompt` entries only; `shell`, `layout` and `command` rows show what they will actually do instead. Optional. |

A `layout` body is the name of a layout: one of the built-ins (`Default`, `Review`,
`Focus`, `Agents`) or one you saved yourself from the layout chip in the status bar.
Names are matched without case.

A `command` body is a registered command's id. The QuickBar's command mode
(`Ctrl+Shift+P`) is the catalogue: every row there is a command you can bind by id. Most
are bindable; the few that open a folder, move the workspace, or delete a saved layout
are not, and an entry naming one — or an id that does not exist — becomes a message
rather than a binding.

`Alt` is the only modifier a shortcuts file may take, and it is the one that works:
plain keys are never intercepted, and inside the terminal or editor only `Alt` and
`Ctrl+Shift` chords reach Workbench (`DESIGN.md` §6.8). Everywhere else Workbench takes
*any* `Ctrl` chord, which is why a file cannot have one — `Ctrl+V` would stop pasting
into the chat box. Built-in bindings win too: ask for `Alt+T` and the entry keeps its
QuickBar row, loses the chord, and says so. **Which `Alt` chords are already taken is
never a guess**: the keyboard reference (`Alt+K`, or the `Keys` chip in the status bar)
lists every chord the app binds, generated from the registry — search it for `alt` before
choosing one.

Anything inside a fenced code block is example text, not shortcuts — including `##`
lines. The starter below can be pasted into your file as a reference without arming it.

### Copy-paste starter

````markdown
# My shortcuts

## Status board
type: shell
keys: Alt+G

```
git status -sb
```

## Run the suite
type: shell

```
uv run pytest
```

## Review this diff
type: prompt
keys: Ctrl+Alt+R
detail: adversarial review

```
Review the working diff. Focus on correctness and edge cases, not style.
List findings as: file:line — what breaks — how to fix. No praise.
```

## Fleet view
type: layout
keys: Alt+Y

```
Agents
```

## Save this layout
type: command
keys: Alt+L

```
layout.save
```
````

## When something is wrong

Nothing crashes and nothing else is lost: the bad entry is skipped, the rest load, and
the app toasts the first problem with a count of the others. Common ones:

| Message | Cause |
|---|---|
| `no fenced code block` | heading with no ``` block under it |
| `unterminated code fence` | a ``` that never closes — everything below it is swallowed |
| `unknown type 'x'` | `type:` is not `shell`, `prompt`, `layout` or `command` |
| `shell body must be a single line` | see the safety rule above |
| `shell body must be printable text` | the snippet carries a control byte (tab, `Esc`, …) |
| `layout body must be a single line naming a layout` | a `layout` body is one name, nothing else |
| `layout name longer than 60 characters` | no layout can be called that |
| `command body must be a single line naming a command` | a `command` body is one id, nothing else |
| `chord … must include Alt` | `Alt` is the only modifier a file may bind |
| `chord … is a built-in shortcut` | pick another chord; built-ins win |
| `duplicate name in this file` | two `##` headings with the same text |

A `command` entry naming an unknown command, or one that opens a folder, moves the
workspace, or deletes a saved layout, loads fine but shows its refusal when you run it
(`no command "…"`, or
`"…" cannot be bound from a shortcuts file`) — the file is never the place those decide
what runs, so the row is inert rather than silently missing.
