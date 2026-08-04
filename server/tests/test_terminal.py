"""Terminal stack tests: protocol units + a real PowerShell integration round-trip."""

import json
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.terminal import (
    TerminalInput,
    TerminalResize,
    terminal_client_message,
)


class TestProtocol:
    def test_parses_input(self) -> None:
        msg = terminal_client_message.validate_json(json.dumps({"type": "input", "data": "ls\r"}))
        assert isinstance(msg, TerminalInput)
        assert msg.data == "ls\r"

    def test_parses_resize(self) -> None:
        msg = terminal_client_message.validate_json(
            json.dumps({"type": "resize", "cols": 120, "rows": 30})
        )
        assert isinstance(msg, TerminalResize)

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError):
            terminal_client_message.validate_json(json.dumps({"type": "boom", "data": "x"}))

    def test_rejects_absurd_dimensions(self) -> None:
        with pytest.raises(ValidationError):
            terminal_client_message.validate_json(
                json.dumps({"type": "resize", "cols": 0, "rows": 30})
            )


@pytest.mark.skipif(sys.platform != "win32", reason="pywinpty is Windows-only")
@pytest.mark.timeout(90)
class TestIntegration:
    def test_powershell_round_trip(self, settings: Settings) -> None:
        """Full pipeline: WS connect -> spawn shell -> command -> output comes back."""
        app = create_app(settings)
        with TestClient(app) as client, client.websocket_connect("/ws/terminal") as ws:
            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 30}))
            ws.send_text(json.dumps({"type": "input", "data": "echo wb_e2e_777\r"}))
            seen = ""
            for _ in range(500):
                frame = json.loads(ws.receive_text())
                if frame["type"] == "output":
                    seen += frame["data"]
                # the echoed command AND its output both contain the marker
                if seen.count("wb_e2e_777") >= 2:
                    break
            assert "wb_e2e_777" in seen

    def test_sessions_are_cleaned_up_on_disconnect(self, settings: Settings) -> None:
        app = create_app(settings)
        with TestClient(app) as client, client.websocket_connect("/ws/terminal") as ws:
            ws.send_text(json.dumps({"type": "input", "data": "echo hi\r"}))
            ws.receive_text()  # ensure the session is live
            # context exit closed the socket; manager must have released the PTY
        assert app.state.pty_manager._sessions == {}
