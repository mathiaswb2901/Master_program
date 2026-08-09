"""Settings REST API. Thin: the service owns the file and the precedence.

Two endpoints over one document. ``GET`` answers with everything the panel
renders — the stored choices, what is actually in force, any override from the
environment, and the zero-telemetry statement — and ``PUT`` replaces the stored
choices and answers with the same shape, so a save needs no follow-up read.
"""

from fastapi import APIRouter, HTTPException, Request, status

from workbench_server.models.settings import SettingsState, WorkbenchSettings
from workbench_server.services.settings import SettingsService

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _service(request: Request) -> SettingsService:
    service: SettingsService = request.app.state.settings_service
    return service


@router.get("")
def settings(request: Request) -> SettingsState:
    """The stored choices, what is in force, and why they differ. Never 500s on
    a bad document — the defaults are in force and ``problem`` says why."""
    return _service(request).state()


@router.put("")
def put_settings(request: Request, body: WorkbenchSettings) -> SettingsState:
    """Replace the stored choices. The client holds the whole document and
    writes it whole, so this is the only write endpoint and it is idempotent."""
    try:
        return _service(request).save(body)
    except OSError as err:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not write settings: {err.strerror or err}",
        ) from err
