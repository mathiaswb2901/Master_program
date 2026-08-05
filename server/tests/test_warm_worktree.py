"""The worktree warmer's decision table.

Everything here is about *not* creating a junction when one would be wrong: a
junction shares the main checkout's ``node_modules``, so linking against a
different lockfile hands the worktree the wrong dependency set, and the failure
shows up somewhere unrelated (a Vite "failed to resolve import", not an npm
error). The byte comparison is therefore the load-bearing guard, and it is
tested from both sides.

No real junctions are created: ``FakeOps`` stands in for git, npm, uv and the
link syscall, the same Protocol-and-fake shape the server uses for the PTY
(``services/pty_manager.py``) and the SDK client (``services/sdk_factory.py``).
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

import warm_worktree
from warm_worktree import MARKER_NAME, main, plan_node_package


class FakeOps:
    """Records every command and link instead of performing it."""

    def __init__(self, captures: dict[tuple[str, ...], str]) -> None:
        self._captures = captures
        self.link_ok = True
        self.exit_code = 0
        self.runs: list[tuple[tuple[str, ...], Path]] = []
        self.links: list[tuple[Path, Path]] = []

    def capture(self, args: Sequence[str], cwd: Path) -> str:
        return self._captures[tuple(args)]

    def run(self, args: Sequence[str], cwd: Path) -> int:
        self.runs.append((tuple(args), cwd))
        return self.exit_code

    def link_dir(self, link: Path, target: Path) -> bool:
        self.links.append((link, target))
        if not self.link_ok:
            return False
        link.mkdir(parents=True)  # a stand-in for the junction, so it now "exists"
        return True

    @property
    def commands(self) -> list[tuple[str, ...]]:
        return [args for args, _ in self.runs]


LOCK = b'{"name":"ui","lockfileVersion":3}\n'


def _package(root: Path, name: str, lock: bytes | None, node_modules: bool) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if lock is not None:
        (directory / "package-lock.json").write_bytes(lock)
    if node_modules:
        (directory / "node_modules").mkdir()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout with both packages installed, and an empty worktree."""
    main_checkout = tmp_path / "workbench"
    (main_checkout / ".git").mkdir(parents=True)
    for name in ("ui", "desktop"):
        _package(main_checkout, name, LOCK, node_modules=True)

    worktree = main_checkout / ".claude" / "worktrees" / "feature"
    for name in ("ui", "desktop"):
        _package(worktree, name, LOCK, node_modules=False)
    return main_checkout, worktree


def _ops(main_checkout: Path, worktree: Path) -> FakeOps:
    return FakeOps(
        {
            ("git", "rev-parse", "--show-toplevel"): str(worktree),
            ("git", "rev-parse", "--git-common-dir"): str(main_checkout / ".git"),
        }
    )


# --- discovering the main checkout -----------------------------------------


def test_finds_main_checkout_from_a_linked_worktree(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)

    assert warm_worktree.find_main_checkout(worktree, ops) == main_checkout


def test_finds_main_checkout_from_a_relative_common_dir(tmp_path: Path) -> None:
    """Inside the main checkout git answers a bare relative ``.git``."""
    checkout = tmp_path / "workbench"
    (checkout / ".git").mkdir(parents=True)
    ops = FakeOps({("git", "rev-parse", "--git-common-dir"): ".git"})

    assert warm_worktree.find_main_checkout(checkout, ops) == checkout


def test_refuses_to_run_in_the_main_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "workbench"
    (checkout / ".git").mkdir(parents=True)
    _package(checkout, "ui", LOCK, node_modules=True)
    ops = FakeOps(
        {
            ("git", "rev-parse", "--show-toplevel"): str(checkout),
            ("git", "rev-parse", "--git-common-dir"): ".git",
        }
    )

    code = main([], ops=ops, cwd=checkout)

    assert code != 0
    assert ops.runs == [] and ops.links == []


# --- the lockfile guard -----------------------------------------------------


