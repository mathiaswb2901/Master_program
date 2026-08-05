"""Performance budgets — the server half of the Feel lane.

**Work-shaped, not wall-clock.** Every assertion here counts *work done* — how
many directories a request lists — rather than how long it took. A millisecond
ceiling on a shared CI runner fails for reasons that have nothing to do with the
code, so it gets ignored, and a budget that gets ignored is not a budget. A
count cannot flake: 16 directory listings is 16 listings on a fast laptop and on
a throttled runner, and when it changes, something in the code changed.

The measured wall-clock numbers are recorded next to each budget anyway, because
they are what makes a count *mean* something. The browser half of the lane —
cold launch, frame timing, the bytes a watcher event costs — lives in
`ui/e2e/perf/`, where the timing assertions are reported rather than blocking.

All numbers below were measured on the 5,005-file fixture
(`perf_fixture.py`) on the author's machine, 2026-08-05.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services.workspace import Workspace

from .perf_fixture import CACHE_DIR, Fixture, build


class DirListingCounter:
    """Counts directory listings under one root, by patching the primitives.

    `os.scandir` and `Path.iterdir` are the only two ways this codebase lists a
    directory, so counting them counts the walk. Two details earn their lines:

    * **Scoped to the root.** Unrelated listings happen during a request — the
      session index globbing Claude Code's storage, importlib touching
      site-packages — and none of them is workspace work. Only paths inside the
      workspace root are counted.
    * **Re-entrancy guard.** `Path.iterdir` is implemented *in terms of* one of
      these primitives, and which one depends on the CPython version (3.11 uses
      `os.listdir`, later versions use `os.scandir`). Counting both without the
      guard would silently double on some interpreters and not on others — a
      budget whose number depends on the Python build is worse than none.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self.listings: list[Path] = []
        self._depth = 0
        self._scandir = os.scandir
        self._listdir = os.listdir
        self._iterdir = Path.iterdir

    @property
    def count(self) -> int:
        return len(self.listings)

    def _note(self, target: Any) -> None:
        if self._depth:  # already counted by the wrapper we are nested inside
            return
        try:
            path = Path(os.fspath(target)).resolve()
        except (TypeError, ValueError, OSError):
            return
        if path == self._root or self._root in path.parents:
            self.listings.append(path)

    def __enter__(self) -> "DirListingCounter":
        outer = self

        # `Any` on the way through: these stand in for two heavily overloaded
        # builtins, and re-declaring their signatures here would be a second
        # copy of typeshed maintained by this test file.
        def scandir(path: Any = ".") -> Any:
            outer._note(path)
            return outer._scandir(path)

        def listdir(path: Any = ".") -> Any:
            outer._note(path)
            return outer._listdir(path)

        def iterdir(self: Path) -> Iterator[Path]:
            outer._note(self)
            outer._depth += 1
            try:
                return iter(list(outer._iterdir(self)))
            finally:
                outer._depth -= 1

        os.scandir = scandir
        os.listdir = listdir
        Path.iterdir = iterdir  # type: ignore[assignment,method-assign]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        os.scandir = self._scandir
        os.listdir = self._listdir
        Path.iterdir = self._iterdir  # type: ignore[method-assign]


@pytest.fixture(scope="session")
def perf_workspace(tmp_path_factory: pytest.TempPathFactory) -> Fixture:
    """The 5,005-file fixture, built once for the whole session (~3 s on Windows)."""
    return build(tmp_path_factory.mktemp("perf-workspace"))


@pytest.fixture
async def perf_client(perf_workspace: Fixture, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """The real app on the fixture. Session storage points at an empty temp
    directory: the budgets must not depend on the developer's own history."""
    settings = Settings(
        workspace_root=perf_workspace.root,
        claude_projects_dir=tmp_path / "claude-projects",
    )
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestBudgets:
    async def test_fixture_is_the_shape_the_budgets_assume(self, perf_workspace: Fixture) -> None:
        """A budget is only legible if the workspace behind it is known."""
        assert perf_workspace.visible_files == 5_005
        assert perf_workspace.visible_dirs == 16
        assert perf_workspace.top_level_dirs == ("deep", "flat", "src")

    async def test_session_list_lists_one_directory(
        self, perf_client: AsyncClient, perf_workspace: Fixture
    ) -> None:
        """`GET /api/agents/sessions` needs the first-level folder names, and
        nothing deeper. It must therefore list exactly one directory.

        Measured before the fix in this PR: **16 listings, 325 ms** — the router
        called `workspace.tree()`, a full recursive walk of all 5,005 files, to
        read three names. Five of those requests (the UI issues one per
        reconnect) cost 1,612 ms. After: **1 listing, 10 ms**, five in 11 ms.

        This is the budget that keeps it that way. It fails the moment the
        folder list is derived from a walk again.
        """
        await perf_client.get("/api/agents/sessions")  # warm the OS directory cache

        with DirListingCounter(perf_workspace.root) as counter:
            resp = await perf_client.get("/api/agents/sessions")

        assert resp.status_code == 200
        assert counter.count <= 1, f"listed {counter.listings}"

    async def test_file_tree_walks_the_workspace_exactly_once(
        self, perf_client: AsyncClient, perf_workspace: Fixture
    ) -> None:
        """`GET /api/files/tree` is allowed one walk — and only one.

        Measured today: **16 listings, 359 ms, 471 KB of JSON** for one request;
        one listing per visible directory is exactly one full walk. A second walk
        slipping in (a caller asking for the tree to answer a smaller question,
        which is precisely what the sessions endpoint used to do) doubles this
        number and fails here, with the count naming what went wrong.
        """
        await perf_client.get("/api/files/tree")  # warm

        with DirListingCounter(perf_workspace.root) as counter:
            resp = await perf_client.get("/api/files/tree")

        assert resp.status_code == 200
        assert counter.count == perf_workspace.visible_dirs, f"listed {counter.listings}"

    async def test_neither_endpoint_descends_into_a_tagged_build_cache(
        self, perf_client: AsyncClient, perf_workspace: Fixture
    ) -> None:
        """The ignore rules are part of the budget, not separate from it.

        A build cache is where the file counts that make a workspace slow
        actually come from, so a "one directory" claim is only worth something
        if that directory's tagged children are still skipped — by both callers,
        through the same rule (`services/ignore.py`).
        """
        with DirListingCounter(perf_workspace.root) as counter:
            await perf_client.get("/api/files/tree")
            await perf_client.get("/api/agents/sessions")

        cache = perf_workspace.root / CACHE_DIR
        assert not [p for p in counter.listings if p == cache or cache in p.parents]

    async def test_session_list_still_names_the_top_level_folders(
        self, perf_client: AsyncClient, perf_workspace: Fixture
    ) -> None:
        """The cheap path must return the same folders the expensive one did.

        Nothing is grouped under them in a fresh workspace (no transcripts on
        disk), so the endpoint's own output cannot show it — this asserts the
        service method the router now calls, against the tree the user sees.
        """
        workspace = Workspace(perf_workspace.root)
        walked = [n.path for n in (workspace.tree().children or []) if n.kind == "dir"]
        assert workspace.top_level_dirs() == walked
        assert (await perf_client.get("/api/agents/sessions")).status_code == 200
