"""Shortcuts REST API. Thin: the service owns loading, merging and watching."""

from fastapi import APIRouter, Request

from workbench_server.models.shortcuts import ShortcutsState
from workbench_server.services.shortcuts import ShortcutsService

router = APIRouter(prefix="/api/shortcuts", tags=["shortcuts"])


@router.get("")
def shortcuts(request: Request) -> ShortcutsState:
    service: ShortcutsService = request.app.state.shortcuts
    return service.state()
