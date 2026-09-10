"""``workbench-cmd --script``: a routine as a file, and the import budget under it.

Two things live here, and they are the two halves of "a morning routine is a
file you run, not twelve things you click".

**The batch mode.** Ops in order, over one client; stop at the first failure
unless told otherwise; both AXI shapes a person at a terminal needs (an empty
script says it is empty, a failure names the op index and the command).

**The import budget.** ``commands_cli`` used to import
:mod:`workbench_server.main` for one function and pay 1.66 s of FastAPI, routers,
services, ``openpyxl``, ``nbformat`` and the agent SDK on *every* invocation.
That is fixed by moving the function; the test that keeps it fixed asserts the
module graph, in a subprocess, because a 1.6 s regression is invisible to every
other gate this repo has — pytest, mypy and ruff would all stay green while every
CLI call got three times slower.
"""

import io
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from workbench_server.cli import commands_cli


def _script_client(handler: object) -> httpx.Client:
    return httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _ok(detail: str = "") -> httpx.Response:
    return httpx.Response(
        200, json={"invocation_id": "x", "dispatched": True, "ok": True, "detail": detail}
    )


def _refused(detail: str) -> httpx.Response:
    return httpx.Response(
        200, json={"invocation_id": "x", "dispatched": False, "ok": False, "detail": detail}
    )


def _routine(*ops: dict[str, object]) -> str:
    return json.dumps({"ops": list(ops)})


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """What `--script -` reads. Patched on the real `sys`, which is the one the
    CLI module holds a reference to."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


# --- ops run in order, on one connection -------------------------------------


def test_a_script_runs_its_ops_in_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body["command_id"], body["params"]))
        return _ok(body["command_id"])

    _stdin(
        monkeypatch,
        _routine(
            {"command_id": "workspace.open", "params": {"path": "C:/work/se3"}},
            {"command_id": "layout.switch", "params": {"name": "Morning"}},
            {"command_id": "panel.terminal"},
        ),
    )
    assert commands_cli._cmd_script(_script_client(handler), "-", False) == 0
    assert [command for command, _ in seen] == ["workspace.open", "layout.switch", "panel.terminal"]
    # The params rode the wire verbatim — the whole point of PR-E's other half.
    assert seen[0][1] == {"path": "C:/work/se3"}
    out = capsys.readouterr()
    assert "1/3 ok" in out.out
    assert "3 ops, 3 ok, 0 failed." in out.err


def test_a_script_from_a_file_is_the_same_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "morning.json"
    path.write_text(_routine({"command_id": "panel.terminal"}), encoding="utf-8")
    assert commands_cli._cmd_script(_script_client(lambda _r: _ok()), str(path), False) == 0
    assert "1 ops, 1 ok" in capsys.readouterr().err


# --- failure: stop, and say where --------------------------------------------


def test_a_script_stops_at_the_first_failure_and_names_the_op(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A routine is a sequence: op 3 assumes op 2 happened. Carrying on would run
    the rest of it against a window that is not in the state it was written for."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        command_id = str(json.loads(request.content)["command_id"])
        calls.append(command_id)
        if command_id == "workspace.open":
            return _refused("'C:/nope' is not on the recent workspaces list")
        return _ok()

    _stdin(
        monkeypatch,
        _routine(
            {"command_id": "panel.terminal"},
            {"command_id": "workspace.open", "params": {"path": "C:/nope"}},
            {"command_id": "panel.agent"},
        ),
    )
    assert commands_cli._cmd_script(_script_client(handler), "-", False) == 1
    assert calls == ["panel.terminal", "workspace.open"], "the third op must never have run"
    err = capsys.readouterr().err
    assert "2/3 FAILED workspace.open" in err
    assert "stopped at op 2 of 3" in err
    assert "--continue-on-error" in err


def test_continue_on_error_runs_the_rest_and_tallies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        command_id = str(json.loads(request.content)["command_id"])
        return _refused("nope") if command_id == "b" else _ok()

    _stdin(
        monkeypatch,
        _routine({"command_id": "a"}, {"command_id": "b"}, {"command_id": "c"}),
    )
    # Exit code still non-zero: something in the routine did not happen.
    assert commands_cli._cmd_script(_script_client(handler), "-", True) == 1
    assert "3 ops, 2 ok, 1 failed." in capsys.readouterr().err


def test_an_unregistered_id_is_a_refusal_not_a_transport_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "'x' is not a registered command."})

    _stdin(monkeypatch, _routine({"command_id": "x"}))
    assert commands_cli._cmd_script(_script_client(handler), "-", False) == 1
    assert "not a registered command" in capsys.readouterr().err


# --- the two AXI shapes ------------------------------------------------------


def test_an_empty_script_says_it_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not an error — the file was read and it asked for nothing — but never
    silence: exiting 0 with no output is indistinguishable from a broken run."""
    calls: list[str] = []
    _stdin(monkeypatch, json.dumps({"ops": []}))
    client = _script_client(lambda r: calls.append(str(r.url)) or _ok())  # type: ignore[func-returns-value]
    assert commands_cli._cmd_script(client, "-", False) == 0
    assert calls == []
    assert "no ops" in capsys.readouterr().err


def test_a_malformed_script_names_what_is_wrong_with_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "{not json")
    assert commands_cli._cmd_script(_script_client(lambda _r: _ok()), "-", False) == 2
    assert "stdin is not valid JSON" in capsys.readouterr().err


def test_an_op_missing_its_command_id_names_the_field(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, json.dumps({"ops": [{"params": {}}]}))
    assert commands_cli._cmd_script(_script_client(lambda _r: _ok()), "-", False) == 2
    err = capsys.readouterr().err
    assert "ops.0.command_id" in err


def test_a_script_file_that_is_not_there_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert commands_cli._cmd_script(_script_client(lambda _r: _ok()), "no-such.json", False) == 2
    assert "cannot read the script" in capsys.readouterr().err


# --- argv: the mode is a flag, and neither mode is an error ------------------


def test_main_with_neither_a_subcommand_nor_a_script_prints_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert commands_cli.main([]) == 2
    assert "give a subcommand (list, run) or --script" in capsys.readouterr().err


# --- the import budget -------------------------------------------------------


def test_the_cli_module_does_not_drag_in_the_application() -> None:
    """The 1.6 s regression test.

    Run in a **subprocess** on purpose: this pytest process has already imported
    ``workbench_server.main`` (the app fixtures build it), so an in-process
    ``sys.modules`` check would pass no matter what the CLI imports. A fresh
    interpreter that imports only the CLI module is the only place the question
    can be asked honestly.

    The four named modules are the expensive ones the old edge really pulled in,
    verified by running the same probe against ``workbench_server.main``: the app
    itself, FastAPI, and the two document libraries. (The agent SDK is *not* in
    the list because ``services/sdk_factory.py`` imports it lazily inside its
    functions, so it was never on this path to begin with.) Naming them rather
    than diffing the whole module set keeps the test about the budget instead of
    about every transitive import uv happens to ship.
    """
    probe = (
        "import importlib, sys;"
        "importlib.import_module('workbench_server.cli.commands_cli');"
        "leaked=[m for m in ("
        "'workbench_server.main','fastapi','openpyxl','nbformat'"
        ") if m in sys.modules];"
        "print(','.join(leaked));"
        "sys.exit(1 if leaked else 0)"
    )
    result = subprocess.run(  # noqa: S603 - argv is this file's own literal probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        "the CLI re-acquired an import edge to the application layer: "
        f"{result.stdout.strip()}. See workbench_server.runtime — this costs "
        "~1.2 s on every workbench-cmd invocation and no other gate can see it."
    )
