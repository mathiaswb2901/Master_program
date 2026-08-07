"""The per-launch token handout (M5 item 8 / OSS-bar item 1).

The one endpoint under ``/api/`` exempt from the token requirement, because a
client with no token has to be able to fetch one (chicken-and-egg). That
exemption is the anti-rebind hole, so this router guards the handout itself:
the request's Origin (when present) *and* its Host must both name this machine.
DNS-rebinding turns on a forged Host header — the attacker's page resolves its
own hostname to 127.0.0.1 and the browser sends that hostname as Host — so
refusing a non-local Host is what keeps a rebound page from lifting the token.
"""

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from workbench_server.models.api import AuthTokenResponse
from workbench_server.services.local_auth import is_local_origin

log = structlog.get_logger()

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/token")
def token(request: Request) -> Response:
    """Hand out the per-launch token to a local caller.

    Returns 403 to a request whose Origin or Host is not local — the guard that
    stands in for the token check this one endpoint cannot require.
    """
    origin = request.headers.get("origin")
    if origin is not None and not is_local_origin(origin):
        log.warning("auth.token_denied", reason="foreign_origin", origin=origin)
        return _forbidden()
    # The Host header is always present on HTTP/1.1; treat a missing one as
    # unroutable rather than local.
    host = request.headers.get("host")
    if host is None or not is_local_origin(host):
        log.warning("auth.token_denied", reason="foreign_host", host=host)
        return _forbidden()
    body = AuthTokenResponse(token=request.app.state.auth_token)
    return JSONResponse(body.model_dump())


def _forbidden() -> Response:
    return Response(content="Forbidden", status_code=403, media_type="text/plain")
