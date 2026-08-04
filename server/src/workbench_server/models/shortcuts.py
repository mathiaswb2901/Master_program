"""shortcuts.md schemas.

One markdown file per scope — ``<workspace>/.workbench/shortcuts.md`` merged over
``~/.workbench/shortcuts.md`` — drives QuickBar entries, terminal snippets, chat
prompt templates and custom keybindings.

Security: an entry is *inserted* into a surface, never executed. There is no
"run" field by design, so opening an untrusted workspace can never make a
shortcut do anything on its own (see ``services/shortcuts.py`` and
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

ShortcutKind = Literal["shell", "prompt"]
ShortcutSource = Literal["workspace", "global"]


class ShortcutEntry(BaseModel):
    name: str
    kind: ShortcutKind
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
