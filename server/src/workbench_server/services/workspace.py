"""Workspace file operations. Every path from the wire passes through `safe_path` —
the jail that keeps the server inside the workspace root."""

import hashlib
import os
import tempfile
from pathlib import Path

from workbench_server.models.files import (
    MAX_TEXT_FILE_BYTES,
    DirEntry,
    DirListing,
    TreeNode,
)
from workbench_server.services.ignore import is_hidden_dir, is_ignored_dir


class PathOutsideWorkspaceError(Exception):
    pass


class NotTextError(Exception):
    pass


class StaleWriteError(Exception):
    """expected_hash no longer matches the file on disk."""


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def safe_path(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PathOutsideWorkspaceError(relative)
        return candidate

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def tree(self) -> TreeNode:
        """The whole workspace, recursively. The *search index* — see `TreeNode`.

        Nothing that renders a directory calls this any more; `list_dir` does.
        """
        return self._node(self.root)

    def list_dir(self, relative: str) -> DirListing:
        """One directory's visible children — exactly one `os.scandir`, no walk.

        This is the shape the whole tree is now read in. `top_level_dirs()`
        (PR #35) was the first caller to need it; every expanded folder needs
        the same thing, and a tree read one level at a time is one that costs
        the user nothing for the 4,995 rows they are not looking at.

        Visibility is the ignore rule and nothing else, so this agrees with
        `tree()`, with the watcher and with the QuickBar's index by
        construction — two panels disagreeing about what exists is the failure
        mode a second copy of the rule would buy.

        Agreement has to cover the *requested* directory too, not only its
        children. `tree()` can never be asked for a hidden directory — the
        parent-level filter runs before it recurses — but this takes a path
        from the wire, so it is reachable by name. The case that reaches a real
        user: a folder is expanded, a build drops a `CACHEDIR.TAG` into it, the
        watcher publishes `tree_invalidated`, and the client re-lists every
        folder it has open — including that one. `is_hidden_dir` is the same
        answer `tree()` gives by construction.

        Raises `FileNotFoundError` if the directory is gone or hidden (the row a
        fresh listing of its parent would not have) and `NotADirectoryError` if
        it is a file; the last two come straight from `os.scandir`, which is
        also the stat this method would otherwise pay for.
        """
        path = self.safe_path(relative)
        rel = "" if path == self.root else self.relative(path)
        if rel != "" and is_hidden_dir(self.root, path):
            raise FileNotFoundError(relative)
        prefix = f"{rel}/" if rel else ""
        entries: list[DirEntry] = []
        with os.scandir(path) as scan:
            for entry in scan:
                # Symlinks are followed, because `_node` follows them: the tree
                # renders from here and the QuickBar's index from there, and a
                # linked folder that is a directory in one and a file in the
                # other is a row that cannot be opened.
                is_dir = entry.is_dir()
                if is_dir and is_ignored_dir(Path(entry.path)):
                    continue
                entries.append(
                    DirEntry(
                        name=entry.name,
                        path=prefix + entry.name,
                        kind="dir" if is_dir else "file",
                    )
                )
        entries.sort(key=lambda e: (e.kind == "file", e.name.lower()))
        return DirListing(path=rel, name=path.name or str(path), entries=entries)

    def top_level_dirs(self) -> list[str]:
        """Names of the visible directories directly under the root.

        One `os.scandir` of one directory — deliberately *not* `tree()`. Callers
        that only need the first level (the agent-session folder list) were
        walking the entire workspace to read it: on the 5,005-file perf fixture
        that is 16 directory listings and 5,005 `is_dir` calls to learn three
        names, and it ran concurrently with the real tree request on startup, so
        the two contended for the same disk and the tree arrived roughly twice as
        late. Budget: `server/tests/test_perf_budgets.py`.

        Derived from `list_dir` rather than repeating its scan, so the folder
        list reads as the top of the tree the user is looking at — same rules,
        same order.
        """
        try:
            listing = self.list_dir("")
        except OSError:  # root vanished or is unreadable — no folders, not a crash
            return []
        return [entry.name for entry in listing.entries if entry.kind == "dir"]

    def _node(self, path: Path) -> TreeNode:
        if path.is_dir():
            children = sorted(
                (
                    self._node(child)
                    for child in path.iterdir()
                    if not (child.is_dir() and is_ignored_dir(child))
                ),
                key=lambda n: (n.kind == "file", n.name.lower()),
            )
            return TreeNode(
                name=path.name or str(path),
                path=self.relative(path) if path != self.root else "",
                kind="dir",
                children=children,
            )
        return TreeNode(name=path.name, path=self.relative(path), kind="file")

    def read_text(self, relative: str) -> tuple[str, str]:
        """Return (content, hash). Rejects binary and oversized files."""
        path = self.safe_path(relative)
        # Asked before reading rather than caught after: opening a directory
        # raises `IsADirectoryError` on POSIX but `PermissionError` on Windows,
        # and this is a Windows-first app. One question, one answer, everywhere.
        if path.is_dir():
            raise IsADirectoryError(relative)
        data = path.read_bytes()
        if len(data) > MAX_TEXT_FILE_BYTES or b"\x00" in data[:8192]:
            raise NotTextError(relative)
        return data.decode("utf-8", errors="replace"), content_hash(data)

    def write_text(self, relative: str, content: str, expected_hash: str | None = None) -> str:
        """Atomic write (tmp + replace). Returns the new content hash."""
        path = self.safe_path(relative)
        if expected_hash is not None and path.exists():
            current = content_hash(path.read_bytes())
            if current != expected_hash:
                raise StaleWriteError(relative)
        return self.write_bytes(relative, content.encode("utf-8"))

    def write_bytes(self, relative: str, data: bytes, backup: bool = False) -> str:
        """Atomic write (tmp + replace). With ``backup``, the previous version is
        kept as ``<name>.bak`` — the safety net for Office round-trips until
        fidelity is trusted. Returns the new content hash."""
        path = self.safe_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if backup and path.exists():
            path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return content_hash(data)

    def create(self, relative: str, kind: str) -> None:
        path = self.safe_path(relative)
        if path.exists():
            raise FileExistsError(relative)
        if kind == "dir":
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def rename(self, relative: str, new_relative: str) -> None:
        src = self.safe_path(relative)
        dst = self.safe_path(new_relative)
        if dst.exists():
            raise FileExistsError(new_relative)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    def delete(self, relative: str) -> None:
        """Files only — directory deletion is deliberately unsupported for now."""
        path = self.safe_path(relative)
        if path.is_dir():
            raise IsADirectoryError(relative)
        path.unlink()
