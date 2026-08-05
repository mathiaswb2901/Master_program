"""Files API: jail safety, round-trips, conflict detection, tree shape."""

from pathlib import Path

import pytest
from httpx import AsyncClient

from workbench_server.services.workspace import (
    PathOutsideWorkspaceError,
    Workspace,
    content_hash,
)


class TestJail:
    def test_escape_via_dotdot_is_blocked(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path)
        with pytest.raises(PathOutsideWorkspaceError):
            ws.safe_path("../outside.txt")

    def test_escape_via_absolute_path_is_blocked(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path)
        with pytest.raises(PathOutsideWorkspaceError):
            ws.safe_path("C:/Windows/system32/drivers/etc/hosts")

    def test_workspace_root_itself_is_allowed(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path)
        assert ws.safe_path("") == tmp_path.resolve()

    async def test_http_rejects_escape(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/content", params={"path": "../../secrets.txt"})
        assert resp.status_code == 400


class TestReadWrite:
    async def test_round_trip_with_hash(self, client: AsyncClient) -> None:
        body = {"path": "notes/hello.py", "content": "print('hei')\n"}
        w = await client.put("/api/files/content", json=body)
        assert w.status_code == 200
        digest = w.json()["hash"]
        assert digest == content_hash(b"print('hei')\n")

        r = await client.get("/api/files/content", params={"path": "notes/hello.py"})
        assert r.status_code == 200
        assert r.json() == {"path": "notes/hello.py", "content": "print('hei')\n", "hash": digest}

    async def test_stale_write_conflict(self, client: AsyncClient, tmp_path: Path) -> None:
        await client.put("/api/files/content", json={"path": "a.txt", "content": "v1"})
        # someone else (an agent) changes the file on disk
        (tmp_path / "a.txt").write_bytes(b"v2-from-agent")
        resp = await client.put(
            "/api/files/content",
            json={"path": "a.txt", "content": "v3", "expected_hash": content_hash(b"v1")},
        )
        assert resp.status_code == 409

    async def test_binary_read_rejected(self, client: AsyncClient, tmp_path: Path) -> None:
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02")
        resp = await client.get("/api/files/content", params={"path": "data.bin"})
        assert resp.status_code == 415


class TestTree:
    async def test_tree_ignores_noise_dirs(self, client: AsyncClient, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1")
        resp = await client.get("/api/files/tree")
        assert resp.status_code == 200
        root = resp.json()
        names = [c["name"] for c in root["children"]]
        assert "src" in names
        assert ".git" not in names
        src = next(c for c in root["children"] if c["name"] == "src")
        assert src["children"][0] == {
            "name": "app.py",
            "path": "src/app.py",
            "kind": "file",
            "children": None,
        }

    async def test_a_directory_tagged_mid_session_is_gone_from_the_next_walk(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """The tree re-reads the tag on every walk and remembers nothing.

        The watcher memoizes the same test (``IgnoreIndex``) because a build
        buries it in events, and so has to be told when a tag appears. The tree
        is walked on demand and deliberately does not — this is the guard on
        that difference. Applying the watcher's optimization here would leave
        the tree serving a build directory it had already been told to skip,
        until a restart. A server does not need starting for it: the tree walk
        holds no state to start, which is the whole claim.
        """
        build = tmp_path / "target"
        (build / "debug").mkdir(parents=True)
        (build / "debug" / "app.exe").write_bytes(b"MZ")

        before = (await client.get("/api/files/tree")).json()
        assert [c["name"] for c in before["children"]] == ["target"]

        # cargo, creating its build directory under a session already running
        (build / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")

        after = (await client.get("/api/files/tree")).json()
        assert [c["name"] for c in after["children"]] == []

    async def test_tree_skips_build_caches_but_keeps_folders_of_the_same_name(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Two directories called ``target``; only the one cargo made goes away."""
        build = tmp_path / "desktop" / "src-tauri" / "target"
        (build / "debug").mkdir(parents=True)
        (build / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        (build / "debug" / "popup.toml").write_text("[permissions]")

        own = tmp_path / "analysis" / "target"
        own.mkdir(parents=True)
        (own / "se3-2026.csv").write_text("hour,mw\n")

        root = (await client.get("/api/files/tree")).json()
        by_name = {child["name"]: child for child in root["children"]}
        tauri = by_name["desktop"]["children"][0]
        assert [c["name"] for c in tauri["children"]] == []
        kept = by_name["analysis"]["children"][0]
        assert kept["name"] == "target"
        assert kept["children"] == [
            {
                "name": "se3-2026.csv",
                "path": "analysis/target/se3-2026.csv",
                "kind": "file",
                "children": None,
            }
        ]


class TestDirListing:
    """`GET /api/files/dir` — the tree, one level at a time.

    What it owes the tree it replaced is *parity*: the same rows, in the same
    order, with the same things hidden. The cost is budgeted separately
    (`test_perf_budgets.py`: exactly one directory listing per request).
    """

    async def test_root_listing_names_the_workspace_and_sorts_dirs_first(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        (tmp_path / "notes.md").write_text("x")
        (tmp_path / "Zeta").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / "README.md").write_text("y")

        resp = await client.get("/api/files/dir")
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == ""
        assert body["name"] == tmp_path.name
        assert [(e["name"], e["kind"]) for e in body["entries"]] == [
            ("alpha", "dir"),
            ("Zeta", "dir"),
            ("notes.md", "file"),
            ("README.md", "file"),
        ]

    async def test_a_directory_entry_carries_no_children(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """The whole point: asking for one directory reads exactly that one."""
        (tmp_path / "src" / "deep").mkdir(parents=True)
        (tmp_path / "src" / "app.py").write_text("x = 1")

        entries = (await client.get("/api/files/dir", params={"path": "src"})).json()["entries"]
        assert entries == [
            {"name": "deep", "path": "src/deep", "kind": "dir"},
            {"name": "app.py", "path": "src/app.py", "kind": "file"},
        ]

    async def test_same_visibility_rules_as_the_walk(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Noise names and tagged caches, hidden by the one shared rule — two
        panels disagreeing about what exists is the failure this prevents."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        cache = tmp_path / "target"
        cache.mkdir()
        (cache / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        own = tmp_path / "analysis"
        own.mkdir()
        (own / "target").mkdir()  # same name, no tag: the analyst's own folder

        root = (await client.get("/api/files/dir")).json()["entries"]
        assert [e["name"] for e in root] == ["analysis"]
        nested = (await client.get("/api/files/dir", params={"path": "analysis"})).json()
        assert [e["name"] for e in nested["entries"]] == ["target"]

    async def test_a_tag_dropped_mid_session_hides_the_directory_next_listing(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """No memo here either (the watcher has one; this is walked on demand)."""
        build = tmp_path / "target"
        build.mkdir()
        (build / "app.exe").write_bytes(b"MZ")
        assert [e["name"] for e in (await client.get("/api/files/dir")).json()["entries"]] == [
            "target"
        ]

        (build / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")

        assert (await client.get("/api/files/dir")).json()["entries"] == []

    async def test_missing_and_non_directory_paths_are_told_apart(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        (tmp_path / "notes.md").write_text("x")
        assert (await client.get("/api/files/dir", params={"path": "nope"})).status_code == 404
        assert (await client.get("/api/files/dir", params={"path": "notes.md"})).status_code == 400
        escape = await client.get("/api/files/dir", params={"path": "../.."})
        assert escape.status_code == 400

    async def test_a_hidden_directory_asked_for_by_name_is_not_there(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """The rule applies to the directory *asked for*, not only its children.

        Filtering children is all a walk needs — `tree()` can never be handed a
        hidden directory, because the parent-level filter runs before it
        recurses. This endpoint takes a path from the wire, so it can be, and
        answering it would make `/dir` the one reader that disagrees with the
        tree, the watcher and the search index about what exists.
        """
        (tmp_path / "node_modules" / "vite").mkdir(parents=True)
        (tmp_path / "node_modules" / "vite" / "index.js").write_text("x")

        assert (
            await client.get("/api/files/dir", params={"path": "node_modules"})
        ).status_code == 404
        assert (
            await client.get("/api/files/dir", params={"path": "node_modules/vite"})
        ).status_code == 404

    async def test_a_directory_tagged_while_it_is_open_stops_listing(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """The reconcile path, end to end.

        A folder is expanded, a build drops `CACHEDIR.TAG` into it while it is
        still open, the watcher publishes `tree_invalidated`, and the client
        re-lists everything it has open — that folder included, because nothing
        has told it the folder is gone. It has to hear 404 (which its `loadDir`
        already turns into a `deleted` row) rather than a listing of a directory
        every other reader now hides.
        """
        build = tmp_path / "build"
        build.mkdir()
        (build / "app.exe").write_bytes(b"MZ")
        assert (await client.get("/api/files/dir", params={"path": "build"})).status_code == 200

        (build / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")

        assert (await client.get("/api/files/dir", params={"path": "build"})).status_code == 404
        # ...and its children go with it, however they are reached.
        (build / "debug").mkdir()
        assert (
            await client.get("/api/files/dir", params={"path": "build/debug"})
        ).status_code == 404

    async def test_the_root_is_listable_even_inside_a_tagged_directory(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """A workspace opened inside a cache is still the user's workspace."""
        (tmp_path / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        (tmp_path / "notes.md").write_text("x")

        resp = await client.get("/api/files/dir")
        assert resp.status_code == 200
        assert [e["name"] for e in resp.json()["entries"]] == ["CACHEDIR.TAG", "notes.md"]


def _symlink_dir(link: Path, target: Path) -> None:
    """Create a directory symlink, or skip — Windows needs privilege for it."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as e:  # no developer mode, no admin
        pytest.skip(f"symlinks unavailable here: {e}")


class TestSymlinkedDirectories:
    """One answer to "is this a directory", in every reader.

    The tree renders from `list_dir` and the QuickBar's index from `tree()`. A
    linked folder that is a directory in one and a file in the other is a row
    the user can see but not open — and the click used to reach `read_text` on
    a directory, which is not an error any router catches.
    """

    def test_a_linked_folder_is_a_directory_in_both_readers(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / "prices.csv").write_text("hour,eur\n")
        _symlink_dir(tmp_path / "shared", real)

        ws = Workspace(tmp_path)
        listed = {e.name: e.kind for e in ws.list_dir("").entries}
        walked = {n.name: n.kind for n in ws.tree().children or []}
        assert listed["shared"] == walked["shared"] == "dir"

    def test_a_link_into_a_build_cache_is_hidden_by_both(self, tmp_path: Path) -> None:
        """`is_ignored_dir` runs on the same notion of "is a directory".

        It is only reached `if is_dir`, so a link classified as a file skipped
        the ignore rule entirely — the linked build cache came back as a row.
        """
        cache = tmp_path / "target"
        cache.mkdir()
        (cache / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        _symlink_dir(tmp_path / "vendor", cache)

        ws = Workspace(tmp_path)
        assert [e.name for e in ws.list_dir("").entries] == []
        assert [n.name for n in ws.tree().children or []] == []

    async def test_reading_a_directory_as_a_file_is_a_4xx_not_a_crash(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Opening a directory row must not surface as an uncaught 500.

        Windows raises `PermissionError` where POSIX raises `IsADirectoryError`,
        so this is asked before the read rather than caught after it.
        """
        (tmp_path / "reports").mkdir()
        resp = await client.get("/api/files/content", params={"path": "reports"})
        assert resp.status_code == 400


class TestTopLevelDirs:
    """`Workspace.top_level_dirs` — one directory listing, the tree's own rules.

    The agent-session folder list used to come from `tree()`: a full recursive
    walk of the workspace to read the names sitting directly under the root.
    This is the replacement, so what it owes is *parity* — the same names, in
    the same order, with the same things hidden — for one `os.scandir`. The
    count itself is budgeted in `test_perf_budgets.py`.
    """

    def test_names_the_visible_first_level_dirs_only(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "deeper").mkdir()  # second level: never listed
        (tmp_path / "analysis").mkdir()
        (tmp_path / "notes.md").write_text("x")  # a file is not a folder
        assert Workspace(tmp_path).top_level_dirs() == ["analysis", "src"]

    def test_order_matches_the_tree_the_user_is_looking_at(self, tmp_path: Path) -> None:
        for name in ("Zeta", "alpha", "Beta"):
            (tmp_path / name).mkdir()
        ws = Workspace(tmp_path)
        walked = [n.path for n in (ws.tree().children or []) if n.kind == "dir"]
        assert ws.top_level_dirs() == walked == ["alpha", "Beta", "Zeta"]

    def test_skips_noise_names_and_tagged_build_caches(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        (tmp_path / ".git").mkdir()
        cache = tmp_path / "target"
        cache.mkdir()
        (cache / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        (tmp_path / "analysis").mkdir()
        assert Workspace(tmp_path).top_level_dirs() == ["analysis"]

    def test_a_folder_named_target_without_the_tag_is_kept(self, tmp_path: Path) -> None:
        """The tag hides a directory; the name never does — same as the tree."""
        (tmp_path / "target").mkdir()
        assert Workspace(tmp_path).top_level_dirs() == ["target"]

    def test_a_root_that_is_not_there_yields_no_folders_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """The session list is not where a user should learn the workspace moved."""
        assert Workspace(tmp_path / "not-there").top_level_dirs() == []
