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
