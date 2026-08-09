"""Local-API security hardening (M5 item 8 / OSS-bar item 1): enforcement.

Enforcement is the shipped default now (PR4, ``enforce_auth=True``). The suite
runs it off (conftest sets ``WORKBENCH_ENFORCE_AUTH=0`` so the broad suite need
not thread a token), so the enforcement paths are exercised here by building an
app — and the middleware directly — with ``enforce=True``: token-gated REST, the
WS Origin/token handshake, the per-endpoint subprotocol echo, and the CORS
ordering that keeps a 403 readable to a browser.
"""

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services.local_auth import (
    WS_CLOSE_POLICY,
    LocalAuthMiddleware,
    is_local_origin,
    ws_subprotocol_to_echo,
)

TOKEN = "sekret-launch-token"
LOCAL_ORIGIN = "http://localhost:5173"


def _label(token: str) -> str:
    return f"workbench.auth.{token}"


# --- is_local_origin ---------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:5173",
        "http://127.0.0.1:8787",
        "http://[::1]:8787",
        "https://tauri.localhost",  # the Tauri shell's own origin
        "http://app.localhost",  # RFC 6761: any *.localhost is loopback
        "127.0.0.1:8787",  # a bare Host header, no scheme
        "[::1]:8787",  # bracketed IPv6, as a Host header carries it
        "localhost",
    ],
)
def test_is_local_origin_accepts_loopback(value: str) -> None:
    assert is_local_origin(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.com",
        "http://localhost.evil.com",  # localhost is a *label*, not the host
        "https://notlocalhost",  # endswith needs the dot
        "http://10.0.0.1",
        "http://example.com:8787",
        "test",  # the ASGITransport default Host
    ],
)
def test_is_local_origin_rejects_foreign(value: str) -> None:
    assert not is_local_origin(value)


# --- middleware unit harness -------------------------------------------------

Message = dict[str, Any]


def _downstream() -> tuple[Callable[[Scope, Receive, Send], Awaitable[None]], dict[str, bool]]:
    """A trivial ASGI app that records whether it ran and answers minimally."""
    seen = {"hit": False}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen["hit"] = True
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        else:  # websocket
            await send({"type": "websocket.accept"})

    return app, seen


async def _run(mw: LocalAuthMiddleware, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> MutableMapping[str, Any]:
        return {"type": f"{scope['type']}.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    await mw(scope, receive, send)
    return sent


def _http_scope(
    *, path: str = "/api/layouts", method: str = "GET", token: str | None = None
) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"x-workbench-token", token.encode()))
    return {"type": "http", "method": method, "path": path, "headers": headers}


def _ws_scope(
    *,
    path: str = "/ws/agents",
    origin: str | None = None,
    header_token: str | None = None,
    subprotocol_token: str | None = None,
) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if header_token is not None:
        headers.append((b"x-workbench-token", header_token.encode()))
    subprotocols: list[str] = []
    if subprotocol_token is not None:
        subprotocols.append(f"workbench.auth.{subprotocol_token}")
    return {"type": "websocket", "path": path, "headers": headers, "subprotocols": subprotocols}


def _enforced() -> tuple[LocalAuthMiddleware, dict[str, bool]]:
    app, seen = _downstream()
    mw = LocalAuthMiddleware(app, token=TOKEN, enforce=True, is_local_origin=is_local_origin)
    return mw, seen


# --- HTTP enforcement --------------------------------------------------------


async def test_http_tokenless_gated_call_is_forbidden() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _http_scope())
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403
    assert seen["hit"] is False


async def test_http_correct_token_passes() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _http_scope(token=TOKEN))
    assert sent[0]["status"] == 200
    assert seen["hit"] is True


async def test_http_wrong_token_is_forbidden() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _http_scope(token="nope"))
    assert sent[0]["status"] == 403
    assert seen["hit"] is False


@pytest.mark.parametrize(
    "scope",
    [
        _http_scope(method="OPTIONS"),  # CORS preflight
        _http_scope(path="/api/health"),  # liveness
        _http_scope(path="/api/auth/token"),  # the handout itself
        _http_scope(path="/index.html"),  # the static UI mount
        _http_scope(path="/"),
    ],
)
async def test_http_exempt_paths_pass_without_token(scope: Scope) -> None:
    mw, seen = _enforced()
    sent = await _run(mw, scope)
    assert sent[0]["status"] == 200
    assert seen["hit"] is True


