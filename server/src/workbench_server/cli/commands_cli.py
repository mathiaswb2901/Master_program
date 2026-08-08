"""``workbench-cmd`` — invoke a registered window command from a shell.

The window owns the command registry; this reaches one from outside the window,
the same surface the ``run_command`` agent tool exposes (M5 item 14). It is a
thin HTTP client over the relay endpoints:

* ``workbench-cmd list`` -> ``GET /api/commands``
* ``workbench-cmd run <id> [--json '{...}']`` -> ``POST /api/commands/invoke``

It inherits the per-launch auth token the server drops for an attaching client
(``runtime_token_path`` in :mod:`workbench_server.main`) — read exactly the way
the desktop shell reads it, keyed by port — and sends it on every call. Without
that token the server refuses (item 8 enforcement is on), which is the point: a
process that cannot read the per-user token file cannot drive the window.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from workbench_server.config import Settings, load_settings
from workbench_server.main import runtime_token_path
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
        print(f"{item['id']} :: {item['title']}")
    return 0


def _cmd_run(client: httpx.Client, command_id: str, params_json: str | None) -> int:
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as exc:
        print(f"--json is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("--json must be a JSON object.", file=sys.stderr)
        return 2
    resp = client.post("/api/commands/invoke", json={"command_id": command_id, "params": params})
    if resp.status_code == 404:
        print(_detail(resp) or f"{command_id!r} is not a registered command.", file=sys.stderr)
        return 1
    resp.raise_for_status()
    result = resp.json()
    detail = result.get("detail", "")
    if result.get("ok"):
        print(f"ran {command_id}" + (f": {detail}" if detail else ""))
        return 0
    print(f"did not run {command_id}: {detail}", file=sys.stderr)
    return 1


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
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="List the invocable commands (id and title).")
    run = sub.add_parser("run", help="Run one command by id.")
    run.add_argument("command_id", help="A command id from `workbench-cmd list`.")
    run.add_argument("--json", dest="params_json", help="Params as a JSON object.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
        with _client(base_url, token) as client:
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
