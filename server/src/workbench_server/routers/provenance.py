"""Provenance REST API. Thin: the service owns the correlation and the map."""

from fastapi import APIRouter, Request

from workbench_server.models.files import OkResponse
from workbench_server.models.provenance import AcknowledgeRequest, ProvenanceMap
from workbench_server.services.provenance import ProvenanceService

router = APIRouter(prefix="/api/provenance", tags=["provenance"])


def _service(request: Request) -> ProvenanceService:
    service: ProvenanceService = request.app.state.provenance
    return service


@router.get("")
async def provenance(request: Request) -> ProvenanceMap:
    """The current map, for initial load and for reconnects (the live updates
    ride ``/ws/events`` as ``file_provenance``).

    ``async def`` on purpose: this reads an in-memory map with nothing blocking
    in it, and the service is loop-affine (see ``services/provenance.py``). A
    sync handler would run it in FastAPI's threadpool instead.
    """
    return _service(request).snapshot()


@router.post("/acknowledge")
async def acknowledge(request: Request, body: AcknowledgeRequest) -> OkResponse:
    """The user opened or dismissed this change. A path with no entry is a
    no-op, not a 404: the UI acknowledges on every open.

    ``async def`` is load-bearing here: acknowledging publishes a
    ``file_provenance`` frame onto every ``/ws/events`` subscriber queue, and
    ``asyncio.Queue.put_nowait`` from a threadpool thread wakes the waiting
    socket only when something else happens to wake the loop — the other
    windows' markers would hang lit until then.
    """
    _service(request).acknowledge(body.path)
    return OkResponse()
