"""``workbench-cmd`` — invoke a registered window command from a shell.

The window owns the command registry; this reaches one from outside the window,
the same surface the ``run_command`` agent tool exposes (M5 item 14). It is a
thin HTTP client over the relay endpoints:

* ``workbench-cmd list`` -> ``GET /api/commands``
* ``workbench-cmd run <id> [--json '{...}']`` -> ``POST /api/commands/invoke``

* ``workbench-cmd --script routine.json`` (or ``-`` for stdin) -> a JSON op-list,
  run in order over **one** connection and one interpreter start.

It inherits the per-launch auth token the server drops for an attaching client
(``runtime_token_path`` in :mod:`workbench_server.runtime`) — read exactly the way
the desktop shell reads it, keyed by port — and sends it on every call. Without
that token the server refuses (item 8 enforcement is on), which is the point: a
process that cannot read the per-user token file cannot drive the window.

**Nothing here imports** :mod:`workbench_server.main`, and that is a budget, not
a preference: it used to, for ``runtime_token_path`` alone, and dragged FastAPI,
every router, every service, ``openpyxl`` and ``nbformat`` in behind it — 1.66 s
of the 1.74 s this module cost to import, measured five runs each against a bare
interpreter at 0.06 s. See :mod:`workbench_server.runtime`;
``server/tests/test_cli_script.py`` fails if the edge ever comes back.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
from pydantic import ValidationError

from workbench_server.config import Settings, load_settings
from workbench_server.models.commands import (
    CommandParamsSchema,
    CommandScript,
    ScriptOp,
    ScriptResult,
)
from workbench_server.runtime import runtime_token_path
from workbench_server.services.local_auth import TOKEN_HEADER

_HEADER = TOKEN_HEADER.decode("latin-1")


def _read_token(settings: Settings) -> str | None:
    """The per-launch token the running server dropped for this port, or None."""
    path: Path = runtime_token_path(settings)
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _base_url(settings: Settings, host: str | None, port: int | None) -> str:
    resolved_host = host or "127.0.0.1"
    resolved_port = port if port is not None else settings.port
    return f"http://{resolved_host}:{resolved_port}"


def _client(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers={_HEADER: token}, timeout=15.0)


def _cmd_list(client: httpx.Client) -> int:
    resp = client.get("/api/commands")
    resp.raise_for_status()
    commands = resp.json().get("commands", [])
    if not commands:
        print("No commands available - is a Workbench window open?", file=sys.stderr)
        return 0
    for item in commands:
        schema = item.get("params_schema")
        shape = CommandParamsSchema.model_validate(schema).hint() if schema else ""
        print(f"{item['id']} :: {item['title']}" + (f"  {shape}" if shape else ""))
    return 0


def _invoke(client: httpx.Client, op: ScriptOp) -> tuple[bool, str]:
    """One relay round trip. ``(ok, detail)`` — never raises for a refusal.

    A 404 is the id being unregistered, which is a refusal like any other here
    rather than a transport failure; anything else non-2xx still raises, so a
    server that is down is reported as a server that is down.
    """
    resp = client.post(
        "/api/commands/invoke", json={"command_id": op.command_id, "params": op.params}
    )
    if resp.status_code == 404:
        return False, _detail(resp) or f"{op.command_id!r} is not a registered command."
    resp.raise_for_status()
    result = resp.json()
    return bool(result.get("ok")), str(result.get("detail", ""))


def _cmd_run(client: httpx.Client, command_id: str, params_json: str | None) -> int:
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as exc:
        print(f"--json is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("--json must be a JSON object.", file=sys.stderr)
        return 2
    ok, detail = _invoke(client, ScriptOp(command_id=command_id, params=params))
    if ok:
        print(f"ran {command_id}" + (f": {detail}" if detail else ""))
        return 0
    print(f"did not run {command_id}: {detail}", file=sys.stderr)
    return 1


class ScriptError(Exception):
    """A script that could not be read or parsed, with the reason for a human."""


def _read_script(source: str) -> CommandScript:
    """Parse a ``--script`` document from a file, or from stdin for ``-``.

    Raises :class:`ScriptError` with a sentence a person at a terminal can act
    on — which file, which line, which field — rather than a traceback.
    """
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise ScriptError(f"cannot read the script {source}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        where = "stdin" if source == "-" else source
        raise ScriptError(f"{where} is not valid JSON: {exc}") from exc
    try:
        return CommandScript.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "(document)"
        raise ScriptError(f"the script is not a valid op list: {field}: {first['msg']}") from exc


def _cmd_script(client: httpx.Client, source: str, keep_going: bool) -> int:
    """Run every op in order, over this one connection.

    Stops at the first failure by default: a routine is a sequence, and op 3
    usually assumes op 2 happened — carrying on would run the rest of it against
    a window that is not in the state they were written for. ``--continue-on-error``
    is for the other kind of script, and says which ops failed either way.

    Both AXI shapes a person at a terminal needs: an empty script says it is
    empty rather than exiting 0 in silence, and a failure names the op *index*
    and the command, so a twelve-line routine does not have to be bisected.
    """
    try:
        script = _read_script(source)
    except ScriptError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    total = len(script.ops)
    if total == 0:
        print("The script has no ops — nothing to run.", file=sys.stderr)
        return 0
    failed = 0
    for index, op in enumerate(script.ops, start=1):
        ok, detail = _invoke(client, op)
        result = ScriptResult(index=index, command_id=op.command_id, ok=ok, detail=detail)
        print(result.line(total), file=sys.stdout if ok else sys.stderr)
        if ok:
            continue
        failed += 1
        if not keep_going:
            print(
                f"stopped at op {index} of {total}. "
                "Fix it and re-run, or pass --continue-on-error.",
                file=sys.stderr,
            )
            return 1
    print(f"{total} ops, {total - failed} ok, {failed} failed.", file=sys.stderr)
    return 0 if failed == 0 else 1


def _detail(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workbench-cmd",
        description="Invoke a registered Workbench window command from a shell.",
    )
    parser.add_argument("--host", help="Backend host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, help="Backend port (default WORKBENCH_PORT or 8787).")
    # A top-level flag rather than a third subcommand: it is the *mode* the
    # whole invocation runs in, and a subcommand would read as one more thing to
    # run rather than as "these ops, one process".
    parser.add_argument(
        "--script",
        metavar="FILE",
        help='Run a JSON op list ({"ops":[{"command_id":…,"params":{…}}]}); "-" reads stdin.',
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep running a script after an op fails (default: stop at the first).",
    )
    # Not `required=True` any more: `--script` is a complete invocation on its
    # own. `main` refuses an invocation with neither, with the usage text.
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list", help="List the invocable commands (id, title, and any params).")
    run = sub.add_parser("run", help="Run one command by id.")
    run.add_argument("command_id", help="A command id from `workbench-cmd list`.")
    run.add_argument("--json", dest="params_json", help="Params as a JSON object.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.script is None and args.action is None:
        parser.print_usage(sys.stderr)
        print("workbench-cmd: give a subcommand (list, run) or --script.", file=sys.stderr)
        return 2
    # Re-key the settings to the port actually being addressed (--port wins over
    # WORKBENCH_PORT/default) before reading the token, so runtime_token_path and
    # _base_url agree on which server this is: without it the token always comes
    # from the default port's file while the request goes to --port, so a second
    # server on another port is dialled with the wrong (or missing) token.
    settings = load_settings()
    resolved_port = args.port if args.port is not None else settings.port
    settings = settings.model_copy(update={"port": resolved_port})
    token = _read_token(settings)
    if token is None:
        print(
            "No auth token found — start the server (`uv run workbench-server`) first, "
            "and pass --port if it is not on the default port.",
            file=sys.stderr,
        )
        return 2
    base_url = _base_url(settings, args.host, args.port)
    try:
        # One client for the whole invocation — which is the whole of what
        # `--script` buys over N shells: one interpreter start, one connection,
        # N round trips of a few milliseconds each.
        with _client(base_url, token) as client:
            if args.script is not None:
                return _cmd_script(client, args.script, args.continue_on_error)
            if args.action == "list":
                return _cmd_list(client)
            return _cmd_run(client, args.command_id, args.params_json)
    except httpx.HTTPStatusError as exc:
        print(
            f"{base_url}: {exc.response.status_code} {exc.response.reason_phrase}", file=sys.stderr
        )
        return 1
    except httpx.HTTPError as exc:
        print(f"cannot reach the backend at {base_url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
