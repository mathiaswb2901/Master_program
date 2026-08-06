"""Claude plan usage REST API. Thin: the service owns the accumulation."""

from fastapi import APIRouter, Request

from workbench_server.models.usage import UsageSnapshot
from workbench_server.services.usage import UsageService

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("")
async def usage(request: Request) -> UsageSnapshot:
    """The last snapshot and its age, for initial load and for reconnects (the
    live updates ride ``/ws/events`` as ``usage``).

    An empty ``buckets`` with a null ``observed_at`` is a valid answer and the
    most likely one: the figures only exist once a live session's stream has
    carried a rate-limit transition, and an account may never emit one.

    ``async def`` on purpose: this reads an in-memory snapshot with nothing
    blocking in it, and the service is loop-affine (same reasoning as
    ``routers/provenance.py``).
    """
    service: UsageService = request.app.state.usage
    return service.snapshot()
