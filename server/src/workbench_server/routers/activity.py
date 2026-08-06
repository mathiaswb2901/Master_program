"""Live agent activity REST API. Thin: the service owns the window."""

from fastapi import APIRouter, Request

from workbench_server.models.activity import ActivitySnapshot
from workbench_server.services.activity import ActivityService

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
async def activity(request: Request) -> ActivitySnapshot:
    """The whole fleet as it stands, for initial load and for reconnects (live
    updates ride ``/ws/events`` as ``session_activity``).

    An empty ``sessions`` is a valid answer and the common one: no agent
    sessions are running. It is also what a restart reports — this is live state
    about processes, never persisted.

    ``async def`` on purpose: this reads an in-memory snapshot with nothing
    blocking in it, and the service is loop-affine (same reasoning as
    ``routers/usage.py``).
    """
    service: ActivityService = request.app.state.activity
    return service.snapshot()
