"""Local-API security hardening: a per-launch token and a WS Origin allowlist.

The server binds loopback, but loopback is not private: remote web content a
user's browser loads (drive-by/CSRF, and DNS-rebinding that resolves an
attacker hostname to 127.0.0.1) can still fire REST and WebSocket requests at
the backend. Two defenses live here, behind one rollout flag:

* a per-launch bearer token, required on everything under ``/api/`` and
  ``/ws/`` except the handful of bootstrap endpoints that cannot require it; and
* a strict Origin allowlist on the WebSocket handshake, because the browser
  attaches an ``Origin`` we can trust for cross-site requests where CORS does
  not gate a WS upgrade.

This is a **raw ASGI** middleware, not a ``BaseHTTPMiddleware`` subclass,
because it must gate the WebSocket handshake as well as HTTP — and the
handshake is only visible at the ASGI ``scope['type'] == 'websocket'`` layer,
before any route runs.

``enforce`` is the rollout seam. When it is ``False`` — the shipped default of
the first PR — every request passes through untouched, so the plumbing lands
with zero behavior change. A later PR flips it on once the client injects the
token and sends a local Origin.
"""

from collections.abc import Callable
from secrets import compare_digest
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

#: WebSocket close code for a rejected handshake. In the 4000-4999 private range
#: so it never collides with a protocol-defined code; a client that reads the
#: close frame sees "the server refused you", not "the connection dropped". For
#: this to reach the client at all the connection must be *accepted* first —
#: closing before the 101 upgrade makes uvicorn fail the handshake with a flat
#: HTTP 403 and the browser reports a bare 1006 with no code (see ``_reject_ws``).
WS_CLOSE_POLICY = 4403

#: A client offering the token as a WebSocket subprotocol prefixes it with this,
#: because a browser ``WebSocket`` cannot set request headers but *can* offer
#: subprotocols. ``workbench.auth.<token>``. Not a secret itself, just a label.
WS_TOKEN_SUBPROTOCOL_PREFIX = "workbench.auth."  # noqa: S105

#: The header the REST client (and a header-capable WS client) sends the token
#: in. Lower-cased here because ASGI header names arrive lower-cased.
TOKEN_HEADER = b"x-workbench-token"
ORIGIN_HEADER = b"origin"

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_local_origin(value: str) -> bool:
    """True when ``value`` names this machine — loopback IPs or the reserved
    ``localhost`` TLD (RFC 6761, which also covers the Tauri shell's
    ``tauri.localhost``). Accepts a full Origin (``http://localhost:5173``) or a
    bare ``Host`` (``127.0.0.1:8787``); the port is ignored.
    """
    # A bare Host header has no scheme; give urlsplit an authority to parse so
    # its ``hostname`` (lower-cased, port and brackets stripped) does the work.
    parsed = urlsplit(value if "://" in value else f"//{value}")
    host = parsed.hostname
    if host is None:
        return False
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, val in scope.get("headers", []):
        if key == name:
            return str(val.decode("latin-1"))
    return None


def _ws_subprotocol_token(scope: Scope) -> str | None:
    """The token offered as a ``workbench.auth.<token>`` subprotocol, if any.

    ASGI puts the offered subprotocols in ``scope['subprotocols']`` (already
    split from the comma-separated ``Sec-WebSocket-Protocol`` header).
    """
    for proto in scope.get("subprotocols", []):
        if proto.startswith(WS_TOKEN_SUBPROTOCOL_PREFIX):
            return str(proto[len(WS_TOKEN_SUBPROTOCOL_PREFIX) :])
    return None


def ws_subprotocol_to_echo(scope: Scope) -> str | None:
    """The full ``workbench.auth.<token>`` label a WS endpoint must echo on accept.

    A browser **fails** any handshake whose offered subprotocol the server does
    not echo back in the 101 response, so every WS endpoint accepts with this:
    ``await ws.accept(subprotocol=ws_subprotocol_to_echo(ws.scope))``. It returns
    the label verbatim (not just the token) because that whole string is what the
    ``Sec-WebSocket-Protocol`` response header must repeat. ``None`` when the
    client offered no token label — native and test clients that authenticate by
    header, or don't authenticate at all — which accepts with no subprotocol,
    exactly as before this PR. The token *validation* already happened in the
    middleware before the endpoint ran; this only mirrors the label back.
    """
    for proto in scope.get("subprotocols", []):
        if proto.startswith(WS_TOKEN_SUBPROTOCOL_PREFIX):
            return str(proto)
    return None


class LocalAuthMiddleware:
    """Gate REST + WS on a per-launch token, and WS additionally on Origin.

    See the module docstring. ``is_local_origin`` is injected rather than
    imported so a test can substitute its own allowlist.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        enforce: bool,
        is_local_origin: Callable[[str], bool],
    ) -> None:
        self._app = app
        self._token = token
        self._enforce = enforce
        self._is_local_origin = is_local_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The whole point of the rollout flag: wired but inert until flipped.
        if not self._enforce:
            await self._app(scope, receive, send)
            return
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:  # lifespan and anything else is not ours to gate.
            await self._app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = scope["method"]
        path = scope["path"]
        if self._http_exempt(method, path) or self._token_ok(_header(scope, TOKEN_HEADER)):
            await self._app(scope, receive, send)
            return
        await self._reject_http(send)

    def _http_exempt(self, method: str, path: str) -> bool:
        # CORS preflight carries no credentials and must reach the CORS layer.
        if method == "OPTIONS":
            return True
        # The static UI mount and any non-API path is not token-gated; only the
        # API and WebSocket surfaces are.
        if not (path.startswith("/api/") or path.startswith("/ws/")):
            return True
        # Bootstrap endpoints a tokenless client must still reach: liveness, and
        # the token handout itself (which guards on Origin/Host instead).
        return method == "GET" and path in ("/api/health", "/api/auth/token")

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        origin = _header(scope, ORIGIN_HEADER)
        # Absent Origin is allowed on purpose: native and test WS clients send
        # none, and only a browser attaches one we could distrust. Present and
        # foreign → reject before the endpoint runs.
        if origin is not None and not self._is_local_origin(origin):
            await self._reject_ws(receive, send)
            return
        token = _ws_subprotocol_token(scope) or _header(scope, TOKEN_HEADER)
        if self._token_ok(token):
            await self._app(scope, receive, send)
            return
        await self._reject_ws(receive, send)

    def _token_ok(self, offered: str | None) -> bool:
        if offered is None:
            return False
        return compare_digest(offered, self._token)

    @staticmethod
    async def _reject_http(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"Forbidden"})

    @staticmethod
    async def _reject_ws(receive: Receive, send: Send) -> None:
        # Accept the handshake, then immediately close with the policy code.
        # Accepting first is what makes the close code *reach the client*: a
        # close sent before the 101 upgrade is dropped to a flat HTTP 403 by
        # uvicorn, which a browser reports as an opaque 1006 with no code. We
        # drain the ``websocket.connect`` event before accepting so the accept
        # is spec-valid, and never dispatch downstream — the endpoint never runs.
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": WS_CLOSE_POLICY})
