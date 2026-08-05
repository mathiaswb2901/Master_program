"""Layout persistence schemas.

A *layout* is dockview's own serialized arrangement (`api.toJSON()`). The server
deliberately does not interpret it: it is stored verbatim as ``JsonValue`` in
``<workspace>/.workbench/layouts.json`` and handed back untouched. Two reasons —
the shape belongs to a UI library this process does not import, and every rule
about which panels may appear in it is a *client* fact (the tool registry), so a
server-side schema would be a second authority that goes stale the moment a tool
is added. Restore-time validation lives where the registry is: ``ui/src/layouts.ts``.

The file lives under ``.workbench/`` — the workspace's own data directory, next
to ``shortcuts.md`` and ``scratch.md``, gitignored by repo convention — which is
what makes an arrangement *per project* rather than per browser origin.
"""

from pydantic import BaseModel, Field, JsonValue

#: Ceilings. The whole document rides every response, and it is written from an
#: autosave on every drag, so a runaway file must not become a runaway payload.
MAX_NAME_CHARS = 60
MAX_SAVED_LAYOUTS = 24
MAX_FILE_BYTES = 512 * 1024


class NamedLayout(BaseModel):
    """One arrangement the user saved under a name."""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    state: JsonValue


class LayoutsState(BaseModel):
    """Everything persisted for this workspace. Also the PUT body: the UI owns
    the list and writes the whole document, so save/rename/delete are one
    idempotent call rather than four endpoints with names in the URL."""

    #: The live arrangement, restored on the next load. None = never saved.
    current: JsonValue = None
    #: Which named layout ``current`` came from, purely so the window can still
    #: say "Review" after a restart. Advisory: the arrangement is ``current``.
    current_name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    saved: list[NamedLayout] = Field(default_factory=list, max_length=MAX_SAVED_LAYOUTS)


class LayoutsResponse(BaseModel):
    """What GET returns: the state, and why it might be empty.

    ``problem`` is non-null when the file on disk could not be used — unreadable,
    oversized, not JSON, or not this shape. The state is then the empty one, so
    the UI falls back to the default layout *and* can say so, which is the whole
    difference between a stale file and a blank window.
    """

    state: LayoutsState
    problem: str | None = None
