# shortcuts.md

Your personal shortcuts, in one markdown file you write like a note. Every entry shows
up in the QuickBar (`Ctrl+Shift+P`), can carry its own keybinding, and either inserts a
shell snippet or a prompt template into whatever you are working in, or switches the
window to one of your saved layouts.

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
  the one kind that acts rather than inserts, and the reason it may is that moving panels
  is the only thing it can do: no text reaches a shell or an agent, no file is touched,
  and a name you never saved is a message, not a guess. Its body is one line, no longer
  than a layout name — it cannot carry a payload.
- The QuickBar shows a shell entry's **actual snippet** next to its name, and a layout
  entry's **actual layout**, not the `detail:` line from the file, so a row cannot
  describe itself as one thing and do another.

There is no `run: true` option and there will not be one. Opening someone else's
workspace can add rows to your QuickBar and rearrange your panels; it cannot run a
command, send a prompt, or read a file.

## Format

An `##` heading names the shortcut. Optional `key: value` lines configure it. A fenced
code block is the body. Anything else in the file — a title, prose, notes — is ignored.

| Key | Meaning |
|---|---|
| `type` | `shell` (default), `prompt` or `layout`. A fence tagged with a kind selects it too — ` ```prompt `, ` ```layout ` — while ` ```powershell ` stays a shell body. |
| `keys` | One chord, e.g. `Alt+G`. **Must include `Alt`.** Optional. |
| `detail` | One line shown next to the name in the QuickBar — `prompt` entries only; `shell` and `layout` rows show what they will actually do instead. Optional. |

A `layout` body is the name of a layout: one of the built-ins (`Default`, `Review`,
`Focus`, `Agents`) or one you saved yourself from the layout chip in the status bar.
Names are matched without case.

`Alt` is the only modifier a shortcuts file may take, and it is the one that works:
plain keys are never intercepted, and inside the terminal or editor only `Alt` and
`Ctrl+Shift` chords reach Workbench (`DESIGN.md` §6.8). Everywhere else Workbench takes
*any* `Ctrl` chord, which is why a file cannot have one — `Ctrl+V` would stop pasting
into the chat box. Built-in bindings win too: ask for `Alt+T` and the entry keeps its
QuickBar row, loses the chord, and says so.

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
````

## When something is wrong

Nothing crashes and nothing else is lost: the bad entry is skipped, the rest load, and
the app toasts the first problem with a count of the others. Common ones:

| Message | Cause |
|---|---|
| `no fenced code block` | heading with no ``` block under it |
| `unterminated code fence` | a ``` that never closes — everything below it is swallowed |
| `unknown type 'x'` | `type:` is not `shell`, `prompt` or `layout` |
| `shell body must be a single line` | see the safety rule above |
| `shell body must be printable text` | the snippet carries a control byte (tab, `Esc`, …) |
| `layout body must be a single line naming a layout` | a `layout` body is one name, nothing else |
| `layout name longer than 60 characters` | no layout can be called that |
| `chord … must include Alt` | `Alt` is the only modifier a file may bind |
| `chord … is a built-in shortcut` | pick another chord; built-ins win |
| `duplicate name in this file` | two `##` headings with the same text |