# --- WebSocket enforcement ---------------------------------------------------


#: The frames a rejected handshake emits: accept first (so the close code is
#: actually delivered), then close with the policy code. See ``_reject_ws``.
_REJECT_FRAMES = [
    {"type": "websocket.accept"},
    {"type": "websocket.close", "code": WS_CLOSE_POLICY},
]


async def test_ws_tokenless_handshake_is_rejected() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _ws_scope())
    assert sent == _REJECT_FRAMES
    assert seen["hit"] is False


async def test_ws_foreign_origin_is_rejected_even_with_token() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _ws_scope(origin="http://evil.com", header_token=TOKEN))
    assert sent == _REJECT_FRAMES
    assert seen["hit"] is False


async def test_ws_local_origin_with_subprotocol_token_passes() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _ws_scope(origin="http://localhost:5173", subprotocol_token=TOKEN))
    assert seen["hit"] is True
    assert sent[0]["type"] == "websocket.accept"


async def test_ws_local_origin_with_header_token_passes() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _ws_scope(origin="https://tauri.localhost", header_token=TOKEN))
    assert seen["hit"] is True
    assert sent[0]["type"] == "websocket.accept"


async def test_ws_absent_origin_is_allowed_with_token() -> None:
    # Native and test clients send no Origin; only a browser attaches one.
    mw, seen = _enforced()
    sent = await _run(mw, _ws_scope(origin=None, header_token=TOKEN))
    assert seen["hit"] is True
    assert sent[0]["type"] == "websocket.accept"


async def test_ws_absent_origin_still_needs_a_token() -> None:
    mw, seen = _enforced()
    sent = await _run(mw, _ws_scope(origin=None))
    assert sent == _REJECT_FRAMES
    assert seen["hit"] is False


# --- the shipped default: enforce=False is a pass-through --------------------


async def test_disabled_middleware_passes_tokenless_http_through() -> None:
    app, seen = _downstream()
    mw = LocalAuthMiddleware(app, token=TOKEN, enforce=False, is_local_origin=is_local_origin)
    sent = await _run(mw, _http_scope())
    assert sent[0]["status"] == 200
    assert seen["hit"] is True


async def test_disabled_middleware_passes_foreign_ws_through() -> None:
    app, seen = _downstream()
    mw = LocalAuthMiddleware(app, token=TOKEN, enforce=False, is_local_origin=is_local_origin)
    sent = await _run(mw, _ws_scope(origin="http://evil.com"))
    assert seen["hit"] is True
    assert sent[0]["type"] == "websocket.accept"


# --- integration through create_app -----------------------------------------


async def _app_client(settings: Settings) -> AsyncClient:
    app = create_app(settings)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_token_handout_returns_token_to_local_caller(settings: Settings) -> None:
    settings = settings.model_copy(update={"auth_token": TOKEN})
    async with await _app_client(settings) as client:
        resp = await client.get("/api/auth/token", headers={"host": "localhost:8787"})
    assert resp.status_code == 200
    assert resp.json() == {"token": TOKEN}


async def test_token_handout_refuses_foreign_host(settings: Settings) -> None:
    async with await _app_client(settings) as client:
        # ASGITransport's default Host is "test", which is not local.
        resp = await client.get("/api/auth/token")
    assert resp.status_code == 403


async def test_token_handout_refuses_foreign_origin(settings: Settings) -> None:
    async with await _app_client(settings) as client:
        resp = await client.get(
            "/api/auth/token",
            headers={"host": "localhost", "origin": "http://evil.com"},
        )
    assert resp.status_code == 403


def test_config_default_enforces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The shipped default is enforcement ON (M5 item 8, PR4). The test suite runs
    # it off (conftest sets WORKBENCH_ENFORCE_AUTH=0 so the broad suite need not
    # thread a token); read the real default by dropping that override first.
    monkeypatch.delenv("WORKBENCH_ENFORCE_AUTH", raising=False)
    assert Settings(workspace_root=tmp_path).enforce_auth is True


def test_enforcement_can_be_forced_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The debugging escape hatch: WORKBENCH_ENFORCE_AUTH=0 turns the middleware
    # back into a pass-through without editing code.
    monkeypatch.setenv("WORKBENCH_ENFORCE_AUTH", "0")
    assert Settings(workspace_root=tmp_path).enforce_auth is False


