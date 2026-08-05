"""Layout persistence REST API. Thin: the service owns the file."""

from fastapi import APIRouter, HTTPException, Request, status

from workbench_server.models.files import OkResponse
from workbench_server.models.layouts import LayoutsResponse, LayoutsState
from workbench_server.services.layouts import LayoutsService, LayoutTooLargeError

router = APIRouter(prefix="/api/layouts", tags=["layouts"])


def _service(request: Request) -> LayoutsService:
    service: LayoutsService = request.app.state.layouts
    return service


@router.get("")
def layouts(request: Request) -> LayoutsResponse:
    """This workspace's saved arrangements, plus the reason the file was ignored
    if it could not be read. Never 500s on a bad file — see the service."""
    return _service(request).load()


@router.put("")
def put_layouts(request: Request, body: LayoutsState) -> OkResponse:
    """Replace the document. The UI holds the authoritative list and writes it
    whole, so this is the only write endpoint and it is idempotent."""
    try:
        _service(request).save(body)
    except LayoutTooLargeError as err:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"layout document too large: {err}"
        ) from err
    except OSError as err:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"could not write layouts: {err.strerror or err}"
        ) from err
    return OkResponse()
