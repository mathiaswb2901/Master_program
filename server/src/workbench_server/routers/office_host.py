"""Office host endpoints. Thin: the service owns the lifecycle and the events.

``GET /hosts`` is the reconnect path — the live updates ride ``/ws/events`` as
``office_host`` frames, and a client that missed them re-reads the same truth
here.
"""

import structlog
from fastapi import APIRouter, HTTPException, Request

from workbench_server.models.office_host import (
    HostReason,
    OfficeCapabilities,
    OfficeHostInfo,
    OfficeHostList,
    OpenHostRequest,
    SetBoundsRequest,
)
from workbench_server.services.office import OfficeService
from workbench_server.services.office_host import (
    HostNotFoundError,
    HostRefusedError,
    HostStateError,
    OfficeHostService,
)
from workbench_server.services.workspace import PathOutsideWorkspaceError

log = structlog.get_logger()

router = APIRouter(prefix="/api/office", tags=["office-host"])

#: A refusal is a policy answer, not a crash — each one maps to the status that
#: says *why* without the client parsing prose.
_REFUSAL_STATUS: dict[HostReason, int] = {
    "native_hosting_disabled": 503,
    "unsupported_file": 415,
    "powerpoint_preview_only": 409,
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


@router.post("/host")
async def open_host(request: Request, body: OpenHostRequest) -> OfficeHostInfo:
    """Host a document. A live host for the same path is returned as-is (and
    re-embedded if it was detached) rather than duplicated."""
    try:
        return await _hosts(request).open(body.path, body.rect)
    except HostRefusedError as e:
        raise HTTPException(_REFUSAL_STATUS.get(e.reason, 409), str(e)) from e
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
