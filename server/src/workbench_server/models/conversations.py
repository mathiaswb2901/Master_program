"""The conversation browser's payloads: every Claude chat, grouped by folder.

Read-only mirror of Claude Code's own storage. Nothing in this module describes
a write, because there is none — the browser reads
``~/.claude/projects/<encoded-cwd>/*.jsonl`` and never touches it.

Every field that could be a guess is instead two fields: what we *know* (the
encoded project key, always true) and what we *resolved* (a real directory, or
nothing). Claude Code's encoding replaces every non-alphanumeric with ``-`` and
is therefore **not reversible** — ``C:\\a\\b`` and ``C:/a-b`` encode to the same
key — so a folder name is either matched against a directory that exists or
shown as the raw key. A guessed path would be a lie a user cannot check.
"""

from pydantic import BaseModel, Field


class ConversationInfo(BaseModel):
    """One transcript on disk, as a row."""

    session_id: str
    #: Derived by ``services/titles.py`` — the same function live sessions use,
    #: so a conversation does not visibly retitle when it stops being live.
    title: str
    #: Transcript mtime (unix seconds): when the conversation was last active.
    updated_at: float
    #: User messages carrying text. Tool results are stored as user records and
    #: are deliberately not counted (see ``TranscriptFacts.turns``).
    turns: int
    #: The scan hit its byte budget, so ``turns`` is a floor. Rendered as "120+".
    turns_capped: bool = False
    #: This transcript was read for its title and turn count. False when it fell
    #: outside the response's read budget (``limit``): the conversation exists
    #: and is listed — every one always is — but ``title`` is a placeholder and
    #: ``turns`` is 0 until a wider ``limit`` reads it. Listing is cheap and
    #: reading is not, and the row says which of the two it got.
    read: bool = True
    #: Local id of the live session in this process that continues this
    #: transcript, or None. Resuming an already-resumed conversation would fork
    #: it into a second session; with this the browser focuses the pane the
    #: conversation is already open in.
    live_session_id: str | None = None
    #: Why this row is not fully knowable — unreadable file, no parseable
    #: messages. Never a reason to drop the row: the conversation exists.
    problem: str | None = None


class ProjectGroup(BaseModel):
    """One folder's conversations — a directory of Claude Code's storage."""

    #: The encoded directory name. Always true, never a guess; shown verbatim
    #: when nothing on disk matches it.
    key: str
    #: What to show for this folder: the resolved path when there is one, the
    #: encoded key when there is not.
    label: str
    #: A real directory matched ``key``. False = ``folder`` is None and ``label``
    #: is the raw key.
    resolved: bool
    #: The matched directory, absolute, or None.
    folder: str | None = None
    #: Workspace-relative path when the folder is inside this workspace
    #: (``""`` = the workspace root); None when it is outside or unresolved.
    workspace_relative: str | None = None
    #: Whether a row here can be opened *now*. False is not a hidden row — it is
    #: a visible row with ``reason`` on it (half B of M5 item 12 lifts the jail).
    openable: bool
    reason: str | None = None
    #: Newest conversation in this group, for ordering. 0 when it is empty.
    updated_at: float = 0.0
    conversations: list[ConversationInfo] = Field(default_factory=list)


class ConversationStore(BaseModel):
    """Everything the browser read in one pass, and what it did not read.

    ``total_conversations`` counts what *exists* (an `os.scandir` per project
    directory — cheap), while ``returned_conversations`` counts what was read in
    full for its title and turn count (one pass per transcript — not cheap). The
    difference is the honest part: a store bigger than ``limit`` says so, and
    names the argument that widens the window rather than silently showing a
    prefix.

    ``limit`` bounds only that second number. **Every** conversation that exists
    is listed, in the folder it ran in, whatever the limit is — the unread ones
    carry ``read=False`` rather than being left out. Dropping rows would drop
    whole folders with them (a project whose conversations are all older than
    the newest N would disappear entirely), and a browser that silently omits a
    folder is worse than one that admits it has not read it yet.
    """

    projects: list[ProjectGroup] = Field(default_factory=list)
    #: Where these came from, so an empty result can say where it looked.
    projects_root: str
    root_exists: bool
    #: Folders in the store. Always ``len(projects)`` — the two disagreeing was
    #: the shape of a bug (a folder counted here but never sent), so they are now
    #: the same number by construction rather than by coincidence.
    total_projects: int = 0
    total_conversations: int = 0
    returned_conversations: int = 0
    #: The cap this response was built under (``?limit=``).
    limit: int = 0
    #: When the scan ran (unix seconds). There is no watcher on somebody else's
    #: storage, so the panel says how old the reading is and offers a refresh
    #: rather than implying it is live.
    scanned_at: float = 0.0
