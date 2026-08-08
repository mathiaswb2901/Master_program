"""Workspace content search — the request, and the results grouped by file.

The one wire contract for ``POST /api/search`` and the ``workspace_search`` agent
tool, mirrored in ``ui/src/types.ts``. Text is found across the workspace's files,
respecting the same visibility rules the file tree uses (``services/ignore.py`` —
``IGNORED_DIRS`` plus ``CACHEDIR.TAG``), so search never surfaces a build cache the
tree hides.

The result carries the AXI truncation shape by construction: ``truncated`` says the
cap was hit and there are more, and ``max_results`` is the argument that widens the
window. An empty ``files`` list is the honest "no matches" answer, never blankness.
"""

from pydantic import BaseModel, Field

#: Hits returned by default, and the ceiling a caller may ask for. The scan stops
#: the moment it has this many, so a workspace-wide query for a common word costs
#: bounded work rather than reading every match in the tree.
DEFAULT_MAX_HITS = 200
MAX_HITS = 2_000

#: One matching line is clipped to this many characters before it reaches the wire:
#: a minified bundle or a data row can be tens of kilobytes on one line, and the
#: match is legible in far less. The clip is stated where the UI can show it.
MAX_LINE_CHARS = 400


class SearchRequest(BaseModel):
    """What to look for, and how much to bring back."""

    #: The literal text to find. Substring match, not a regex — a plain query is
    #: what an analyst types, and it cannot become a runaway pattern over the tree.
    query: str = Field(min_length=1)
    #: Cap on hits across all files; the scan stops here and sets ``truncated``.
    max_results: int = Field(default=DEFAULT_MAX_HITS, ge=1, le=MAX_HITS)
    #: Default is case-insensitive: it is what "find SE3" almost always means.
    case_sensitive: bool = False


class SearchHit(BaseModel):
    """One matching line in one file."""

    #: 1-based line number, the number the editor jumps to.
    line: int
    #: 0-based character offset of the first match on the line, for highlighting.
    col: int
    #: The matching line with its trailing newline stripped, clipped to
    #: ``MAX_LINE_CHARS``; ``line_truncated`` says whether it was cut.
    text: str
    #: True when the line was longer than ``MAX_LINE_CHARS`` and ``text`` is a prefix.
    line_truncated: bool = False


class FileMatches(BaseModel):
    """Every returned hit in one file, in line order."""

    #: Workspace-relative, forward slashes — the path the editor opens.
    path: str
    hits: list[SearchHit]


class SearchResponse(BaseModel):
    """Hits grouped by file, plus the honest truncation/empty signals."""

    #: Echoed back so a stale response can be told from the current query.
    query: str
    #: Files with at least one hit, in path order. Empty means no matches.
    files: list[FileMatches]
    #: Total hits returned across every file (``<= max_results``).
    total_hits: int
    #: Number of files that carried a hit — ``len(files)``, stated for the reader.
    files_with_matches: int
    #: True when the scan stopped at ``max_results`` and more matches exist. The
    #: way to widen the window is a larger ``max_results`` or a narrower query.
    truncated: bool
