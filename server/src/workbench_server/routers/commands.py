"""Command-relay REST API. Thin: the service owns the manifest and the relay.

Every route here is under ``/api/``, so the local-auth middleware gates it —
a request without the per-launch token is refused before it arrives
(``services/local_auth.py``). That is item 14's second safety constraint; the
first — only *registered* commands are invocable — lives one call down, in
``CommandRelay.is_registered`` and the 404 below.
"""

from fastapi import APIRouter, HTTPException, Request

from workbench_server.models.commands import (
    CommandInvokeRequest,
    CommandInvokeResult,
    CommandManifest,
    CommandResultRequest,
)
from workbench_server.models.files import OkResponse
from workbench_server.services.commands import CommandRelay

router = APIRouter(prefix="/api/commands", tags=["commands"])


def _relay(request: Request) -> CommandRelay:
    service: CommandRelay = request.app.state.commands
    return service


@router.get("")
async def list_commands(request: Request) -> CommandManifest:
    """The invocable-command manifest, for a CLI/agent to discover ids from.

    Empty until a window has connected and published — the honest first state,
    not an error.
    """
    return _relay(request).manifest()


@router.put("/manifest")
async def publish_manifest(request: Request, body: CommandManifest) -> OkResponse:
    """The window publishes what it will run on request. Called on every
    (re)connect, so the backend always validates against the live window."""
    _relay(request).set_manifest(body)
    return OkResponse()


@router.post("/invoke")
async def invoke_command(request: Request, body: CommandInvokeRequest) -> CommandInvokeResult:
    """Run a registered command in the connected window and report the outcome.

    An id absent from the published manifest is a 404 — an honest refusal, never
    a silent pass to a window that would not know the command either.
    """
    relay = _relay(request)
    if not relay.is_registered(body.command_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"{body.command_id!r} is not a registered command. "
                "GET /api/commands lists the ones that are."
            ),
        )
    return await relay.invoke(body.command_id, body.params)


@router.post("/result")
async def report_result(request: Request, body: CommandResultRequest) -> OkResponse:
    """The window reports how one invocation went, completing the awaiting
    invoke. A result for an invocation that already timed out is a no-op."""
    _relay(request).resolve(body.invocation_id, ok=body.ok, detail=body.detail)
    return OkResponse()
