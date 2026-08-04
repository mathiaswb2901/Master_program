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
