"""First-run / Setup endpoint. Thin: the service owns the detection.

``GET /api/setup/status`` is the whole surface — the UI reads it once on a fresh
workspace to greet the user, and again when they open the walkthrough by hand.
There is no write half: dismissal is workspace state the UI persists through the
files API (``.workbench/setup.json``), beside ``welcome.json`` and
``layouts.json``, exactly as the welcome card does.
"""

from fastapi import APIRouter, Request

from workbench_server.models.setup import SetupStatus
from workbench_server.services.setup import SetupService

router = APIRouter(prefix="/api/setup", tags=["setup"])


def _setup(request: Request) -> SetupService:
    service: SetupService = request.app.state.setup
    return service


@router.get("/status")
def status(request: Request) -> SetupStatus:
    """What is wired up, said plainly — Claude login (detected, never performed),
    Office/OnlyOffice readiness (echoed from the office capabilities), and
    whether this is a fresh workspace."""
    return _setup(request).status()
