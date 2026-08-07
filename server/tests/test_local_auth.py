"""Local-API security hardening (M5 item 8 / OSS-bar item 1), PR1: plumbing.

The middleware is wired but shipped inert (``enforce_auth=False``), so the
enforcement paths are exercised here by building an app — and the middleware
directly — with ``enforce=True``. The last group asserts the shipped default is
a pure pass-through, which is what lets the rest of the suite stay untouched.
"""

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services.local_auth import (
    WS_CLOSE_POLICY,
    LocalAuthMiddleware,
    is_local_origin,
)

TOKEN = "sekret-launch-token"

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


async def test_default_app_does_not_enforce(settings: Settings) -> None:
    # The shipped default: a tokenless gated call sails through, so the rest of
    # the suite (which sends no token) is untouched.
    async with await _app_client(settings) as client:
        resp = await client.get("/api/layouts")
    assert resp.status_code != 403


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