async def test_enforced_app_forbids_tokenless_rest(settings: Settings) -> None:
    settings = settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN})
    async with await _app_client(settings) as client:
        resp = await client.get("/api/layouts")
    assert resp.status_code == 403


async def test_enforced_app_allows_tokened_rest(settings: Settings) -> None:
    settings = settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN})
    async with await _app_client(settings) as client:
        resp = await client.get("/api/layouts", headers={"X-Workbench-Token": TOKEN})
    assert resp.status_code == 200


async def test_enforced_app_lets_options_preflight_through(settings: Settings) -> None:
    # CORS preflight carries no credentials; it must reach the CORS layer without
    # a token or a browser can never make the real call that follows it.
    settings = settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN})
    async with await _app_client(settings) as client:
        resp = await client.options(
            "/api/layouts",
            headers={
                "origin": "http://localhost:5173",
                "access-control-request-method": "GET",
            },
        )
    assert resp.status_code < 400
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


async def test_enforced_app_serves_token_handout_without_a_token(settings: Settings) -> None:
    # The bootstrap endpoint stays reachable with no token (chicken-and-egg): it
    # is how a fresh client obtains one. Guarded on local Origin/Host instead.
    settings = settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN})
    async with await _app_client(settings) as client:
        resp = await client.get("/api/auth/token", headers={"host": "localhost:8787"})
    assert resp.status_code == 200
    assert resp.json() == {"token": TOKEN}


async def test_default_app_mints_a_token(settings: Settings) -> None:
    app = create_app(settings)
    assert isinstance(app.state.auth_token, str)
    assert len(app.state.auth_token) >= 32


async def test_enforced_403_still_carries_cors_header(settings: Settings) -> None:
    # Middleware order regression: CORS must be the OUTER layer so a request the
    # auth layer rejects still gets its Access-Control-Allow-Origin header on the
    # way out. Reversed, a browser XHR with a missing/wrong token would see an
    # opaque CORS failure instead of the readable 403 the middleware sends.
    settings = settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN})
    async with await _app_client(settings) as client:
        # A cross-origin browser call from the Vite dev origin, no token.
        resp = await client.get("/api/layouts", headers={"origin": "http://localhost:5173"})
    assert resp.status_code == 403
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


# --- real-server WebSocket close code ----------------------------------------
#
# The unit harness above drives the middleware's own ``send`` callback, so it
# can only assert which frames the middleware *emits*. Whether the documented
# 4403 close code actually reaches a client depends on the ASGI server: a close
# sent before the 101 upgrade is dropped by uvicorn to a flat HTTP 403, which a
# browser surfaces as an opaque 1006. Only a real uvicorn server with a real WS
# client can catch that, so this test stands one up.


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _live_server(settings: Settings) -> Iterator[int]:
    """Run ``create_app(settings)`` under a real uvicorn on a free port."""
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_config=None, lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start in time")
            time.sleep(0.05)
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        # Asserted, not hoped for. `should_exit` only ends uvicorn's *accept*
        # loop; it then waits for every live connection task, so a WebSocket
        # handler that never notices its client left keeps this thread — and the
        # whole app, watcher included — running for the life of the process.
        # That is the shape of a Ctrl-C that hangs, and a bare `join(timeout=)`
        # turns it into fifteen silent seconds instead of a red test.
        assert not thread.is_alive(), "uvicorn never shut down: a connection task is still live"


async def test_ws_reject_delivers_policy_close_code_over_real_server(settings: Settings) -> None:
    import websockets

    settings = settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN})
    with _live_server(settings) as port:
        # No token and no Origin: the middleware rejects before the endpoint
        # runs. The regression is that the client must observe the 4403 policy
        # code, not a dropped connection — which requires accepting the upgrade
        # before closing.
        uri = f"ws://127.0.0.1:{port}/ws/agents"
        async with websockets.connect(uri) as ws:
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=10)
            assert ws.close_code == WS_CLOSE_POLICY


