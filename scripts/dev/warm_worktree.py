"""Make a fresh git worktree usable in seconds instead of minutes.

A new worktree starts with no ``node_modules`` and no ``.venv``, so the first
gate an agent runs pays 10-15 minutes of ``npm ci`` (twice: ``ui/`` and
``desktop/``) plus ``uv sync --dev``. The dependency trees are almost always
byte-identical to the main checkout's, because the branch usually does not touch
a lockfile at all. This script skips that install by pointing the worktree's
``node_modules`` at the main checkout's with a Windows **directory junction**
(``mklink /J``, which needs neither administrator rights nor developer mode).

Run it from inside the fresh worktree, with any Python 3.11+ — it imports only
the standard library on purpose, so it works *before* ``uv sync`` has built a
venv::

    python scripts/dev/warm_worktree.py

SAFETY — READ THIS BEFORE RUNNING ANY npm COMMAND IN A WARMED WORKTREE
======================================================================

A junction is not a copy. ``<worktree>/ui/node_modules`` and
``<main>/ui/node_modules`` are the *same directory on disk*, seen through two
names. That is exactly what makes warming free, and it has two consequences:

1. **Reading is safe, writing is not.** Running the test suite, the linter, the
   type-checker, Vite or Playwright out of a junctioned ``node_modules`` is
   fine — they only read. But ``npm ci``, ``npm install``, ``npm update`` and
   ``npm dedupe`` *rewrite the tree they are pointed at*, so running one inside
   a warmed worktree silently rewrites the main checkout's dependencies — and
   every other worktree junctioned to it. ``npm ci`` is the worst case: it
   deletes ``node_modules`` first.

   Before installing or adding a dependency, break the link first. Removing a
   junction removes only the link, never the target::

       cmd /c rmdir ui\\node_modules      # Windows: unlinks, target untouched
       rm ui/node_modules                 # POSIX symlink fallback

   then ``npm install`` normally. (``rmdir`` refuses if you point it at a real
   directory with contents, which is the safety net; ``shutil.rmtree`` likewise
   refuses to recurse into a link.)

2. **A junction is only correct while the lockfiles agree.** If the worktree's
   ``package-lock.json`` differs from the main checkout's, its ``node_modules``
   is the *wrong* dependency set, and the failure does not look like a
   dependency problem: a branch that adds ``@tauri-apps/api`` surfaces as a Vite
   "Failed to resolve import" error, which reads like a broken import path. So
   the byte-comparison of the two lockfiles is the load-bearing guard here — if
   they differ by a single byte this script runs a real ``npm ci`` instead and
   says why.

Every warmed worktree gets a ``.warm-worktree.json`` marker at its root listing
which directories are junctions, so an agent (or a hook) can check before
running an install. No marker means nothing is shared.

``.venv`` is deliberately **not** junctioned. A virtualenv bakes absolute paths
into ``pyvenv.cfg`` and into every console script it generates, so a shared one
resolves back to the main checkout in ways that are subtly wrong rather than
loudly broken. ``uv sync --dev`` is run instead; uv's global package cache makes
it hard-links-fast anyway.

Non-Windows hosts fall back to a directory symlink, and to a plain install if
the symlink is refused (unprivileged Windows without developer mode, or a
filesystem that has no links).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

#: Directories that carry their own ``package.json`` + ``package-lock.json``.
NODE_PACKAGES = ("ui", "desktop")

#: Written at the worktree root when at least one junction exists.
MARKER_NAME = ".warm-worktree.json"

MARKER_WARNING = (
    "node_modules here is a link to the main checkout - shared, not copied. "
    "Reading/testing is safe; npm ci|install|update would rewrite the main "
    "checkout. Remove the link first: cmd /c rmdir <dir>\\node_modules"
)

Action = Literal["link", "install", "skip"]


@dataclass(frozen=True)
class Step:
    """What to do about one directory, and why — decided before anything runs."""

    name: str
    action: Action
    reason: str


@dataclass(frozen=True)
class Outcome:
    """What actually happened, including a downgrade from ``link`` to ``install``."""

    name: str
    action: Action
    reason: str
    ok: bool
    seconds: float


class Ops(Protocol):
    """The effectful surface, injected so tests never shell out or create links.

    Mirrors how the server fakes its own edges (``PtyLike`` in
    ``services/pty_manager.py``, the SDK client factory in
    ``services/sdk_factory.py``): a Protocol narrow enough that a dozen-line
    fake is a faithful stand-in.
    """

    def capture(self, args: Sequence[str], cwd: Path) -> str:
        """Run a command and return its stdout, stripped. Raises on failure."""

    def run(self, args: Sequence[str], cwd: Path) -> int:
        """Run a command with output streaming to the terminal; return its exit code."""

    def link_dir(self, link: Path, target: Path) -> bool:
        """Point ``link`` at the existing directory ``target``. False if unsupported."""


class SystemOps:
    """The real thing: subprocess + ``mklink /J`` (or ``os.symlink`` off Windows)."""

    def capture(self, args: Sequence[str], cwd: Path) -> str:
        exe = shutil.which(args[0]) or args[0]
        completed = subprocess.run(  # noqa: S603 - fixed argv, path from shutil.which
            [exe, *args[1:]], cwd=cwd, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    def run(self, args: Sequence[str], cwd: Path) -> int:
        exe = shutil.which(args[0])
        if exe is None:
            print(f"    {args[0]} is not on PATH")
            return 127
        print(f"    $ {' '.join(args)}")
        return subprocess.run(  # noqa: S603 - fixed argv, path from shutil.which
            [exe, *args[1:]], cwd=cwd
        ).returncode

    def link_dir(self, link: Path, target: Path) -> bool:
        if os.name == "nt":
            # A junction, not a symlink: junctions need no administrator rights
            # and no developer mode, which is the difference between this script
            # working on a stock Windows box and not. mklink is a cmd builtin,
            # so it has to be invoked through cmd itself.
            cmd = shutil.which("cmd") or "cmd"
            completed = subprocess.run(  # noqa: S603 - fixed argv, path from shutil.which
                [cmd, "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                print(f"    mklink failed: {completed.stdout.strip()} {completed.stderr.strip()}")
                return False
            return True
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            print(f"    symlink failed: {exc}")
            return False
        return True


def _exists(path: Path) -> bool:
    """True for a directory, a link, and a *broken* link (which blocks mklink)."""
    return path.exists() or path.is_symlink()


def _same_path(a: Path, b: Path) -> bool:
    """Windows paths compare case-insensitively; ``==`` on Path does not."""
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def worktree_root(cwd: Path, ops: Ops) -> Path:
    """The checkout that ``cwd`` belongs to."""
    return Path(ops.capture(["git", "rev-parse", "--show-toplevel"], cwd)).resolve()


def find_main_checkout(worktree: Path, ops: Ops) -> Path:
    """The checkout that owns the shared object store.

    ``git rev-parse --git-common-dir`` answers ``.git`` (relative) in the main
    checkout and an absolute ``.../<main>/.git`` in a linked worktree; either
    way the parent of the resolved path is the main checkout. Equality with the
    worktree root is therefore how "you are in the main checkout" is detected.
    """
    common = Path(ops.capture(["git", "rev-parse", "--git-common-dir"], worktree))
    if not common.is_absolute():
        common = worktree / common
    return common.resolve().parent


def plan_node_package(name: str, worktree: Path, main: Path) -> Step:
    """Decide link / install / skip for one Node package directory.

    Pure apart from filesystem reads, so the whole decision table is testable
    against a ``tmp_path`` skeleton.
    """
    here = worktree / name
    if not here.is_dir():
        return Step(name, "skip", "not present in this worktree")
    if _exists(here / "node_modules"):
        return Step(name, "skip", "node_modules already present")

    ours = here / "package-lock.json"
    theirs = main / name / "package-lock.json"
    if not ours.is_file() or not theirs.is_file():
        return Step(name, "install", "no package-lock.json to compare")
    if ours.read_bytes() != theirs.read_bytes():
        return Step(name, "install", "package-lock.json differs from the main checkout")
    if not (main / name / "node_modules").is_dir():
        return Step(name, "install", "main checkout has no node_modules to share")
    return Step(name, "link", "lockfile identical to the main checkout")


def run_node_step(step: Step, worktree: Path, main: Path, ops: Ops) -> Outcome:
    """Carry out one planned step, downgrading ``link`` to ``install`` if linking fails."""
    started = time.monotonic()
    if step.action == "skip":
        return Outcome(step.name, "skip", step.reason, True, 0.0)

    if step.action == "link":
        link = worktree / step.name / "node_modules"
        target = main / step.name / "node_modules"
        if ops.link_dir(link, target):
            return Outcome(step.name, "link", step.reason, True, time.monotonic() - started)
        step = Step(step.name, "install", "links unsupported here")

    code = ops.run(["npm", "ci"], worktree / step.name)
    return Outcome(step.name, "install", step.reason, code == 0, time.monotonic() - started)


def run_python_step(worktree: Path, ops: Ops) -> Outcome:
    """``uv sync --dev``. Never a link: a venv hard-codes its own absolute path."""
    started = time.monotonic()
    code = ops.run(["uv", "sync", "--dev"], worktree)
    reason = "uv's global cache makes this cheap; a venv cannot be shared"
    return Outcome("python", "install", reason, code == 0, time.monotonic() - started)


def write_marker(worktree: Path, main: Path, linked: Sequence[str]) -> None:
    """Record which directories are shared, or clear a stale record when none are."""
    marker = worktree / MARKER_NAME
    if not linked:
        marker.unlink(missing_ok=True)
        return
    marker.write_text(
        json.dumps(
            {
                "warmed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "main_checkout": str(main),
                "junctioned": [f"{name}/node_modules" for name in linked],
                "warning": MARKER_WARNING,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _report(outcomes: Sequence[Outcome], elapsed: float, linked: Sequence[str]) -> None:
    verb = {"link": "linked  ", "install": "installed", "skip": "skipped "}
    print("\nSummary")
    for outcome in outcomes:
        mark = " " if outcome.ok else "!"
        timing = f"{outcome.seconds:6.1f}s" if outcome.action != "skip" else "       "
        print(f" {mark} {verb[outcome.action]} {outcome.name:<8} {timing}  {outcome.reason}")
    print(f"\nTotal {elapsed:.1f}s")
    if linked:
        shared = ", ".join(f"{name}/node_modules" for name in linked)
        print(
            f"\n  !! {shared} is SHARED with the main checkout, not copied.\n"
            "     Tests and builds read it safely. npm ci / npm install / npm update\n"
            "     would rewrite the main checkout's tree and every worktree linked to\n"
            "     it - unlink first:  cmd /c rmdir ui\\node_modules\n"
            f"     Recorded in {MARKER_NAME}."
        )


def main(
    argv: Sequence[str] | None = None,
    ops: Ops | None = None,
    cwd: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="warm_worktree",
        description="Warm a fresh git worktree from the main checkout (see module docstring).",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    args = parser.parse_args(argv)

    ops = ops or SystemOps()
    here = cwd or Path.cwd()
    started = time.monotonic()

    try:
        worktree = worktree_root(here, ops)
        main_checkout = find_main_checkout(worktree, ops)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Not inside a git checkout ({exc}).")
        return 1

    if _same_path(worktree, main_checkout):
        print(
            f"Refusing to run: {worktree} is the main checkout.\n"
            "This script warms a *linked worktree* from the main checkout; there is\n"
            "nothing to warm from here, and junctioning a directory onto itself would\n"
            "destroy it. Run it from inside a worktree created with `git worktree add`."
        )
        return 2
    if not (main_checkout / ".git").exists():
        print(f"Refusing to run: {main_checkout} does not look like a checkout.")
        return 2

    print(f"worktree     {worktree}\nmain checkout {main_checkout}\n")

    steps = [plan_node_package(name, worktree, main_checkout) for name in NODE_PACKAGES]
    if args.dry_run:
        for step in steps:
            print(f"  {step.action:<8} {step.name:<8} {step.reason}")
        print("  install  python   uv sync --dev")
        return 0

    outcomes: list[Outcome] = []
    for step in steps:
        print(f"  {step.name}: {step.action} - {step.reason}")
        outcomes.append(run_node_step(step, worktree, main_checkout, ops))
    print("  python: uv sync --dev")
    outcomes.append(run_python_step(worktree, ops))

    linked = [outcome.name for outcome in outcomes if outcome.action == "link" and outcome.ok]
    write_marker(worktree, main_checkout, linked)
    _report(outcomes, time.monotonic() - started, linked)
    return 0 if all(outcome.ok for outcome in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
