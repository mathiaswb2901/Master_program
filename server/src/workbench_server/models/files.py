"""File API schemas. All paths cross the wire as workspace-relative POSIX strings."""

from typing import Literal

from pydantic import BaseModel, Field


class TreeNode(BaseModel):
    """The whole workspace in one payload — the *search index*, not the file tree.

    `GET /api/files/tree` is a full recursive walk (471 KB of JSON on the perf
    fixture), so nothing that renders per-directory uses it any more: the tree
    panel reads `DirListing` one level at a time. What still needs every path at
    once is the QuickBar's fuzzy file search and the chat's tool-row file links,
    and the UI fetches this on demand for them rather than on a timer or on
    every watcher event.
    """

    name: str
    path: str  # workspace-relative, forward slashes
    kind: Literal["file", "dir"]
    children: list["TreeNode"] | None = None  # None for files, list (possibly empty) for dirs


class DirEntry(BaseModel):
    """One visible child of one directory.

    Deliberately childless. A directory entry says *that* it is a directory and
    nothing about what is inside it — that is the whole difference between a
    tree the server walks and a tree the client asks about one level at a time.
    """

    name: str
    path: str  # workspace-relative, forward slashes
    kind: Literal["file", "dir"]


class DirListing(BaseModel):
    """`GET /api/files/dir` — exactly one directory listing, never a walk.

    Ordering is the tree's: directories first, then files, each by lowercased
    name. The client re-sorts inserted rows with the same rule, so a row that
    arrives from a watcher event lands where a refetch would have put it.
    """

    path: str  # "" is the workspace root
    name: str  # the directory's own name; the workspace folder's at the root
    entries: list[DirEntry]


class TreeInvalidatedEvent(BaseModel):
    """Every loaded listing is suspect — re-list what is open.

    Published when the *visibility rules* moved under the tree rather than a
    file: a `CACHEDIR.TAG` appearing makes a whole directory disappear from the
    tree, and no per-file event describes that (the events from inside it are
    precisely the ones now being suppressed). Rare, cheap, and the reason a
    client may patch its own tree from `FileChangedEvent` without ever drifting
    from what a fresh listing would say.
    """

    type: Literal["tree_invalidated"] = "tree_invalidated"
    reason: Literal["ignore_rules"] = "ignore_rules"


class FileContent(BaseModel):
    path: str
    content: str
    hash: str  # sha256 of the bytes on disk


class WriteRequest(BaseModel):
    path: str
    content: str
    # If set, the write is rejected when the on-disk hash no longer matches —
    # the editor's defense against silently clobbering an agent's concurrent edit.
    expected_hash: str | None = None


class WriteResponse(BaseModel):
    path: str
    hash: str


class CreateRequest(BaseModel):
    path: str
    kind: Literal["file", "dir"]


class RenameRequest(BaseModel):
    path: str
    new_path: str


class OkResponse(BaseModel):
    ok: Literal[True] = True


class FileChangedEvent(BaseModel):
    """Broadcast on /ws/events for every observed filesystem change.

    Directories appear here only when they are *added* or *deleted* — the two
    changes the tree has a row for. `hash` is null for both.
    """

    type: Literal["file_changed"] = "file_changed"
    path: str
    change: Literal["added", "modified", "deleted"]
    hash: str | None = None  # None for deletions and unreadable/binary-too-large files
    #: The path was a directory when the change was observed. A deletion cannot
    #: know (the path is gone), so it is False there and the client drops the
    #: row by path whatever it was.
    is_dir: bool = False
    origin: Literal["watcher"] = "watcher"


MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024


class ApiError(BaseModel):
    detail: str = Field(description="Human-readable error cause")
