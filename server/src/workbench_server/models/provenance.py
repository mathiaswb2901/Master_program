"""Provenance schemas: who last changed a workspace file.

An entry is a claim about the user's own files, so the schema keeps the honest
answer expressible. ``agent is None`` means "not attributable to any session" —
the user, another editor, a git checkout — and is never a guess at the most
recent session. See ``services/provenance.py`` for what the heuristic can and
cannot know.
"""

from typing import Literal

from pydantic import BaseModel, Field


class AgentAttribution(BaseModel):
    """The session a file change was attributed to, and the tool that did it."""

    session_id: str
    session_title: str
    #: The file-writing tool that named the path (``Write``/``Edit``/…).
    tool: str


class ProvenanceEntry(BaseModel):
    """What is known about the last change to one path."""

    path: str  # workspace-relative POSIX, exactly as the watcher reports it
    changed_at: float  # unix seconds
    #: None = unattributed. Not "unknown agent" — nobody claimed this change.
    agent: AgentAttribution | None = None
    #: The user has opened or dismissed this change; the tree marker clears.
    acknowledged: bool = False


class FileProvenanceEvent(BaseModel):
    """Broadcast on /ws/events when a path's provenance changes.

    ``entry.agent is None`` means "no agent claim on this path any more": the
    user or something outside Workbench wrote it. Clients drop the path from
    their map rather than keep showing a claim that is no longer true.
    """

    type: Literal["file_provenance"] = "file_provenance"
    entry: ProvenanceEntry


class ProvenanceMap(BaseModel):
    """GET /api/provenance — every path currently attributed to a session.

    In-memory only: a server restart returns an empty map (see ARCHITECTURE.md).
    """

    entries: list[ProvenanceEntry] = Field(default_factory=list)


class AcknowledgeRequest(BaseModel):
    """POST /api/provenance/acknowledge — the user has seen this change."""

    path: str
