"""shortcuts.md schemas.

One markdown file per scope — ``<workspace>/.workbench/shortcuts.md`` merged over
``~/.workbench/shortcuts.md`` — drives QuickBar entries, terminal snippets, chat
prompt templates and custom keybindings.

Security: an entry never executes anything a workspace file could weaponize.
``shell`` and ``prompt`` entries are *inserted* into a surface; ``layout`` names
one of the user's own saved arrangements and can do nothing but rearrange panels;
``command`` names a *registered* command id and the UI runs it through the
registry — but a command that reaches a filesystem path or re-points the path
jail declares itself unbindable from a file (``ui/src/registry.ts``), and the UI
refuses an entry naming an unsafe or unknown command. There is no "run" field by
design, so opening an untrusted workspace can never make a shortcut send a
prompt, type a shell line, or reach a file (see ``services/shortcuts.py`` and
``docs/shortcuts.md``).
"""

from typing import Literal

from pydantic import BaseModel

# Payload ceilings. The whole state rides every /api/shortcuts response, so a
# runaway file must not become a runaway payload.
MAX_NAME_CHARS = 60
MAX_DETAIL_CHARS = 80
MAX_BODY_CHARS = 4000
MAX_FILE_BYTES = 256 * 1024
# A command id is a short dotted token (``terminal.new``); the body of a
# ``command`` entry is one, bounded so a file cannot smuggle a payload in it.
MAX_COMMAND_ID_CHARS = 100

ShortcutKind = Literal["shell", "prompt", "layout", "command"]
ShortcutSource = Literal["workspace", "global"]


class ShortcutEntry(BaseModel):
    name: str
    kind: ShortcutKind
    # shell/prompt: the text that is inserted. layout: the name of the layout.
    # command: the id of a registered command the UI resolves and runs.
    body: str
    # A single chord ("Alt+G"); None = reachable from the QuickBar only.
    keys: str | None = None
    detail: str | None = None
    source: ShortcutSource


class ShortcutProblem(BaseModel):
    """One thing the parser could not use. Never fatal: the rest still loads."""

    file: str  # display label of the file it came from
    message: str


class ShortcutsState(BaseModel):
    entries: list[ShortcutEntry]
    problems: list[ShortcutProblem]


class ShortcutsChangedEvent(BaseModel):
    """Broadcast on /ws/events after a shortcuts file loads differently than before."""

    type: Literal["shortcuts_changed"] = "shortcuts_changed"
    entry_count: int
    problem_count: int
