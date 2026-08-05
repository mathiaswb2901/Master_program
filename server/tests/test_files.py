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
