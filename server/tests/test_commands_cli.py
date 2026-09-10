"""``workbench-cmd`` CLI: token resolution, URL building, and the two subcommands.

The relay service has its own suite (``test_commands.py``); this covers the thin
HTTP client that reaches it from a shell. The load-bearing case is the regression
for the ``--port``/token-file divergence: the token must come from the port the
request is actually addressed to, not the default port's file.
"""

import json
from pathlib import Path

import httpx
import pytest

from workbench_server.cli import commands_cli
from workbench_server.cli.commands_cli import _HEADER
from workbench_server.config import Settings
from workbench_server.runtime import runtime_token_path


def _mock_client(handler: object) -> httpx.Client:
    """A sync client whose requests are answered by ``handler`` — no network."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return httpx.Client(base_url="http://test", transport=transport)


# --- _read_token -------------------------------------------------------------


def test_read_token_reads_the_port_keyed_file(tmp_path: Path) -> None:
    settings = Settings(workspace_root=tmp_path, app_data_root=tmp_path, port=9001)
    path = runtime_token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("  the-token \n", encoding="utf-8")
    assert commands_cli._read_token(settings) == "the-token"


def test_read_token_missing_is_none(tmp_path: Path) -> None:
    settings = Settings(workspace_root=tmp_path, app_data_root=tmp_path, port=9002)
    assert commands_cli._read_token(settings) is None


# --- _base_url ---------------------------------------------------------------


def test_base_url_prefers_the_flag_port_then_falls_back(tmp_path: Path) -> None:
    settings = Settings(workspace_root=tmp_path, port=8787)
    assert commands_cli._base_url(settings, None, 9001) == "http://127.0.0.1:9001"
    assert commands_cli._base_url(settings, None, None) == "http://127.0.0.1:8787"
    assert commands_cli._base_url(settings, "localhost", 5000) == "http://localhost:5000"


# --- main(): the --port/token regression -------------------------------------


def test_main_reads_the_token_for_the_addressed_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--port 9001`` must authenticate with the 9001 server's token file, not
    the default port's. Before the fix the token was resolved from settings.port
    (8787) while the request went to 9001, so this returned 2 ('No auth token
    found') even though the 9001 server was healthy."""
    settings = Settings(workspace_root=tmp_path, app_data_root=tmp_path, port=8787)
    monkeypatch.setattr(commands_cli, "load_settings", lambda: settings)
    # Only the 9001 server has dropped a token; the default 8787 has not.
    token_path = runtime_token_path(settings.model_copy(update={"port": 9001}))
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("token-9001", encoding="utf-8")

    captured: dict[str, str] = {}

    def fake_client(base_url: str, token: str) -> httpx.Client:
        captured["base_url"] = base_url
        captured["token"] = token
        return httpx.Client(
            base_url=base_url,
            headers={_HEADER: token},
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"commands": []})),
        )

    monkeypatch.setattr(commands_cli, "_client", fake_client)

    assert commands_cli.main(["--port", "9001", "list"]) == 0
    assert captured["token"] == "token-9001"
    assert captured["base_url"] == "http://127.0.0.1:9001"


def test_main_without_a_token_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(workspace_root=tmp_path, app_data_root=tmp_path, port=8787)
    monkeypatch.setattr(commands_cli, "load_settings", lambda: settings)
    assert commands_cli.main(["list"]) == 2
    assert "No auth token found" in capsys.readouterr().err


# --- _cmd_list ---------------------------------------------------------------


def test_cmd_list_prints_ids_and_titles(capsys: pytest.CaptureFixture[str]) -> None:
    client = _mock_client(
        lambda req: httpx.Response(
            200, json={"commands": [{"id": "view.toggleTheme", "title": "Toggle theme"}]}
        )
    )
    assert commands_cli._cmd_list(client) == 0
    assert "view.toggleTheme :: Toggle theme" in capsys.readouterr().out


def test_cmd_list_says_none_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    client = _mock_client(lambda req: httpx.Response(200, json={"commands": []}))
    assert commands_cli._cmd_list(client) == 0
    assert "is a Workbench window open" in capsys.readouterr().err


# --- _cmd_run ----------------------------------------------------------------


def test_cmd_run_reports_a_successful_run(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/commands/invoke"
        assert json.loads(req.content) == {"command_id": "view.toggleTheme", "params": {}}
        return httpx.Response(
            200,
            json={
                "invocation_id": "x",
                "dispatched": True,
                "ok": True,
                "detail": "Toggle theme",
            },
        )

    assert commands_cli._cmd_run(_mock_client(handler), "view.toggleTheme", None) == 0
    assert "ran view.toggleTheme: Toggle theme" in capsys.readouterr().out


def test_cmd_run_forwards_params_json(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content) == {"command_id": "x", "params": {"a": 1}}
        return httpx.Response(
            200, json={"invocation_id": "x", "dispatched": True, "ok": True, "detail": ""}
        )

    assert commands_cli._cmd_run(_mock_client(handler), "x", '{"a": 1}') == 0


def test_cmd_run_surfaces_a_404_as_not_registered(capsys: pytest.CaptureFixture[str]) -> None:
    client = _mock_client(
        lambda req: httpx.Response(404, json={"detail": "'x' is not a registered command."})
    )
    assert commands_cli._cmd_run(client, "x", None) == 1
    assert "not a registered command" in capsys.readouterr().err


def test_cmd_run_reports_a_refused_run(capsys: pytest.CaptureFixture[str]) -> None:
    client = _mock_client(
        lambda req: httpx.Response(
            200,
            json={
                "invocation_id": "x",
                "dispatched": False,
                "ok": False,
                "detail": "No Workbench window is connected to run commands.",
            },
        )
    )
    assert commands_cli._cmd_run(client, "view.toggleTheme", None) == 1
    assert "did not run view.toggleTheme" in capsys.readouterr().err


def test_cmd_run_rejects_invalid_json(capsys: pytest.CaptureFixture[str]) -> None:
    client = _mock_client(lambda req: httpx.Response(200, json={}))
    assert commands_cli._cmd_run(client, "x", "{not json") == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_cmd_run_rejects_non_object_json(capsys: pytest.CaptureFixture[str]) -> None:
    client = _mock_client(lambda req: httpx.Response(200, json={}))
    assert commands_cli._cmd_run(client, "x", "[1, 2]") == 2
    assert "must be a JSON object" in capsys.readouterr().err
