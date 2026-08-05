"""What the workspace pretends not to see.

Two signals, deliberately different in kind:

* ``IGNORED_DIRS`` — a short list of *names*. Every entry means the same thing
  everywhere, and none of them is a name an analyst would give a folder of their
  own. Adding a name here is a claim that nobody will ever want to see it.
* ``CACHEDIR.TAG`` — the marker from the Cache Directory Tagging Specification
  (https://bford.info/cachedir/), which tools drop into the directory they
  generate: cargo into ``target/``, pytest into ``.pytest_cache/``, uv into its
  cache. The tag is the directory declaring *itself* disposable.

The second signal exists because of the first one's limit. A Rust build tree
must disappear — it is tens of thousands of files nobody opens — but ``target``
is an ordinary name for a folder of target data, and banning the name would
silently hide the user's own work. The tag is precise where the name is not, so
only the build caches go.
"""

from pathlib import Path

IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}

CACHEDIR_TAG = "CACHEDIR.TAG"
#: The tag file's required first line, per the specification. Verified rather
#: than assumed: a file that merely carries the *name* hides nothing, so the
#: user's own ``CACHEDIR.TAG`` — a note to themselves, say — costs them no files.
_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


def is_cache_dir(path: Path) -> bool:
    """True if ``path`` declares itself a disposable cache via ``CACHEDIR.TAG``."""
    try:
        with (path / CACHEDIR_TAG).open("rb") as tag:
            return tag.read(len(_TAG_SIGNATURE)) == _TAG_SIGNATURE
    except OSError:  # absent, not a directory, or unreadable — all of them "no"
        return False


def is_ignored_dir(path: Path) -> bool:
    """True if the file tree should skip ``path`` and everything beneath it."""
    return path.name in IGNORED_DIRS or is_cache_dir(path)


def is_hidden_dir(root: Path, path: Path) -> bool:
    """True if ``path`` is a directory the tree hides, or sits inside one.

    :func:`is_ignored_dir` judges one directory by itself, which is all a
    listing loop needs to filter its own children. It is *not* enough to answer
    "may this directory be listed at all": a caller can name
    ``node_modules/vite`` directly, and every parent-level filter that would
    have stopped it was skipped by asking for it by name.

    So this is the same rule applied to the whole path — the directory and
    every ancestor below the root, which is the walk
    :meth:`IgnoreIndex.ignored` does for files on the watcher's hot path. The
    root is never hidden from itself: a workspace opened inside a tagged
    directory has to stay usable.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True  # outside the workspace entirely
    directory = root
    for part in parts:
        directory = directory / part
        if is_ignored_dir(directory):
            return True
    return False


class IgnoreIndex:
    """:func:`is_ignored_dir` applied to a whole path, memoized per directory.

    For the watcher's hot path. One ``cargo build`` emits tens of thousands of
    events under a single ignored directory, and reading a tag file for every
    ancestor of every one of them is itself the churn this exists to stop; the
    memo turns all but the first into a dict lookup.

    A memo can only be wrong in one direction — a directory seen *before* it was
    tagged stays visible — which is exactly the case of a build starting while
    the server runs. :meth:`invalidate` is the answer, and the watcher calls it
    when a tag appears or disappears.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache_dirs: dict[Path, bool] = {}

    def invalidate(self) -> None:
        """Forget every remembered verdict."""
        self._cache_dirs.clear()

    def ignored(self, path: Path) -> bool:
        """True if ``path`` lies outside the root, is named as noise, or sits in a cache."""
        try:
            parts = path.relative_to(self._root).parts
        except ValueError:
            return True
        if any(part in IGNORED_DIRS for part in parts):
            return True
        # Ancestors only: the workspace root is never hidden from itself, and the
        # last part is the changed file rather than a directory to test.
        directory = self._root
        for part in parts[:-1]:
            directory = directory / part
            if self._is_cache_dir(directory):
                return True
        return False

    def _is_cache_dir(self, path: Path) -> bool:
        remembered = self._cache_dirs.get(path)
        if remembered is None:
            remembered = is_cache_dir(path)
            self._cache_dirs[path] = remembered
        return remembered
