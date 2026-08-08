"""Named-session REST API. Thin: the store owns the list and the disk.

Detachable working sessions (M5 item 15, PR2) — persistence only, no UI. The
store is global (queried *by* workspace, never re-rooted by a switch), so the
list endpoint takes the workspace as a query parameter rather than reading it
off the current root.
"""

from fastapi import APIRouter, HTTPException, Request, Response, status

from workbench_server.models.sessions import (
    CreateNamedSessionRequest,
    NamedSession,
    Objective,
    ObjectiveView,
    SessionsResponse,
    SetObjectiveRequest,
    UpdateNamedSessionRequest,
)
from workbench_server.models.validation import ValidationResult
from workbench_server.services.sessions import (
    SessionNotFoundError,
    SessionsFullError,
    SessionsStore,
    SessionTooLargeError,
    objective_view,
)
from workbench_server.services.validation import ValidationService

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _store(request: Request) -> SessionsStore:
    store: SessionsStore = request.app.state.sessions
    return store


def _results(request: Request) -> list[ValidationResult]:
    """The held validation results, read through the service's own replay
    snapshot — the same seam ``GET /api/validation`` exposes. The objective's
    status is a join over these, never a second copy of them (`services/sessions.py`
    ``objective_view``)."""
    service: ValidationService = request.app.state.validation
    return service.snapshot().results


@router.get("")
def list_sessions(request: Request, workspace: str | None = None) -> SessionsResponse:
    """The named sessions, most recently attached first. ``workspace`` filters to
    one project's sessions; omit it for every session across every project."""
    store = _store(request)
    return SessionsResponse(sessions=store.entries(workspace), problem=store.problem)


@router.post("")
def create_session(request: Request, body: CreateNamedSessionRequest) -> NamedSession:
    """Mint a session. 409 when the store is full, 413 when it is too large."""
    try:
        return _store(request).create(body)
    except SessionsFullError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    except SessionTooLargeError as err:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(err)) from err


@router.put("/{session_id}")
def update_session(
    request: Request, session_id: str, body: UpdateNamedSessionRequest
) -> NamedSession:
    """Replace a session whole. 404 when the id is unknown, 413 when too large."""
    try:
        return _store(request).update(session_id, body)
    except SessionNotFoundError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {session_id}") from err
    except SessionTooLargeError as err:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(err)) from err


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(request: Request, session_id: str) -> Response:
    """Forget a session. 404 when the id was never there."""
    try:
        _store(request).delete(session_id)
    except SessionNotFoundError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {session_id}") from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- objective (plan §3): a goal bound to a session, closed by evidence ------
#
# The status in every response is **derived** from the validation results, never
# stored or accepted from the client — the router joins the session with the
# results snapshot and returns one view.


@router.get("/{session_id}/objective")
def get_objective(request: Request, session_id: str) -> ObjectiveView:
    """The session's objective and its evidence-derived status. 404 when the id
    is unknown; a session with no objective answers ``status='open'``."""
    try:
        session = _store(request).get(session_id)
    except SessionNotFoundError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {session_id}") from err
    return objective_view(session, _results(request))


@router.put("/{session_id}/objective")
def set_objective(request: Request, session_id: str, body: SetObjectiveRequest) -> ObjectiveView:
    """Bind (or re-state) the session's goal. 404 when the id is unknown, 413 when
    the manifest would exceed its size ceiling."""
    objective = Objective(statement=body.statement, acceptance=body.acceptance)
    try:
        session = _store(request).set_objective(session_id, objective)
    except SessionNotFoundError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {session_id}") from err
    except SessionTooLargeError as err:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(err)) from err
    return objective_view(session, _results(request))


@router.delete("/{session_id}/objective")
def clear_objective(request: Request, session_id: str) -> ObjectiveView:
    """Drop the session's goal. 404 when the id is unknown. Returns the view so
    the client sees the objective gone and the status back to ``open``."""
    try:
        session = _store(request).clear_objective(session_id)
    except SessionNotFoundError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {session_id}") from err
    return objective_view(session, _results(request))
