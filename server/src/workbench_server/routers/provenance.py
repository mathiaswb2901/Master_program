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
def provenance(request: Request) -> ProvenanceMap:
    """The current map, for initial load and for reconnects (the live updates
    ride ``/ws/events`` as ``file_provenance``)."""
    return _service(request).snapshot()


@router.post("/acknowledge")
def acknowledge(request: Request, body: AcknowledgeRequest) -> OkResponse:
    """The user opened or dismissed this change. A path with no entry is a
    no-op, not a 404: the UI acknowledges on every open."""
    _service(request).acknowledge(body.path)
    return OkResponse()
