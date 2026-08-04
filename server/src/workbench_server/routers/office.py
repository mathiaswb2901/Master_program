"""OnlyOffice endpoints: editor config for the UI; file pull + save push for the
Document Server. Always returns {"error": 0} to callbacks it processed or
deliberately ignored — a non-zero error makes the editor surface a scary banner."""

from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from workbench_server.models.office import (
    CallbackResponse,
    DocEditorConfig,
    ForcesaveRequest,
    LastSaveResponse,
    OfficeStatus,
    UiTheme,
)
from workbench_server.services.office import (
    InvalidOfficeTokenError,
    NotAnOfficeFileError,
    OfficeDisabledError,
    OfficeService,
)
from workbench_server.services.workspace import PathOutsideWorkspaceError

log = structlog.get_logger()

router = APIRouter(prefix="/api/office", tags=["office"])


def _svc(request: Request) -> OfficeService:
    service: OfficeService = request.app.state.office
    return service


def _http(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http
    return client


@router.get("/status")
def status(request: Request) -> OfficeStatus:
    svc = _svc(request)
    return OfficeStatus(enabled=svc.enabled, server_url=svc.server_url)


@router.get("/config")
def config(
    request: Request, path: str = Query(min_length=1), theme: UiTheme = "dark"
) -> DocEditorConfig:
    try:
        return _svc(request).editor_config(path, theme)
    except OfficeDisabledError as e:
        raise HTTPException(503, "office editing is not configured") from e
    except NotAnOfficeFileError as e:
        raise HTTPException(415, "not an office document") from e
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except FileNotFoundError as e:
        raise HTTPException(404, "file not found") from e


@router.get("/files/{token}")
def serve_file(request: Request, token: str) -> FileResponse:
    svc = _svc(request)
    try:
        path = svc.verify_path_token(token)
        file_path = request.app.state.workspace.safe_path(path)
    except (InvalidOfficeTokenError, OfficeDisabledError) as e:
        raise HTTPException(401, "invalid file token") from e
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    if not file_path.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(file_path)


@router.post("/callback/{token}")
async def callback(request: Request, token: str, body: dict[str, Any]) -> CallbackResponse:
    svc = _svc(request)
    try:
        path = svc.verify_path_token(token)
        await svc.handle_callback(path, body, _http(request))
    except (InvalidOfficeTokenError, OfficeDisabledError) as e:
        # invalid caller: this IS an error response — nobody legitimate loses saves
        raise HTTPException(401, "invalid callback token") from e
    except httpx.HTTPError:
        log.exception("office.save_download_failed")
        return CallbackResponse(error=1)
    return CallbackResponse()


@router.get("/last-save")
def last_save(request: Request, path: str = Query(min_length=1)) -> LastSaveResponse:
    """Hash of the last Document-Server save for a path; lets the UI ignore
    watcher events caused by the editor's own autosaves."""
    return LastSaveResponse(hash=_svc(request).last_save_hash(path))


@router.post("/forcesave")
async def forcesave(request: Request, body: ForcesaveRequest) -> CallbackResponse:
    try:
        flushed = await _svc(request).forcesave(body.path, _http(request))
    except OfficeDisabledError as e:
        raise HTTPException(503, "office editing is not configured") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, "document server unreachable") from e
    return CallbackResponse(error=0 if flushed else 1)