def test_links_when_lockfiles_are_byte_identical(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo

    step = plan_node_package("ui", worktree, main_checkout)

    assert step.action == "link"


def test_installs_when_the_lockfile_differs_by_one_byte(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    (worktree / "ui" / "package-lock.json").write_bytes(LOCK.replace(b"3", b"4"))

    step = plan_node_package("ui", worktree, main_checkout)

    assert step.action == "install"
    assert "differs" in step.reason


def test_installs_when_the_main_checkout_has_no_node_modules(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    (main_checkout / "ui" / "node_modules").rmdir()

    step = plan_node_package("ui", worktree, main_checkout)

    assert step.action == "install"
    assert "no node_modules" in step.reason


def test_installs_when_a_lockfile_is_missing(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    (worktree / "ui" / "package-lock.json").unlink()

    step = plan_node_package("ui", worktree, main_checkout)

    assert step.action == "install"


def test_skips_when_the_worktree_already_has_node_modules(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    (worktree / "ui" / "node_modules").mkdir()

    step = plan_node_package("ui", worktree, main_checkout)

    assert step.action == "skip"


def test_skips_a_package_directory_the_worktree_does_not_have(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    (worktree / "desktop" / "package-lock.json").unlink()
    (worktree / "desktop").rmdir()

    step = plan_node_package("desktop", worktree, main_checkout)

    assert step.action == "skip"


# --- the whole run ----------------------------------------------------------


def test_a_warm_run_links_both_packages_and_never_runs_npm(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)

    code = main([], ops=ops, cwd=worktree)

    assert code == 0
    assert ops.links == [
        (worktree / "ui" / "node_modules", main_checkout / "ui" / "node_modules"),
        (worktree / "desktop" / "node_modules", main_checkout / "desktop" / "node_modules"),
    ]
    assert ops.commands == [("uv", "sync", "--dev")]


def test_python_is_synced_never_linked(repo: tuple[Path, Path]) -> None:
    """A venv bakes in absolute paths, so .venv must never be shared."""
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)

    main([], ops=ops, cwd=worktree)

    assert ("uv", "sync", "--dev") in ops.commands
    assert all(".venv" not in str(link) for link, _ in ops.links)


def test_a_differing_lockfile_installs_that_package_only(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    (worktree / "desktop" / "package-lock.json").write_bytes(LOCK + b"\n")
    ops = _ops(main_checkout, worktree)

    main([], ops=ops, cwd=worktree)

    assert [link for link, _ in ops.links] == [worktree / "ui" / "node_modules"]
    assert (("npm", "ci"), worktree / "desktop") in ops.runs


def test_a_failed_link_falls_back_to_npm_ci(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)
    ops.link_ok = False

    code = main([], ops=ops, cwd=worktree)

    assert code == 0
    assert (("npm", "ci"), worktree / "ui") in ops.runs
    assert (("npm", "ci"), worktree / "desktop") in ops.runs
    assert not (worktree / MARKER_NAME).exists()


def test_a_failed_install_exits_non_zero(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)
    ops.exit_code = 1

    assert main([], ops=ops, cwd=worktree) == 1


def test_the_marker_names_every_shared_directory(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)

    main([], ops=ops, cwd=worktree)

    marker = json.loads((worktree / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["junctioned"] == ["ui/node_modules", "desktop/node_modules"]
    assert marker["main_checkout"] == str(main_checkout)
    assert "npm" in marker["warning"]


def test_a_stale_marker_is_cleared_when_nothing_is_shared(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    (worktree / MARKER_NAME).write_text("{}", encoding="utf-8")
    for name in ("ui", "desktop"):
        (worktree / name / "node_modules").mkdir()
    ops = _ops(main_checkout, worktree)

    main([], ops=ops, cwd=worktree)

    assert not (worktree / MARKER_NAME).exists()


# --- giving the links back ---------------------------------------------------
#
# `git worktree remove` recurses through a junction and deletes the main
# checkout's node_modules, so unlinking is not tidiness — it is the difference
# between removing a worktree and wiping the checkout it borrowed from.


def test_unlink_drops_every_recorded_link_and_the_marker(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)
    main([], ops=ops, cwd=worktree)

    code = main(["--unlink"], ops=ops, cwd=worktree)

    assert code == 0
    assert not (worktree / "ui" / "node_modules").exists()
    assert not (worktree / "desktop" / "node_modules").exists()
    assert not (worktree / MARKER_NAME).exists()
    assert (main_checkout / "ui" / "node_modules").is_dir()


def test_unlink_refuses_a_real_populated_node_modules(repo: tuple[Path, Path]) -> None:
    """os.rmdir is the safety net: it cannot recurse, so a real tree survives."""
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)
    main([], ops=ops, cwd=worktree)
    (worktree / "ui" / "node_modules" / "react").mkdir(parents=True)

    code = main(["--unlink"], ops=ops, cwd=worktree)

    assert code == 1
    assert (worktree / "ui" / "node_modules" / "react").is_dir()
    assert (worktree / MARKER_NAME).exists()  # still describes reality


def test_unlink_without_a_marker_is_a_no_op(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)

    assert main(["--unlink"], ops=ops, cwd=worktree) == 0
    assert ops.runs == [] and ops.links == []


def test_dry_run_changes_nothing(repo: tuple[Path, Path]) -> None:
    main_checkout, worktree = repo
    ops = _ops(main_checkout, worktree)

    code = main(["--dry-run"], ops=ops, cwd=worktree)

    assert code == 0
    assert ops.runs == [] and ops.links == []
    assert not (worktree / MARKER_NAME).exists()
