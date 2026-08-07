"""Validation REST API. Thin: the service owns the checks and the map."""

from fastapi import APIRouter, HTTPException, Request

from workbench_server.models.validation import (
    ApproveRequest,
    ValidationResult,
    ValidationResults,
    ValidationSpec,
)
from workbench_server.services.validation import ValidationService

router = APIRouter(prefix="/api/validation", tags=["validation"])


def _service(request: Request) -> ValidationService:
    service: ValidationService = request.app.state.validation
    return service


@router.post("/run")
async def run(request: Request, spec: ValidationSpec) -> ValidationResult:
    """Run the checks the spec names and return the assembled result. The result
    is also published on ``/ws/events`` as ``validation`` and held for reconnects.
    """
    return await _service(request).run(spec)


@router.get("")
async def results(request: Request) -> ValidationResults:
    """Every result currently held, for initial load and reconnect (the live
    updates ride ``/ws/events`` as ``validation``).

    An empty ``results`` is a valid answer and the common one: nothing has been
    validated yet, and it is also what a restart reports — in-memory only.
    """
    return _service(request).snapshot()


@router.get("/{validation_id}")
async def result(request: Request, validation_id: str) -> ValidationResult:
    """One result by id. 404 once the LRU has evicted it — a client holding a
    stale handle is told it is gone, not handed a guess."""
    found = _service(request).get(validation_id)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown validation")
    return found


@router.post("/{validation_id}/approve")
async def approve(request: Request, validation_id: str, body: ApproveRequest) -> ValidationResult:
    """Record the human decision on a result. The timestamp is server-minted.

    A stale/superseded id (evicted by the LRU) is **404**, not a 200 a caller
    could misread as an approval that landed — the Mission Control settled-
    permission precedent.
    """
    updated = _service(request).approve(validation_id, body.approver, body.note)
    if updated is None:
        raise HTTPException(status_code=404, detail="unknown or superseded validation")
    return updated
