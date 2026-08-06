"""Mission Control's orchestrator endpoints. Thin: every rule is in the service."""

import structlog
from fastapi import APIRouter, Request

from workbench_server.models.orchestrator import OrchestratorSnapshot
from workbench_server.services.orchestrator import OrchestratorService

log = structlog.get_logger()

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])


@router.get("")
def snapshot(request: Request) -> OrchestratorSnapshot:
    """Every orchestrator, its workers and the budget — load and reconnect."""
    service: OrchestratorService = request.app.state.orchestrator
    return service.snapshot()


@router.post("/sessions/{orchestrator_id}/stop")
async def stop(request: Request, orchestrator_id: str) -> OrchestratorSnapshot:
    """Stop this orchestrator's whole crew and give their slots back.

    Deliberately succeeds for an unknown id: the board's Stop button means "make
    sure nothing of this one is running", and a 404 for an orchestrator whose
    workers already exited would be a scary answer to a request that got what it
    asked for. The snapshot that comes back is the proof.
    """
    service: OrchestratorService = request.app.state.orchestrator
    stopped = await service.stop(orchestrator_id)
    log.info("orchestrator.stopped_by_user", orchestrator=orchestrator_id, workers=stopped)
    return service.snapshot()
