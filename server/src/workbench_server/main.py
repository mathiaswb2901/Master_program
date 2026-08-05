"""Application factory and entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from workbench_server.config import Settings, load_settings
from workbench_server.logging import configure_logging
from workbench_server.routers import (
    agents,
    events,
    files,
    health,
    layouts,
    office,
    office_host,
    provenance,
    shortcuts,
    terminal,
)
from workbench_server.services.agent_sessions import ClientFactory, SessionManager
from workbench_server.services.event_bus import EventBus
from workbench_server.services.fake_agent import fake_client_factory
from workbench_server.services.layouts import LayoutsService
from workbench_server.services.office import OfficeService
from workbench_server.services.office_host import OfficeHostService, build_backend
from workbench_server.services.provenance import ProvenanceService
from workbench_server.services.pty_manager import PtyManager
from workbench_server.services.sdk_factory import UiStateStore, sdk_client_factory
from workbench_server.services.session_index import SessionIndex
from workbench_server.services.shortcuts import ShortcutsService
from workbench_server.services.watcher import Watcher
from workbench_server.services.workspace import Workspace

log = structlog.get_logger()

# Vite dev server origin; irrelevant once the built UI is served by this process.
_DEV_ORIGINS = ["http://localhost:5173"]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings)

    pty_manager = PtyManager()
    workspace = Workspace(settings.resolved_workspace())
    event_bus = EventBus()
    watcher = Watcher(workspace.root, event_bus)
    # Rides the watcher's bus for the workspace file; watches the global one itself.
    shortcuts_service = ShortcutsService(workspace.root, event_bus)
    # Correlates agent tool calls with the watcher's file events; in-memory only.
    provenance_service = ProvenanceService(workspace.root, event_bus)
    # One JSON document per workspace, so different projects keep different
    # arrangements. Stateless: read and written on demand, nothing to start.
    layouts_service = LayoutsService(workspace.root)
    ui_state_store = UiStateStore()
    session_index = SessionIndex(settings.resolved_projects_dir())
    # Fake mode replaces the SDK client and nothing else: same SessionManager,
    # same bridge, same WebSockets — so what a test drives is the real backend.
    client_factory: ClientFactory
    if settings.fake_agent:
        log.warning(
            "agent.fake_mode_enabled",
            detail="WORKBENCH_FAKE_AGENT is set: replies are scripted, no agent is running",
        )
        client_factory = fake_client_factory()
    else:
        client_factory = sdk_client_factory(ui_state_store, settings)
    session_manager = SessionManager(
        workspace.root,
        client_factory,
        settings.max_concurrent_sessions,
        session_index=session_index,
        # Session state changes ride the same bus as watcher events, so the UI
        # tracks sessions it has no agent socket open for.
        event_publisher=event_bus,
        # Every announced tool call is offered to the correlator, which keeps
        # the ones that write a file inside the workspace.
        tool_observer=provenance_service,
    )
    office_service = OfficeService(
        workspace,
        server_url=settings.onlyoffice_url,
        jwt_secret=settings.onlyoffice_jwt_secret,
        public_base_url=settings.resolved_public_base_url(),
        backup_originals=settings.office_backup,
        # A Document Server save is the user typing, flushed — the correlator
        # has to hear about it or an open agent claim would take the credit.
        provenance=provenance_service,
    )
    # Native Office hosting. The backend is None on any machine that cannot host
    # (and whenever the policy says not to), which is not an error: the
    # capabilities endpoint reports it and the UI degrades to OnlyOffice.
    host_backend = build_backend(settings.office_native, settings.office_fake)
    if settings.office_fake and host_backend is not None:
        log.warning(
            "office_host.fake_mode_enabled",
            detail="WORKBENCH_OFFICE_FAKE is set: hosts are simulated, no document is really open",
        )
    office_host_service = OfficeHostService(
        workspace,
        # Host state changes ride the same bus as watcher and session events,
        # so a window that never issued the open request still tracks them.
        event_bus,
        host_backend,
        mode=settings.office_native,
        fake=settings.office_fake,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("workbench.starting", workspace=str(workspace.root), port=settings.port)
        app.state.http = httpx.AsyncClient(timeout=30)
        watcher.start()
        shortcuts_service.start()
        provenance_service.start()
        office_host_service.start()
        yield
        await session_manager.close_all()
        # Before the watcher: a hosted window outliving the server would be an
        # Office instance nobody owns, still wearing our panel's chrome.
        await office_host_service.shutdown()
        await provenance_service.stop()
        await shortcuts_service.stop()
        await watcher.stop()
        pty_manager.shutdown()
        await app.state.http.aclose()
        log.info("workbench.stopped")

    app = FastAPI(title="Workbench", lifespan=lifespan)
    app.state.settings = settings
    app.state.pty_manager = pty_manager
    app.state.workspace = workspace
    app.state.event_bus = event_bus
    app.state.ui_state_store = ui_state_store
    app.state.session_manager = session_manager
    app.state.session_index = session_index
    app.state.office = office_service
    app.state.office_host = office_host_service
    app.state.shortcuts = shortcuts_service
    app.state.provenance = provenance_service
    app.state.layouts = layouts_service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(terminal.router)
    app.include_router(files.router)
    app.include_router(events.router)
    app.include_router(agents.router)
    app.include_router(agents.ws_router)
    app.include_router(office.router)
    app.include_router(office_host.router)
    app.include_router(shortcuts.router)
    app.include_router(provenance.router)
    app.include_router(layouts.router)

    # Built frontend, when present (repo layout: <root>/ui/dist next to server/)
    ui_dist = Path(__file__).resolve().parents[3] / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

    return app


def run() -> None:
    """Console entrypoint: `uv run workbench-server`."""
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
