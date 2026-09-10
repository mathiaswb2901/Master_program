"""Validation REST API. Thin: the service owns the checks and the map."""

import structlog
from fastapi import APIRouter, HTTPException, Request

from workbench_server.models.evidence import EvidencePayload
from workbench_server.models.validation import (
    ApproveRequest,
    EvidenceKind,
    ValidationResult,
    ValidationResults,
    ValidationSpec,
)
from workbench_server.models.validation_store import EvidenceExport
from workbench_server.services.validation import ValidationService
from workbench_server.services.validation_store import payload_envelope

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
    # One narrowing, shared with the disk source that has to write the same
    # envelope (`services/validation_store.py`) — two copies of it would drift
    # the day a fourth payload kind lands and only one of them is visited.
    envelope = payload_envelope(kind, ref, found)
    if envelope is None:
        # A payload shape this build has no field for. Refused rather than
        # returned as an envelope with every field null, which a client can only
        # read as either "empty" or "broken" — the emptiness AXI shape 2 forbids.
        log.warning(
            "validation.payload_unrenderable", kind=kind, ref=ref, shape=type(found).__name__
        )
        raise HTTPException(status_code=404, detail="no payload shape this server can render")
    return envelope


@router.get("/{validation_id}")
async def result(request: Request, validation_id: str) -> ValidationResult:
    """One result by id. 404 once the LRU has evicted it — a client holding a
    stale handle is told it is gone, not handed a guess."""
    found = _service(request).get(validation_id)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown validation")
    return found


@router.post("/{validation_id}/export")
async def export(request: Request, validation_id: str) -> EvidenceExport:
    """Render this result as a one-page Markdown report and write it into the
    workspace (``.workbench/validation/exports/<id>.md``).

    A POST because it leaves a file behind — which is the point: a proof you can
    hand to someone is a file, not a string in a terminal that scrolled away. The
    rendered document rides the response too, so the panel that asked for it can
    show it without reading the file back. **404** for an id the server no longer
    holds, exactly as ``approve`` answers one.
    """
    try:
        written = _service(request).export(validation_id)
    except OSError as err:
        log.warning("validation.export_failed", validation_id=validation_id, error=str(err))
        raise HTTPException(
            status_code=500, detail=f"could not write the report: {err.strerror or err}"
        ) from err
    if written is None:
        raise HTTPException(status_code=404, detail="unknown or superseded validation")
    return written


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