async def test_ws_echoes_offered_subprotocol_over_real_server(settings: Settings) -> None:
    # The browser-critical half: a browser fails any handshake whose offered
    # subprotocol the server does not echo. `websockets` does not enforce that,
    # so we assert the echo explicitly — the server must repeat the exact label.
    import websockets
    from websockets.typing import Subprotocol

    settings = settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN})
    with _live_server(settings) as port:
        uri = f"ws://127.0.0.1:{port}/ws/events"
        async with websockets.connect(uri, subprotocols=[Subprotocol(_label(TOKEN))]) as ws:
            assert ws.subprotocol == _label(TOKEN)


# --- the subprotocol echo, on every WS endpoint (TestClient) -----------------
#
# The middleware validates the token from the offered subprotocol; the endpoint
# must then *echo* that exact label on accept, or a browser fails the handshake.
# The echo is one shared helper (`ws_subprotocol_to_echo`) called from all four
# endpoints, exercised here end to end through the full ASGI stack.


def test_ws_subprotocol_to_echo_returns_the_offered_label() -> None:
    assert ws_subprotocol_to_echo({"subprotocols": [_label(TOKEN)]}) == _label(TOKEN)


def test_ws_subprotocol_to_echo_is_none_without_a_token_label() -> None:
    # A native/test client that authenticates by header, or offers unrelated
    # subprotocols, gets a plain accept (subprotocol=None) — unchanged behavior.
    assert ws_subprotocol_to_echo({"subprotocols": []}) is None
    assert ws_subprotocol_to_echo({"subprotocols": ["graphql-ws", "chat"]}) is None


def _enforced_app(settings: Settings) -> FastAPI:
    return create_app(settings.model_copy(update={"enforce_auth": True, "auth_token": TOKEN}))


#: The endpoints whose handshake needs no prior REST setup. `/ws/agent/{id}` is
#: covered separately because it 404s an unknown session before it can accept.
_SIMPLE_WS_PATHS = ["/ws/events", "/ws/terminal", "/ws/office-host"]


@pytest.mark.parametrize("path", _SIMPLE_WS_PATHS)
def test_ws_echoes_offered_subprotocol(settings: Settings, path: str) -> None:
    with (
        TestClient(_enforced_app(settings)) as client,
        client.websocket_connect(
            path, subprotocols=[_label(TOKEN)], headers={"origin": LOCAL_ORIGIN}
        ) as ws,
    ):
        assert ws.accepted_subprotocol == _label(TOKEN)


@pytest.mark.parametrize("path", _SIMPLE_WS_PATHS)
def test_ws_without_token_is_rejected(settings: Settings, path: str) -> None:
    with (
        TestClient(_enforced_app(settings)) as client,
        client.websocket_connect(path, headers={"origin": LOCAL_ORIGIN}) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_text()
    assert exc.value.code == WS_CLOSE_POLICY


def test_ws_wrong_subprotocol_token_is_rejected(settings: Settings) -> None:
    with (
        TestClient(_enforced_app(settings)) as client,
        client.websocket_connect(
            "/ws/events", subprotocols=[_label("nope")], headers={"origin": LOCAL_ORIGIN}
        ) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_text()
    assert exc.value.code == WS_CLOSE_POLICY


def test_ws_foreign_origin_is_rejected_through_app(settings: Settings) -> None:
    with (
        TestClient(_enforced_app(settings)) as client,
        client.websocket_connect(
            "/ws/events", subprotocols=[_label(TOKEN)], headers={"origin": "http://evil.com"}
        ) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_text()
    assert exc.value.code == WS_CLOSE_POLICY


def test_ws_agent_echoes_offered_subprotocol(tmp_path: Path) -> None:
    # The fourth endpoint, which needs a live session id first. Fake-agent mode
    # gives one with no Claude login; the REST create carries the token too.
    settings = Settings(
        workspace_root=tmp_path,
        claude_projects_dir=tmp_path / "projects",
        fake_agent=True,
        enforce_auth=True,
        auth_token=TOKEN,
    )
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/api/agents/sessions", json={"folder": ""}, headers={"X-Workbench-Token": TOKEN}
        )
        assert resp.status_code == 200
        local_id = resp.json()["session_id"]
        with client.websocket_connect(
            f"/ws/agent/{local_id}",
            subprotocols=[_label(TOKEN)],
            headers={"origin": LOCAL_ORIGIN},
        ) as ws:
            assert ws.accepted_subprotocol == _label(TOKEN)
