"""Full-pipeline watcher test: write through the API -> event arrives on /ws/events."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services.workspace import content_hash


@pytest.mark.timeout(60)
def test_api_write_reaches_events_websocket(settings: Settings, tmp_path: Path) -> None:
    app = create_app(settings)
    # lifespan starts the real watcher
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        resp = client.put(
            "/api/files/content",
            json={"path": "report.md", "content": "# Q3\n"},
        )
        assert resp.status_code == 200
        event = json.loads(ws.receive_text())
        assert event["type"] == "file_changed"
        # atomic-write .tmp artifacts must never surface — first event is the real file
        assert event["path"] == "report.md"
        assert event["change"] in ("added", "modified")
        # hash lets the writing client recognize its own echo
        assert event["hash"] == content_hash(b"# Q3\n")


@pytest.mark.timeout(60)
def test_external_edit_is_observed(settings: Settings, tmp_path: Path) -> None:
    """Simulates an agent editing a file directly on disk."""
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        # write_bytes: Path.write_text would translate \n -> \r\n on Windows
        (tmp_path / "model.py").write_bytes(b"VERSION = 2\n")
        event = json.loads(ws.receive_text())
        assert event["path"] == "model.py"
        assert event["hash"] == content_hash(b"VERSION = 2\n")


@pytest.mark.timeout(60)
def test_build_cache_written_while_watching_produces_no_file_events(
    settings: Settings, tmp_path: Path
) -> None:
    """A cargo build starting under a running server: churn stops at the tag.

    The directory does not exist when the watcher starts, so this is the case
    the ignore memo has to be told about — the tag arrives after the events it
    governs, sometimes inside the same debounce window.

    Two kinds of frame are legitimate before the sentinel and are skipped here:
    the ordinary folders on the way down (`desktop`, `desktop/src-tauri` are
    real directories a user sees), and the `tree_invalidated` notice the tag
    itself raises — the signal that tells a client with an incrementally patched
    tree to re-read what it is showing. Nothing from *inside* the cache may
    appear, and the watcher preserves order, so an artifact that slipped past
    would arrive before the sentinel.
    """
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        build = tmp_path / "desktop" / "src-tauri" / "target"
        (build / "debug" / "build").mkdir(parents=True)
        (build / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        for i in range(20):
            (build / "debug" / "build" / f"artifact{i}.rlib").write_bytes(b"\x00" * 64)
        # Written last: the sentinel that says every earlier event has been seen.
        (tmp_path / "model.py").write_bytes(b"VERSION = 3\n")

        seen: list[dict[str, object]] = []
        while True:
            event = json.loads(ws.receive_text())
            if event.get("path") == "model.py":
                break
            seen.append(event)

        assert [e for e in seen if e["type"] == "tree_invalidated"], "the tag was never announced"
        for event in seen:
            if event["type"] != "file_changed":
                continue
            assert event["is_dir"] is True, f"a file inside the cache surfaced: {event}"
            assert event["path"] in ("desktop", "desktop/src-tauri"), event


@pytest.mark.timeout(60)
def test_the_users_own_target_folder_still_reports_changes(
    settings: Settings, tmp_path: Path
) -> None:
    """The same name, untagged, stays fully live — analysts edit these files."""
    (tmp_path / "analysis" / "target").mkdir(parents=True)
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        (tmp_path / "analysis" / "target" / "se3-2026.csv").write_bytes(b"hour,mw\n")
        event = json.loads(ws.receive_text())
        assert event["path"] == "analysis/target/se3-2026.csv"
