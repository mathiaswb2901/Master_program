"""Reconciliation specs REST API. Thin: the service owns the folder, the trust
record and the loop.

Four verbs and no fifth. There is deliberately **no** endpoint that accepts a
spec *body* — a spec is a checked-in file a person edits and a reviewer can see
in a diff, and an endpoint that took one would be a way to run workspace code
without anything on disk to point at afterwards.
"""

import structlog
from fastapi import APIRouter, HTTPException, Request

from workbench_server.models.reconcile_spec import (
    SpecApprovalRequest,
    SpecRunReport,
    SpecState,
    SpecStates,
)
from workbench_server.services.reconcile_spec import (
    ReconcileSpecService,
    SpecConflict,
    SpecProblem,
)

log = structlog.get_logger()

router = APIRouter(prefix="/api/reconcile", tags=["reconcile"])


def _service(request: Request) -> ReconcileSpecService:
    service: ReconcileSpecService = request.app.state.reconcile_specs
    return service


@router.get("/specs")
async def specs(request: Request) -> SpecStates:
    """Every spec in ``.workbench/reconcile/``, with whether it may run.

    An empty list is a valid and common answer — a workspace with no specs — and
    ``detail`` says so rather than leaving blankness to interpret (AXI shape 2).
    Reading this endpoint runs **nothing**: opening a workspace with twenty specs
    in it spawns no process and prompts for nothing.
    """
    return _service(request).states()


@router.post("/specs/{name}/approve")
async def approve(request: Request, name: str, body: SpecApprovalRequest) -> SpecState:
    """Record the one-time trust decision for this spec **and the code it names**.

    The caller echoes the digest it was shown. A digest that no longer matches is
    **409**: approving a spec whose bytes moved under the dialog would approve
    something nobody read. An unknown or unparseable spec is **404**/**422** with
    the reason, never a 200 a caller could misread as a decision that landed.
    """
    try:
        return _service(request).approve(name, body.approver, body.digest)
    except SpecConflict as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except SpecProblem as exc:
        raise HTTPException(status_code=404, detail=exc.reason) from exc


@router.post("/specs/{name}/revoke")
async def revoke(request: Request, name: str) -> SpecState:
    """Withdraw the decision. The spec stays where it is and simply runs nothing
    again until somebody says so. **404** when there is no such spec."""
    state = _service(request).revoke(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no spec named {name!r}")
    return state


@router.post("/specs/{name}/run")
async def run(request: Request, name: str) -> SpecRunReport:
    """Run one spec now — the manual half of a loop whose point is that nobody
    has to. An unapproved, stale or unreadable spec comes back with an outcome
    and a reason rather than an error: the refusal *is* the result."""
    return await _service(request).run(name, trigger="manual")
