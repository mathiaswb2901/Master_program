"""Office host endpoints. Thin: the service owns the lifecycle and the events.

``GET /hosts`` is the reconnect path — the live updates ride ``/ws/events`` as
``office_host`` frames, and a client that missed them re-reads the same truth
here.

``/ws/office-host`` is the other direction, and the only one: a WebSocket the
**desktop shell** holds open so the server can ask it to reparent, move, hide
and release a window. Nothing else may usefully connect — a browser tab has no
native window to host into — and nothing here decides that: the shell connects
because ``isTauri()`` was true, and the service reports ``shell_attached`` so
the UI degrades from a fact.
"""

import contextlib

import structlog
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from workbench_server.models.office_host import (
    HostReason,
    OfficeCapabilities,
    OfficeHostInfo,
    OfficeHostList,
    OfficeIdentity,
    OpenHostRequest,
    SetBoundsRequest,
    SetVisibleRequest,
)
from workbench_server.services.local_auth import ws_subprotocol_to_echo
from workbench_server.services.office import OfficeService
from workbench_server.services.office_host import (
    HostNotFoundError,
    HostRefusedError,
    HostStateError,
    OfficeHostService,
)
from workbench_server.services.office_host.shell_channel import ShellChannel
from workbench_server.services.workspace import PathOutsideWorkspaceError

log = structlog.get_logger()

router = APIRouter(prefix="/api/office", tags=["office-host"])

#: A refusal is a policy answer, not a crash — each one maps to the status that
#: says *why* without the client parsing prose.
_REFUSAL_STATUS: dict[HostReason, int] = {
    "native_hosting_disabled": 503,
    "unsupported_file": 415,
    # Both are "the thing you asked for is in use" rather than a failure: the
    # document in someone else's window, or the whole application in the user's
    # own session. 409 either way, and the reason in the body says which.
    "powerpoint_already_running": 409,
    "document_open_elsewhere": 409,
}


def _hosts(request: Request) -> OfficeHostService:
    service: OfficeHostService = request.app.state.office_host
    return service


def _office(request: Request) -> OfficeService:
    service: OfficeService = request.app.state.office
    return service


@router.get("/capabilities")
def capabilities(request: Request) -> OfficeCapabilities:
    """What this machine can actually do with a document — native hosting,
    OnlyOffice, or read-only preview. The UI degrades from this, never from a
    guess."""
    return _hosts(request).capabilities(_office(request).enabled)


@router.get("/identity")
async def identity(request: Request) -> OfficeIdentity:
    """Which Microsoft account this machine's Office is signed in as, and whether
    it is licensed to edit. Read-only and best-effort: ``unknown`` / ``None``
    where the machine will not say, so the UI degrades from a fact."""
    return await _hosts(request).identity()


@router.post("/host")
async def open_host(request: Request, body: OpenHostRequest) -> OfficeHostInfo:
    """Host a document. A live host for the same path is returned as-is (and
    re-embedded if it was detached) rather than duplicated."""
    try:
        return await _hosts(request).open(body.path, body.rect)
    except HostRefusedError as e:
        raise HTTPException(_REFUSAL_STATUS.get(e.reason, 409), str(e)) from e
    except HostStateError as e:
        # Reuse lost a race: the live host for this path settled between being
        # found and being re-embedded. A conflict, exactly as it is for the
        # move and detach below — never a 500.
        raise HTTPException(409, str(e)) from e
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except FileNotFoundError as e:
        raise HTTPException(404, "file not found") from e


@router.get("/hosts")
def list_hosts(request: Request) -> OfficeHostList:
    return _hosts(request).snapshot()


@router.post("/host/{host_id}/bounds")
async def set_bounds(request: Request, host_id: str, body: SetBoundsRequest) -> OfficeHostInfo:
    try:
        return await _hosts(request).set_bounds(host_id, body.rect)
    except HostNotFoundError as e:
        raise HTTPException(404, "no such host") from e
    except HostStateError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/host/{host_id}/visible")
async def set_visible(request: Request, host_id: str, body: SetVisibleRequest) -> OfficeHostInfo:
    """The panel went behind another editor tab, or came back. A real window
    does not hide itself because a ``div`` did."""
    try:
        return await _hosts(request).set_visible(host_id, body.visible)
    except HostNotFoundError as e:
        raise HTTPException(404, "no such host") from e
    except HostStateError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/host/{host_id}/detach")
async def detach_host(request: Request, host_id: str) -> OfficeHostInfo:
    """Give the window back to the desktop; the document stays open."""
    try:
        return await _hosts(request).detach(host_id)
    except HostNotFoundError as e:
        raise HTTPException(404, "no such host") from e
    except HostStateError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/host/{host_id}/close")
async def close_host(request: Request, host_id: str) -> OfficeHostInfo:
    """Close the instance we launched. Idempotent — a settled host comes back
    with the state it settled in."""
    try:
        return await _hosts(request).close(host_id)
    except HostNotFoundError as e:
        raise HTTPException(404, "no such host") from e


#: WebSockets live under `/ws/*` like every other push channel here — the dev
#: proxy upgrades that prefix and nothing else, so a socket under `/api` works
#: in production and dies in `npm run dev`.
ws_router = APIRouter()


@ws_router.websocket("/ws/office-host")
async def host_channel(ws: WebSocket) -> None:
    """The desktop shell's socket: commands out, acks back.

    Held open for the life of the window. Disconnecting is how the server learns
    there is nothing to host into any more, so there is no goodbye message and
    no heartbeat — the socket *is* the presence.
    """
    channel: ShellChannel | None = getattr(ws.app.state, "office_host_channel", None)
    if channel is None:
        await ws.close(code=1011, reason="native Office hosting is not configured")
        return
    # Echo the offered token subprotocol (middleware already validated it), or a
    # browser fails the handshake. The desktop shell is the only real client; it
    # offers the token as a subprotocol like every other socket.
    await ws.accept(subprotocol=ws_subprotocol_to_echo(ws.scope))
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await channel.serve(ws)
