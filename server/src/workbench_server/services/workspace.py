"""Workspace file operations. Every path from the wire passes through `safe_path` —
the jail that keeps the server inside the workspace root."""

import hashlib
import os
import tempfile
from pathlib import Path

from workbench_server.models.files import MAX_TEXT_FILE_BYTES, TreeNode

IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}


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

    def _node(self, path: Path) -> TreeNode:
        if path.is_dir():
            children = sorted(
                (
                    self._node(child)
                    for child in path.iterdir()
                    if not (child.is_dir() and child.name in IGNORED_DIRS)
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
        data = content.encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
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
