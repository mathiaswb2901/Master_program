"""Notebook read API. Thin: jail, delegate, map domain errors to HTTP.

One route, and deliberately only one. There is no run endpoint, no kernel
endpoint and no write endpoint, because there is no execution in this feature
(ROADMAP M4) — the surface a client can reach is exactly "show me this file".

The one status code worth naming is **422 for a file that will not parse**. It
is not a 500 (nothing broke) and not a 404 (the file is right there); it is the
document being unreadable, and the detail carries the reason so the panel's
degraded card can say what failed instead of going blank.
"""

from fastapi import APIRouter, HTTPException, Query, Request

from workbench_server.models.notebook import NotebookDocument
from workbench_server.services.notebook import (
    NotANotebookError,
    NotebookTooLargeError,
    NotebookUnreadableError,
    read_notebook,
)
from workbench_server.services.workspace import PathOutsideWorkspaceError, Workspace

router = APIRouter(prefix="/api/notebook", tags=["notebook"])


def _ws(request: Request) -> Workspace:
    workspace: Workspace = request.app.state.workspace
    return workspace


@router.get("")
def read(request: Request, path: str = Query(min_length=1)) -> NotebookDocument:
    """One notebook, parsed and rendered. `path` is workspace-relative."""
    try:
        return read_notebook(_ws(request), path)
    except PathOutsideWorkspaceError as e:
        raise HTTPException(400, "path escapes workspace") from e
    except NotANotebookError as e:
        raise HTTPException(400, "not a notebook (.ipynb)") from e
    except FileNotFoundError as e:
        raise HTTPException(404, "notebook not found") from e
    except IsADirectoryError as e:
        raise HTTPException(400, "not a file") from e
    except NotebookTooLargeError as e:
        raise HTTPException(413, f"notebook is too large to read ({e})") from e
    except NotebookUnreadableError as e:
        raise HTTPException(422, f"not a readable notebook: {e}") from e
