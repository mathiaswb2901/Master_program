"""What disappears from the workspace — and, the harder half, what must not.

The name ``target`` carries both cases: a Rust build tree the user never wants
to see, and a folder of target data they always do. Only the tag tells them
apart, so most of what is asserted here is the second case surviving.
"""

from pathlib import Path

import pytest

from workbench_server.services import ignore
from workbench_server.services.ignore import IgnoreIndex, is_cache_dir, is_ignored_dir

TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n# created by cargo.\n"


def build_cache(directory: Path, tag: bytes = TAG) -> Path:
    """A directory that declares itself disposable, as cargo leaves it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "CACHEDIR.TAG").write_bytes(tag)
    return directory


class TestCacheTag:
    def test_tagged_directory_is_a_cache(self, tmp_path: Path) -> None:
        assert is_cache_dir(build_cache(tmp_path / "target"))

    def test_untagged_directory_is_not(self, tmp_path: Path) -> None:
        (tmp_path / "target").mkdir()
        assert not is_cache_dir(tmp_path / "target")

    def test_missing_directory_is_not(self, tmp_path: Path) -> None:
        assert not is_cache_dir(tmp_path / "never-created")

    def test_a_file_is_not(self, tmp_path: Path) -> None:
        """``<file>/CACHEDIR.TAG`` is NotADirectoryError, not a crash."""
        (tmp_path / "prices.csv").write_text("hour,eur\n")
        assert not is_cache_dir(tmp_path / "prices.csv")


class TestOwnFilesSurvive:
    """The regression the name-list approach would have caused."""

    def test_the_name_target_hides_nothing_on_its_own(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (target / "se3-targets-2026.csv").write_text("hour,mw\n")
        assert not is_ignored_dir(target)

    def test_an_untrue_tag_hides_nothing(self, tmp_path: Path) -> None:
        """A user's own file that happens to be called CACHEDIR.TAG."""
        notes = build_cache(tmp_path / "notes", tag=b"remember to tag the cache dirs\n")
        assert not is_ignored_dir(notes)

    def test_an_empty_tag_hides_nothing(self, tmp_path: Path) -> None:
        assert not is_ignored_dir(build_cache(tmp_path / "notes", tag=b""))


class TestIgnoredDir:
    def test_names_still_win(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        assert is_ignored_dir(tmp_path / "node_modules")

    def test_tagged_directory_is_ignored(self, tmp_path: Path) -> None:
        assert is_ignored_dir(build_cache(tmp_path / "target"))


class TestIgnoreIndex:
    def test_artifact_under_a_cache_is_ignored(self, tmp_path: Path) -> None:
        artifact = build_cache(tmp_path / "desktop/src-tauri/target") / "debug/build/popup.toml"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("[permissions]\n")
        assert IgnoreIndex(tmp_path).ignored(artifact)

    def test_file_under_a_plain_target_dir_is_kept(self, tmp_path: Path) -> None:
        prices = tmp_path / "analysis/target/se3-2026.csv"
        prices.parent.mkdir(parents=True)
        prices.write_text("hour,mw\n")
        assert not IgnoreIndex(tmp_path).ignored(prices)

    def test_named_dirs_are_ignored_at_any_depth(self, tmp_path: Path) -> None:
        assert IgnoreIndex(tmp_path).ignored(tmp_path / "ui/node_modules/vite/index.js")

    def test_root_itself_is_never_ignored(self, tmp_path: Path) -> None:
        """Even a workspace opened *inside* a tagged directory stays usable."""
        build_cache(tmp_path)
        assert not IgnoreIndex(tmp_path).ignored(tmp_path / "notes.md")

    def test_path_outside_the_root_is_ignored(self, tmp_path: Path) -> None:
        assert IgnoreIndex(tmp_path).ignored(tmp_path.parent / "elsewhere.txt")

    def test_ancestors_are_read_once_per_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The memo is the point: a build's events must not re-read the tag."""
        reads: list[Path] = []

        def counting_is_cache_dir(path: Path) -> bool:
            reads.append(path)
            # This module's own `is_cache_dir` was bound at import and is not
            # what the patch replaces — so this delegates, it does not recurse.
            return is_cache_dir(path)

        monkeypatch.setattr(ignore, "is_cache_dir", counting_is_cache_dir)

        cache = build_cache(tmp_path / "desktop/src-tauri/target")
        index = IgnoreIndex(tmp_path)
        for name in ("a.rlib", "b.rlib", "c.rlib"):
            assert index.ignored(cache / "debug" / name)
        # desktop, desktop/src-tauri, desktop/src-tauri/target — then nothing more
        assert reads == [
            tmp_path / "desktop",
            tmp_path / "desktop/src-tauri",
            tmp_path / "desktop/src-tauri/target",
        ]

    def test_a_directory_tagged_after_it_was_seen_needs_invalidation(self, tmp_path: Path) -> None:
        """Why the watcher watches for the tag itself: a build started just now.

        The directory appears first and is judged visible; the tag lands a moment
        later. Nothing re-reads it until someone says so.
        """
        index = IgnoreIndex(tmp_path)
        artifact = tmp_path / "target" / "debug" / "app.exe"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"MZ")
        assert not index.ignored(artifact)

        build_cache(tmp_path / "target")
        assert not index.ignored(artifact), "the stale memo this test exists to document"
        index.invalidate()
        assert index.ignored(artifact)
