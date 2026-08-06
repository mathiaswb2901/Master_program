"""Managed worktree pool endpoints. Thin: the service owns git and the events.

``GET /api/worktrees`` is the reconnect path — live changes ride ``/ws/events``
as ``worktree_changed`` frames, and a client that missed them re-reads the same
truth here.

Every refusal below is a *policy* answer with a status that says which one it
is, so a caller never has to parse prose: 409 for "the pool is full" and "that
lease does not hold this slot", 503 for "there is no pool here at all".
"""

import structlog
from fastapi import APIRouter, HTTPException, Request, status

from workbench_server.models.worktrees import (
    AcquireWorktreeRequest,
    PruneRequest,
    PruneResult,
    ReleaseWorktreeRequest,
    RenewWorktreeRequest,
    WorktreeInfo,
    WorktreePool,
)
from workbench_server.services.worktrees import (
    GitError,
    LeaseError,
    PoolExhaustedError,
    PoolUnavailableError,
    SlotNotFoundError,
    WorktreeService,
)

log = structlog.get_logger()

router = APIRouter(prefix="/api/worktrees", tags=["worktrees"])


def _service(request: Request) -> WorktreeService:
    service: WorktreeService = request.app.state.worktrees
    return service


@router.get("")
def list_worktrees(request: Request) -> WorktreePool:
    """Every slot, plus why the pool might be empty or held.

    Never 500s: a machine with no git and a workspace that is not a repository
    both come back as a pool with a ``problem``, exactly like a layouts file
    that could not be read.
    """
    return _service(request).snapshot()


@router.post("/acquire")
async def acquire(request: Request, body: AcquireWorktreeRequest) -> WorktreeInfo:
    """Borrow a slot, detached at ``base`` (default: the repository's HEAD)."""
    try:
        return await _service(request).acquire(body)
    except PoolUnavailableError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    except PoolExhaustedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except GitError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"git: {e}") from e


@router.post("/{slot}/release")
async def release(request: Request, slot: str, body: ReleaseWorktreeRequest) -> WorktreeInfo:
    """Give a slot back. A slot holding uncommitted work is parked as ``dirty``
    rather than reset — ``discard_changes`` is the caller saying otherwise."""
    try:
        return await _service(request).release(
            slot, body.lease_id, discard_changes=body.discard_changes
        )
    except SlotNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such slot") from e
    except LeaseError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "that lease does not hold this slot") from e
    except GitError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"git: {e}") from e


@router.post("/{slot}/renew")
async def renew(request: Request, slot: str, body: RenewWorktreeRequest) -> WorktreeInfo:
    """Push the lease deadline out. A lease that cannot be renewed is a timeout,
    and a slot reclaimed under a working agent is the failure this prevents."""
    try:
        return await _service(request).renew(slot, body.lease_id, body.ttl_seconds)
    except SlotNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such slot") from e
    except LeaseError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "that lease does not hold this slot") from e


@router.post("/prune")
async def prune(request: Request, body: PruneRequest) -> PruneResult:
    """Reclaim slots whose lease has expired *and* whose owner is gone.

    ``force`` additionally discards uncommitted work — the one irreversible
    thing the pool can do — so it is never implied by anything else.
    """
    try:
        return await _service(request).prune(force=body.force)
    except PoolUnavailableError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    except GitError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"git: {e}") from e
