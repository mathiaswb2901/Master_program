"""Validation REST API. Thin: the service owns the checks and the map."""

import structlog
from fastapi import APIRouter, HTTPException, Request

from workbench_server.models.evidence import EvidencePayload
from workbench_server.models.gates import GateLog
from workbench_server.models.reconciliation import ReconciliationReport
from workbench_server.models.validation import (
    ApproveRequest,
    EvidenceKind,
    ValidationResult,
    ValidationResults,
    ValidationSpec,
)
from workbench_server.services.validation import ValidationService

log = structlog.get_logger()

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


@router.get("/payload/{kind}/{ref}")
async def payload(request: Request, kind: EvidenceKind, ref: str) -> EvidencePayload:
    """The detail behind one ``EvidenceItem.payload_ref``.

    The gap the #82 frame left: it stored payloads in a bounded per-kind LRU and
    shipped no way to redeem the reference, so ``payload_ref`` was a dead handle
    in the browser. Survivable for reconciliation (the grouped line carries the
    counts); not for a toolchain gate, whose *entire* value is the captured log.

    **404 once the LRU has dropped it** — the store is bounded and honest about
    it, and the Review panel renders that as "this log has been evicted" rather
    than a spinner that never resolves. Declared above ``/{validation_id}`` so
    the reading order matches the routing.
    """
    found = _service(request).payload(kind, ref)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown or evicted payload")
    if isinstance(found, ReconciliationReport):
        return EvidencePayload(kind=kind, ref=ref, reconciliation=found)
    if isinstance(found, GateLog):
        return EvidencePayload(kind=kind, ref=ref, gate_log=found)
    # A payload shape this build has no field for. Refused rather than returned
    # as an envelope with every field null, which a client can only read as
    # either "empty" or "broken" — the emptiness AXI shape 2 exists to forbid.
    log.warning("validation.payload_unrenderable", kind=kind, ref=ref, shape=type(found).__name__)
    raise HTTPException(status_code=404, detail="no payload shape this server can render")


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
