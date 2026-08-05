"""Workspace file operations. Every path from the wire passes through `safe_path` —
the jail that keeps the server inside the workspace root."""

import hashlib
import os
import tempfile
from pathlib import Path

from workbench_server.models.files import MAX_TEXT_FILE_BYTES, TreeNode
from workbench_server.services.ignore import is_ignored_dir


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
        return self._node(self.root)

    def top_level_dirs(self) -> list[str]:
        """Names of the visible directories directly under the root.

        One `os.scandir` of one directory — deliberately *not* `tree()`. Callers
        that only need the first level (the agent-session folder list) were
        walking the entire workspace to read it: on the 5,005-file perf fixture
        that is 16 directory listings and 5,005 `is_dir` calls to learn three
        names, and it ran concurrently with the real tree request on startup, so
        the two contended for the same disk and the tree arrived roughly twice as
        late. Budget: `server/tests/test_perf_budgets.py`.

        Same visibility rules as `tree()` — noise names and `CACHEDIR.TAG` build
        caches are skipped — and the same order, so the folder list reads as the
        top of the tree the user is looking at.
        """
        try:
            with os.scandir(self.root) as entries:
                names = [
                    entry.name
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False) and not is_ignored_dir(Path(entry.path))
                ]
        except OSError:  # root vanished or is unreadable — no folders, not a crash
            return []
        return sorted(names, key=str.lower)

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
