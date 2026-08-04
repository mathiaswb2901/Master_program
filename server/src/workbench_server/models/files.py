"""File API schemas. All paths cross the wire as workspace-relative POSIX strings."""

from typing import Literal

from pydantic import BaseModel, Field


class TreeNode(BaseModel):
    name: str
    path: str  # workspace-relative, forward slashes
    kind: Literal["file", "dir"]
    children: list["TreeNode"] | None = None  # None for files, list (possibly empty) for dirs


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
    """Broadcast on /ws/events for every observed filesystem change."""

    type: Literal["file_changed"] = "file_changed"
    path: str
    change: Literal["added", "modified", "deleted"]
    hash: str | None = None  # None for deletions and unreadable/binary-too-large files
    origin: Literal["watcher"] = "watcher"


MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024


class ApiError(BaseModel):
    detail: str = Field(description="Human-readable error cause")
