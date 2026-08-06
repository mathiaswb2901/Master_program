"""Workspace REST API. Thin: the service owns the switch and the history."""

from fastapi import APIRouter, HTTPException, Request, status

from workbench_server.models.workspaces import SwitchWorkspaceRequest, WorkspaceState
from workbench_server.services.workspaces import WorkspaceRefusedError, WorkspaceService

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


def _service(request: Request) -> WorkspaceService:
    service: WorkspaceService = request.app.state.workspaces
    return service


@router.get("")
def workspace(request: Request) -> WorkspaceState:
    """The current root, whether anyone chose it, and where you have been."""
    return _service(request).state()


@router.post("/switch")
async def switch(request: Request, body: SwitchWorkspaceRequest) -> WorkspaceState:
    """Re-root the server. 400 with the reason for a root that cannot be served.

    One status for all three refusals (missing, a file, unreadable) on purpose:
    they differ in the *sentence*, which the UI shows, not in what the client
    should do about it — which is always "pick another folder".
    """
    try:
        return await _service(request).switch(body.path)
    except WorkspaceRefusedError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
