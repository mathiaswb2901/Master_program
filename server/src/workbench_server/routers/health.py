"""Liveness endpoint; also the canary that settings resolved sanely."""

from fastapi import APIRouter, Request

from workbench_server import __version__
from workbench_server.config import Settings
from workbench_server.models.api import HealthResponse

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        version=__version__,
        workspace=settings.resolved_workspace(),
    )
