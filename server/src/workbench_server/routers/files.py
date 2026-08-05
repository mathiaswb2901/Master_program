"""File REST API. Thin: validate, delegate to Workspace, map domain errors to HTTP."""

from fastapi import APIRouter, HTTPException, Query, Request

from workbench_server.models.files import (
    CreateRequest,
    FileContent,
    OkResponse,
    RenameRequest,
    TreeNode,
    WriteRequest,
    WriteResponse,
)
from workbench_server.services.provenance import ProvenanceService
from workbench_server.services.workspace import (
    NotTextError,
    PathOutsideWorkspaceError,
    StaleWriteError,
    Workspace,
)

router = APIRouter(prefix="/api/files", tags=["files"])


def _ws(request: Request) -> Workspace:
    workspace: Workspace = request.app.state.workspace
    return workspace


def _provenance(request: Request) -> ProvenanceService:
    service: ProvenanceService = request.app.state.provenance
    return service


@router.get("/tree")
def tree(request: Request) -> TreeNode:
    return _ws(request).tree()


@router.get("/content")
def read(request: Request, path: str = Query(min_length=1)) -> FileContent:
    try:
        content, digest = _ws(request).read_text(path)
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except FileNotFoundError as e:
        raise HTTPException(404, "file not found") from e
    except NotTextError as e:
        raise HTTPException(415, "not an editable text file") from e
    return FileContent(path=path, content=content, hash=digest)


@router.put("/content")
def write(request: Request, body: WriteRequest) -> WriteResponse:
    try:
        digest = _ws(request).write_text(body.path, body.content, body.expected_hash)
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except StaleWriteError as e:
        raise HTTPException(409, "file changed on disk since it was read") from e
    # This save is the user's. Said out loud so the watcher event it produces is
    # not attributed to an agent that wrote the same file moments earlier.
    _provenance(request).note_user_write(body.path)
    return WriteResponse(path=body.path, hash=digest)


@router.post("/create")
def create(request: Request, body: CreateRequest) -> OkResponse:
    try:
        _ws(request).create(body.path, body.kind)
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except FileExistsError as e:
        raise HTTPException(409, "already exists") from e
    # Same reason as `write`: this is the user's doing, and the `added` event it
    # produces must not land on an agent claim for that path.
    _provenance(request).note_user_write(body.path)
    return OkResponse()


@router.post("/rename")
def rename(request: Request, body: RenameRequest) -> OkResponse:
    try:
        _ws(request).rename(body.path, body.new_path)
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except FileNotFoundError as e:
        raise HTTPException(404, "file not found") from e
    except FileExistsError as e:
        raise HTTPException(409, "target already exists") from e
    # The new path appears as an `added` event; renaming onto a name an agent
    # had just claimed would otherwise be credited to it.
    _provenance(request).note_user_write(body.new_path)
    return OkResponse()


@router.delete("/content")
def delete(request: Request, path: str = Query(min_length=1)) -> OkResponse:
    try:
        _ws(request).delete(path)
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except FileNotFoundError as e:
        raise HTTPException(404, "file not found") from e
    except IsADirectoryError as e:
        raise HTTPException(400, "directory deletion is not supported") from e
    return OkResponse()
