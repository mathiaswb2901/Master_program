"""The synthetic workspace the Feel performance lane measures against.

**Generated, never committed.** 5,005 files in git would be 5,005 files every
clone, every checkout and every `git status` pays for — and a fixture that slow
to move is one nobody regenerates when the shape stops being representative.
The shape lives here as constants instead, and both halves of the lane build it
from *this* module: `test_perf_budgets.py` imports it, and the Playwright perf
project shells out to it (`python server/tests/perf_fixture.py <dir>`), which
prints the stamp as one line of JSON on stdout. One definition, so the pytest
budgets and the browser budgets are measuring the same workspace.

The shape is a real workspace's three bad cases at once, because they cost
different things:

* a **deep tree** (12 nested levels, 250 files each) — recursion depth, and the
  per-directory listing cost a walk pays,
* one **flat 2,000-file directory** — the case that makes a virtualised file
  tree the difference between smooth and unusable,
* a **5,000-line source file** — one editor open big enough for Monaco's
  tokenizer to be visible in a frame budget,
* plus a **build cache** tagged with `CACHEDIR.TAG`. It is not counted in the
  5,005: it exists so every measurement runs against a workspace where the
  ignore rules matter, and so a "list one directory" claim has something it
  must still be seen to skip.

Nothing here is random: same bytes every run, so a regression is a change in
the code and never in the fixture.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

#: Bumped whenever the shape changes; a reused directory stamped with an older
#: version is rebuilt rather than silently measured.
SPEC_VERSION = 1

FLAT_DIR = "flat"
FLAT_FILES = 2_000
DEEP_DIR = "deep"
DEEP_LEVELS = 12
DEEP_FILES_PER_LEVEL = 250
SRC_DIR = "src"
BIG_FILE = f"{SRC_DIR}/big_model.py"
BIG_FILE_LINES = 5_000
SMALL_FILE = f"{SRC_DIR}/util.py"
CACHE_DIR = "target"
CACHE_ARTIFACTS = 100

#: Written last and read first: the stamp is one of the fixture's own files, so
#: it costs no exception in the counts below.
STAMP_FILE = "perf-fixture.json"
TOP_LEVEL_FILES = ("README.md", "notes.md", STAMP_FILE)

#: Cache Directory Tagging Specification — https://bford.info/cachedir/
_CACHEDIR_TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n"

#: Files `GET /api/files/tree` returns. The headline number: 5,005.
VISIBLE_FILES = (
    FLAT_FILES + DEEP_LEVELS * DEEP_FILES_PER_LEVEL + len(TOP_LEVEL_FILES) + 2  # BIG + SMALL
)
#: Directories a full recursive walk must *list* — root, the three top-level
#: ones, and the deep chain. The tagged cache is not among them, which is the
#: point of it being there.
VISIBLE_DIRS = 1 + 3 + DEEP_LEVELS
#: What one `os.scandir` of the root should yield, in tree order (`name.lower()`).
TOP_LEVEL_DIRS = (DEEP_DIR, FLAT_DIR, SRC_DIR)


@dataclass(frozen=True)
class Fixture:
    """A built fixture, and the numbers a budget asserts against."""

    root: Path
    visible_files: int
    visible_dirs: int
    top_level_dirs: tuple[str, ...]

    def stamp(self) -> dict[str, object]:
        return {
            "spec_version": SPEC_VERSION,
            "root": str(self.root),
            "visible_files": self.visible_files,
            "visible_dirs": self.visible_dirs,
            "top_level_dirs": list(self.top_level_dirs),
        }


def _current(root: Path) -> Fixture:
    return Fixture(
        root=root,
        visible_files=VISIBLE_FILES,
        visible_dirs=VISIBLE_DIRS,
        top_level_dirs=TOP_LEVEL_DIRS,
    )


def _is_current(root: Path) -> bool:
    """Is `root` already this exact fixture? Cheap enough to always ask."""
    try:
        stamped = json.loads((root / STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(stamped.get("spec_version") == SPEC_VERSION)


def build(root: Path, *, reuse: bool = True) -> Fixture:
    """Create the fixture under `root`, returning what was built.

    With `reuse`, a directory already carrying this spec version is left alone —
    which is what makes a local perf run iterate in seconds instead of rebuilding
    5,105 files. CI passes `reuse=False` implicitly by starting from an empty
    temp directory.
    """
    root.mkdir(parents=True, exist_ok=True)
    if reuse and _is_current(root):
        return _current(root)

    flat = root / FLAT_DIR
    flat.mkdir(exist_ok=True)
    for i in range(FLAT_FILES):
        (flat / f"item_{i:04d}.txt").write_text(f"row {i}\n", encoding="utf-8")

    level = root / DEEP_DIR
    for depth in range(1, DEEP_LEVELS + 1):
        level = level / f"l{depth:02d}"
        level.mkdir(parents=True, exist_ok=True)
        for i in range(DEEP_FILES_PER_LEVEL):
            node = level / f"node_{i:03d}.py"
            node.write_text(f"DEPTH = {depth}\nINDEX = {i}\n", encoding="utf-8")

    (root / SRC_DIR).mkdir(exist_ok=True)
    (root / BIG_FILE).write_text(_big_source(), encoding="utf-8")
    (root / SMALL_FILE).write_text("PRICE_AREA = 'SE3'\n", encoding="utf-8")

    cache = root / CACHE_DIR / "debug" / "build"
    cache.mkdir(parents=True, exist_ok=True)
    (root / CACHE_DIR / "CACHEDIR.TAG").write_bytes(_CACHEDIR_TAG)
    for i in range(CACHE_ARTIFACTS):
        (cache / f"artifact_{i:04d}.o").write_text("cached\n", encoding="utf-8")

    readme = "# Perf fixture\n\nGenerated by server/tests/perf_fixture.py. Do not edit.\n"
    (root / "README.md").write_text(readme, encoding="utf-8")
    (root / "notes.md").write_text("SE3 battery notes.\n", encoding="utf-8")

    fixture = _current(root)
    # Last: the stamp is what `_is_current` trusts, so a half-built fixture must
    # never carry one.
    stamp = json.dumps(fixture.stamp(), indent=2) + "\n"
    (root / STAMP_FILE).write_text(stamp, encoding="utf-8")
    return fixture


def _big_source() -> str:
    """A 5,000-line module — long enough that opening it is a real editor cost."""
    header = ['"""Generated 5,000-line module — the big-file case."""', "", "VALUES = ["]
    body = [f"    ({i}, {i * 1.5:.2f}),  # hour {i % 24}" for i in range(BIG_FILE_LINES - 5)]
    return "\n".join([*header, *body, "]", ""])


def main(argv: list[str]) -> int:
    """CLI entry point. Stdout is the interface — structlog would be noise on a
    channel another process parses, which is why this is the one place in the
    repo that writes with `print`."""
    if len(argv) != 2:
        sys.stderr.write("usage: perf_fixture.py <directory>\n")
        return 2
    fixture = build(Path(argv[1]).resolve())
    sys.stdout.write(json.dumps(fixture.stamp(), separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
