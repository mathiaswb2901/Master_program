"""Named working sessions — the manifest that makes a session detachable.

tmux's real trick is that the *session* outlives the client: you detach, the
work keeps running, and you re-attach later. Workbench already has the pieces —
layouts persist per workspace, the backend outlives the window, agent sessions
are server-side, the worktree pool holds a slot under a lease — but nothing that
ties them into one named thing a user can leave and return to. A
:class:`NamedSession` is that thing: a thin *manifest*, not a copy of any live
state. It names a workspace, an arrangement, and references to the live agents,
terminals and leases that belong to it; the resources themselves stay where they
live (server-side or in the pool), keyed by their own ids.

This is the PR2 persistence surface only — the store and its REST shape, no UI.
It deliberately mirrors ``models/workspaces.py``'s recents file: a list of your
working sessions is data about *you*, not about any one project (and one project
can host more than one session), so it lives under the machine's app data dir,
version-stamped, and losing it costs the list and never the server.

The ``arrangement`` is dockview's own ``api.toJSON()`` output, stored verbatim
as ``JsonValue`` exactly as ``models/layouts.py`` stores a layout — the server
does not interpret it, and the rules about which panels may appear are a client
fact (the tool registry). The whole document rides every response and is capped
by :data:`MAX_FILE_BYTES`, so a runaway arrangement cannot become a runaway
payload.
"""

from pydantic import BaseModel, Field, JsonValue

#: How many named sessions the store keeps. Sized like the neighbours
#: (`MAX_SAVED_LAYOUTS`): long enough to cover the working sessions a person
#: actually keeps around, short enough that the list is one you read.
MAX_NAMED_SESSIONS = 24

#: Longest a session name may be — same ceiling as a saved layout's name.
MAX_NAME_CHARS = 60

#: Ceiling on the whole ``sessions.json`` document. It holds at most
#: `MAX_NAMED_SESSIONS` manifests, each carrying one arrangement blob, so
#: anything larger is not a file this wrote. Matches `layouts.MAX_FILE_BYTES`.
MAX_FILE_BYTES = 512 * 1024

#: Bumped when the on-disk shape changes. A document from a version this one does
#: not understand is discarded, exactly as `services/workspaces.py` and
#: `services/layouts.py` do: losing a *list* costs a rebuild, guessing at a shape
#: this code does not understand costs worse.
SESSIONS_VERSION = 1


class AgentRef(BaseModel):
    """A live agent this session owns, by reference — never a copy of it.

    The SDK session lives server-side keyed by ``sdk_session_id``; the pane that
    views it lives in the arrangement keyed by ``pane_key``. This record only
    says the two belong together in this named session, so a re-attach can point
    the restored pane back at the still-running agent.
    """

    #: The dockview panel id of the pane that views this agent — the same
    #: instance id the arrangement carries. What re-attach reconnects.
    pane_key: str
    #: The server-side SDK session id the pane is a view onto. The resource; this
    #: manifest does not own it and closing the pane does not end it.
    sdk_session_id: str
    #: Workspace-relative folder the agent is bound to ("" = the root).
    folder: str


class LeaseRef(BaseModel):
    """A worktree lease this session holds, by reference.

    The slot and the lease live in the pool (``services/worktrees.py``); this
    record only remembers which lease belongs to this session so re-attach can
    find the checkout the work was happening in.
    """

    #: The pool slot the lease is on (``slot-01``).
    slot: str
    #: The lease token that proves this session holds the slot.
    lease_id: str


class NamedSession(BaseModel):
    """One session a user can name, detach from, and return to.

    A manifest, not a container: it ties a workspace and an arrangement to the
    live resources (agents, leases) that belong together, all held by reference.
    """

    #: Stable identity. Assigned by the server on create; the key every endpoint
    #: takes and the arrangement's panes are filed under.
    id: str
    #: What the user calls it. Bounded like a saved layout's name.
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    #: Absolute path of the workspace this session belongs to, in the OS's own
    #: form — the same shape ``models/workspaces.py`` puts paths on the wire in,
    #: because a human reads it. The store is queried *by* this: a window on one
    #: project asks for the sessions that belong to it.
    workspace: str
    #: dockview's ``api.toJSON()`` arrangement, stored verbatim and never
    #: interpreted server-side (see the module note). ``None`` = no arrangement
    #: captured yet.
    arrangement: JsonValue = None
    #: The live agents this session owns, by reference.
    agents: list[AgentRef] = Field(default_factory=list)
    #: The worktree leases this session holds, by reference.
    leases: list[LeaseRef] = Field(default_factory=list)
    #: When it was first created, unix seconds.
    created_at: float
    #: When it was last attached to, unix seconds. Ordering comes off this — most
    #: recently used first, like the recents list.
    last_attached_at: float


class SessionsFile(BaseModel):
    """``<app data>/sessions.json`` — the user's sessions, not a project's."""

    version: int = SESSIONS_VERSION
    sessions: list[NamedSession] = Field(default_factory=list)


class CreateNamedSessionRequest(BaseModel):
    """POST body: name it, root it, and optionally seed it with an arrangement.

    The server assigns ``id``, ``created_at`` and ``last_attached_at`` — a client
    does not get to forge identity or claim a session was attached before it
    existed.
    """

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    workspace: str = Field(min_length=1)
    arrangement: JsonValue = None
    agents: list[AgentRef] = Field(default_factory=list)
    leases: list[LeaseRef] = Field(default_factory=list)


class UpdateNamedSessionRequest(BaseModel):
    """PUT body: the whole manifest as it should now be, minus the fields the
    server owns. The UI holds the session and writes it back entire, so
    rename/re-arrange/re-lease is one idempotent call rather than four endpoints.

    ``last_attached_at`` is stamped by the server on every update (an update *is*
    an attach), and ``id``/``created_at`` are immutable — none of the three is
    accepted from the client.
    """

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    workspace: str = Field(min_length=1)
    arrangement: JsonValue = None
    agents: list[AgentRef] = Field(default_factory=list)
    leases: list[LeaseRef] = Field(default_factory=list)


class SessionsResponse(BaseModel):
    """What GET returns: the sessions for the asked-about workspace, and why the
    list might be empty.

    ``problem`` is non-null when the file on disk could not be used — unreadable,
    oversized, not JSON, wrong version, or not this shape. The list is then empty
    *and* the UI can say so, which is the difference between a lost file and a
    user who never had a session.
    """

    sessions: list[NamedSession] = Field(default_factory=list)
    #: Why the sessions file was ignored, if it was. Never fatal — see the store.
    problem: str | None = None
