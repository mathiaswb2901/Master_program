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

        The scan stops the instant it has ``max_results`` hits and reports
        ``truncated`` — so a common word over a large tree costs bounded work and
        the caller is told the window was not the whole of it. An empty ``files``
        is the real "no matches" answer.
        """
        root = self._workspace.root
        needle = request.query if request.case_sensitive else request.query.lower()
        remaining = request.max_results
        truncated = False
        files: list[FileMatches] = []

        for path in self._walk(root):
            if remaining <= 0:
                # There is at least one more file to look at, so the cap cut the
                # results even if this file had no hits — err toward honest.
                truncated = True
                break
            hits, more = self._scan_file(path, needle, request.case_sensitive, remaining)
            if hits:
                files.append(FileMatches(path=self._workspace.relative(path), hits=hits))
                remaining -= len(hits)
            if more:
                truncated = True
                break

        files.sort(key=lambda f: f.path)
        total = sum(len(f.hits) for f in files)
        return SearchResponse(
            query=request.query,
            files=files,
            total_hits=total,
            files_with_matches=len(files),
            truncated=truncated,
        )

    def _walk(self, root: Path) -> "list[Path]":
        """Every visible file under the root, ignored directories pruned.

        Pruning happens in :data:`os.walk`'s ``dirnames`` in place, so an ignored
        directory's whole subtree is never descended — the same shape the tree's
        ``list_dir`` gives one level at a time, done depth-first here. Sorted so a
        query is deterministic run to run.
        """
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = sorted(name for name in dirnames if not is_ignored_dir(here / name))
            for name in sorted(filenames):
                found.append(here / name)
        return found

    def _scan_file(
        self,
        path: Path,
        needle: str,
        case_sensitive: bool,
        remaining: int,
    ) -> "tuple[list[SearchHit], bool]":
        """Hits in one file, up to ``remaining``; second value is "there were more".

        Returns ``([], False)`` for a binary, unreadable or oversized file — the
        same files the editor refuses — so a query never reads a blob or a huge log.
        """
        try:
            if path.is_symlink() or not path.is_file():
                return [], False
            if path.stat().st_size > MAX_TEXT_FILE_BYTES:
                return [], False
            data = path.read_bytes()
        except OSError:
            return [], False
        if b"\x00" in data[:_SNIFF_BYTES]:
            return [], False

        text = data.decode("utf-8", errors="replace")
        hits: list[SearchHit] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            hay = line if case_sensitive else line.lower()
            col = hay.find(needle)
            if col < 0:
                continue
            if len(hits) >= remaining:
                return hits, True
            clipped = line[:MAX_LINE_CHARS]
            hits.append(
                SearchHit(
                    line=lineno,
                    col=col,
                    text=clipped,
                    line_truncated=len(line) > MAX_LINE_CHARS,
                )
            )
        return hits, False
