# shortcuts.md

Your personal shortcuts, in one markdown file you write like a note. Every entry shows
up in the QuickBar (`Ctrl+Shift+P`), can carry its own keybinding, and inserts either a
shell snippet or a prompt template into whatever you are working in.

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
  read it and press Enter. Because a newline in a live terminal *is* Enter, a shell body
  must be a single line — a multi-line one is refused rather than half-executed.
- A `prompt` entry is **appended to the chat box** of the active agent session. You press
  Send.

There is no `run: true` option and there will not be one. Opening someone else's
workspace can add rows to your QuickBar; it cannot make anything happen.

## Format

An `##` heading names the shortcut. Optional `key: value` lines configure it. A fenced
code block is the body. Anything else in the file — a title, prose, notes — is ignored.

| Key | Meaning |
|---|---|
| `type` | `shell` (default) or `prompt`. A ` ```prompt ` fence also selects `prompt`. |
| `keys` | One chord, e.g. `Alt+G`. Must include `Ctrl` or `Alt`. Optional. |
| `detail` | One line shown next to the name in the QuickBar. Optional. |

Chords follow the app's pass-through policy (`DESIGN.md` §6.8): plain keys are never
intercepted, and inside the terminal or editor only `Alt` / `Ctrl+Shift` chords reach
Workbench — so `Alt+G` works everywhere, while `Ctrl+G` only works outside them.
Built-in bindings win: if you ask for `Ctrl+S`, the entry keeps its QuickBar row, loses
the chord, and says so.

### Copy-paste starter

````markdown
# My shortcuts

## Status board
type: shell
keys: Alt+G
detail: branch + short status

```
git status -sb
```

## Run the suite
type: shell
detail: pytest, quiet

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
````

## When something is wrong

Nothing crashes and nothing else is lost: the bad entry is skipped, the rest load, and
the app toasts the first problem with a count of the others. Common ones:

| Message | Cause |
|---|---|
| `no fenced code block` | heading with no ``` block under it |
| `unterminated code fence` | a ``` that never closes — everything below it is swallowed |
| `unknown type 'x'` | `type:` is neither `shell` nor `prompt` |
| `shell body must be a single line` | see the safety rule above |
| `chord … is a built-in shortcut` | pick another chord; built-ins win |
| `duplicate name in this file` | two `##` headings with the same text |
