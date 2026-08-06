"""Workspace switching: what the current root is, and where you have been.

Two payloads that look alike and are not. :class:`WorkspaceState` describes
*this server right now* — the root every path resolves against, whether the user
chose it or it defaulted to the launch directory, and the folders they can go
back to. :class:`RecentWorkspacesFile` is what is on disk under the machine's app
data dir: the user's own history, which belongs to the *user* and not to any one
project (see ``services/workspaces.py``).

Paths cross this wire as strings in the OS's own form (``C:\\work\\thing`` on
Windows), because they are shown to a human and typed back in by one. Everything
*inside* a workspace still travels as a workspace-relative POSIX path — that
distinction is the jail, and it does not move.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: How many folders the recent list keeps. Long enough to cover the projects a
#: person actually moves between in a week, short enough that the picker is a
#: list you read rather than one you search.
MAX_RECENT_WORKSPACES = 12

#: Ceiling on one path from the wire. Windows' own limit is 32,767 with long
#: paths enabled and 260 without; this is far above anything real and far below
#: anything worth resolving.
MAX_PATH_CHARS = 4096

#: Ceiling on the recents file. It holds at most `MAX_RECENT_WORKSPACES` short
#: records, so anything larger is not a file this wrote.
MAX_RECENTS_FILE_BYTES = 64 * 1024

#: Bumped when the on-disk shape changes. A document from a version this one
#: does not understand is discarded, exactly as `services/layouts.py` does:
#: losing a *history* costs one keystroke, and guessing at it costs a wrong root.
RECENTS_VERSION = 1


class WorkspaceRef(BaseModel):
    """One folder the user has had open."""

    #: Absolute, in the OS's own form. The only key: two refs are the same
    #: workspace when this matches.
    path: str
    #: The folder's own name, so a picker row reads as a project rather than a
    #: path. Derived server-side, never sent by the client.
    name: str
    #: When it was last made current, unix seconds. Ordering comes off this.
    opened_at: float
    #: False = the directory is not there any more (renamed, on a drive that is
    #: not mounted, deleted). The row stays — a list that silently forgets where
    #: you were is worse than one that says a folder is missing — but it is
    #: offered as unavailable rather than as something to switch to.
    exists: bool = True


class WorkspaceState(BaseModel):
    """The current root plus the history, as one read."""

    #: Absolute path of the workspace every relative path resolves against.
    root: str
    #: Its folder name — what the status chip and the window say.
    name: str
    #: True when the root was chosen (``WORKBENCH_WORKSPACE_ROOT``, or a switch
    #: in this session). False = nobody chose it and the server fell back to the
    #: directory it was launched from, which is the first-run case the UI has to
    #: *say* rather than quietly present as a decision.
    explicit: bool
    #: Most recent first, current root included (it is where you are, and it is
    #: also where you come back to).
    recents: list[WorkspaceRef] = Field(default_factory=list)
    #: Why the recents file was ignored, if it was. Never fatal — see the service.
    problem: str | None = None


class SwitchWorkspaceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)


class WorkspaceChangedEvent(BaseModel):
    """Bus event: the root moved under everyone.

    On ``/ws/events`` rather than only in the switch response, because the
    switch re-roots the *server*: a second window (or the same window's other
    sockets) is now looking at a tree, a layout file and a shortcuts file that
    belong to a different project, and nothing else on the wire would tell it.
    """

    type: Literal["workspace_changed"] = "workspace_changed"
    root: str
    name: str


class RecentWorkspacesFile(BaseModel):
    """``<app data>/workspaces.json`` — the user's history, not a project's."""

    version: int = RECENTS_VERSION
    recents: list[WorkspaceRef] = Field(default_factory=list)
