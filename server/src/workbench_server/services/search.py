"""Workspace-wide content search — a bounded walk that respects the tree's rules.

The repo ships no ripgrep and reaches for no new dependency, so this is a bounded
:func:`os.walk` over the jailed workspace, reusing the *exact* visibility rule the
file tree and the watcher use (:mod:`workbench_server.services.ignore`): the same
``IGNORED_DIRS`` names, and directories that tag themselves disposable with
``CACHEDIR.TAG``. Search and the tree therefore agree by construction — a query
never surfaces a match inside a build cache the tree is hiding, which would be the
"two panels disagree about what exists" failure a second copy of the rule buys.

.gitignore is deliberately *not* consulted: nothing in the codebase parses it (the
tree does not, the watcher does not), so honouring it here would make search hide
files the tree still shows — the same divergence, from the other side. If a
gitignore-aware walk is wanted later it belongs in ``ignore.py``, applied to the
tree and the watcher and search at once, not bolted onto this one caller.

Windows-first: paths via :mod:`pathlib`, results as workspace-relative forward-slash
strings. Binary and oversized files are skipped with the same test the editor's
``read_text`` uses, so a query never reads a gigabyte or scans a null-filled blob.
"""

import os
from collections.abc import Iterator
from pathlib import Path

from workbench_server.models.files import MAX_TEXT_FILE_BYTES
from workbench_server.models.search import (
    MAX_LINE_CHARS,
    FileMatches,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from workbench_server.services.ignore import is_ignored_dir
from workbench_server.services.workspace import Workspace

#: Bytes read from the head of a file to decide "is this text". The same window
#: ``Workspace.read_text`` sniffs for a NUL — a binary file almost always has one
#: in its first pages, and a file that does not is cheap to scan in full.
_SNIFF_BYTES = 8_192

#: Characters of the line kept *before* the match when a long line is windowed.
#: Enough to see what the match sits in (an assignment, a key, a column header)
#: without spending the budget on lead-in — the match itself is what was asked for.
_CLIP_LEAD = 40

#: Marks a windowed line whose head was elided, so the caller can tell "the line
#: starts here" from "the line starts further left".
_ELLIPSIS = "…"


class SearchService:
    """Content search over the live workspace jail.

    Holds the :class:`Workspace` object rather than a copy of its root, so a
    workspace switch re-roots search with everything else (the ``Workspace`` is
    mutated in place — ``services/workspaces.py``) and this service owes no
    ``set_workspace_root`` of its own.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def search(self, request: SearchRequest) -> SearchResponse:
        """Walk the workspace and return hits grouped by file, capped and honest.

        The scan looks for exactly *one hit past* ``max_results`` and stops there:
        that one extra hit is what proves ``truncated``, so the flag is never a
        guess about a file that might have matched. The walk is lazy — files are
        enumerated as they are scanned, so a small ``max_results`` over a large
        tree never pays for a full recursive listing. An empty ``files`` is the
        real "no matches" answer.
        """
        root = self._workspace.root
        needle = request.query if request.case_sensitive else request.query.lower()
        limit = request.max_results
        # One past the cap: enough to *know* there is more, never enough to be a
        # second page's worth of work.
        budget = limit + 1
        found = 0
        files: list[FileMatches] = []

        for path in self._walk(root):
            hits = self._scan_file(path, needle, request.case_sensitive, budget - found)
            if hits:
                files.append(FileMatches(path=self._workspace.relative(path), hits=hits))
                found += len(hits)
            if found >= budget:
                break

        truncated = found > limit
        if truncated:
            # Drop the probe hit so the caller gets exactly ``limit`` back; the
            # file it came from disappears with it if it carried nothing else.
            last = files[-1]
            kept = last.hits[: len(last.hits) - (found - limit)]
            if kept:
                files[-1] = FileMatches(path=last.path, hits=kept)
            else:
                files.pop()

        files.sort(key=lambda f: f.path)
        total = sum(len(f.hits) for f in files)
        return SearchResponse(
            query=request.query,
            files=files,
            total_hits=total,
            files_with_matches=len(files),
            truncated=truncated,
        )

    def _walk(self, root: Path) -> Iterator[Path]:
        """Yield every visible file under the root, ignored directories pruned.

        A *generator*, not a list: :func:`search` stops as soon as its budget is
        spent, and materialising the tree first would make a one-hit query pay for
        a full recursive enumeration of the workspace no matter how early it found
        its answer.

        Pruning happens in :data:`os.walk`'s ``dirnames`` in place, so an ignored
        directory's whole subtree is never descended — the same shape the tree's
        ``list_dir`` gives one level at a time, done depth-first here. Both
        ``dirnames`` and ``filenames`` are sorted before they are yielded, so the
        order a query sees (and therefore which hits survive the cap) is
        deterministic run to run.
        """
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = sorted(name for name in dirnames if not is_ignored_dir(here / name))
            for name in sorted(filenames):
                yield here / name

    def _scan_file(
        self,
        path: Path,
        needle: str,
        case_sensitive: bool,
        remaining: int,
    ) -> list[SearchHit]:
        """Hits in one file, at most ``remaining`` of them.

        Returns ``[]`` for a binary, unreadable or oversized file — the same files
        the editor refuses — so a query never reads a blob or a huge log.
        """
        try:
            if path.is_symlink() or not path.is_file():
                return []
            if path.stat().st_size > MAX_TEXT_FILE_BYTES:
                return []
            data = path.read_bytes()
        except OSError:
            return []
        if b"\x00" in data[:_SNIFF_BYTES]:
            return []

        text = data.decode("utf-8", errors="replace")
        hits: list[SearchHit] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            hay = line if case_sensitive else line.lower()
            col = hay.find(needle)
            if col < 0:
                continue
            clipped, clipped_col = _clip_around(line, col)
            hits.append(
                SearchHit(
                    line=lineno,
                    col=clipped_col,
                    text=clipped,
                    line_truncated=len(line) > MAX_LINE_CHARS,
                )
            )
            if len(hits) >= remaining:
                break
        return hits


def _clip_around(line: str, col: int) -> tuple[str, int]:
    """Clip a long line to a window that *contains* the match, and remap ``col``.

    A minified bundle is one line tens of kilobytes long; clipping it from the
    start would return a hit whose text does not contain what was matched, which
    is worse than no preview at all. So the window is placed around the match:
    ``_CLIP_LEAD`` characters of lead-in where there is room, an ``…`` where the
    head was elided, and the returned column points at the match *within the
    returned text* — which is exactly what the UI's ``<mark>`` slices on.
    """
    if len(line) <= MAX_LINE_CHARS:
        return line, col
    if col <= _CLIP_LEAD:
        return line[:MAX_LINE_CHARS], col
    # Room for the ellipsis, and never a window that runs past the line's end.
    body = MAX_LINE_CHARS - len(_ELLIPSIS)
    start = min(col - _CLIP_LEAD, len(line) - body)
    return _ELLIPSIS + line[start : start + body], col - start + len(_ELLIPSIS)
