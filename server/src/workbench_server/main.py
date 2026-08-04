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
from workbench_server.routers import agents, events, files, health, office, terminal
from workbench_server.services.agent_sessions import SessionManager
from workbench_server.services.event_bus import EventBus
from workbench_server.services.office import OfficeService
from workbench_server.services.pty_manager import PtyManager
from workbench_server.services.sdk_factory import UiStateStore, sdk_client_factory
from workbench_server.services.session_index import SessionIndex
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
    ui_state_store = UiStateStore()
    session_index = SessionIndex(settings.resolved_projects_dir())
    session_manager = SessionManager(
        workspace.root,
        sdk_client_factory(ui_state_store, settings),
        settings.max_concurrent_sessions,
        session_index=session_index,
        # Session state changes ride the same bus as watcher events, so the UI
        # tracks sessions it has no agent socket open for.
        event_publisher=event_bus,
    )
    office_service = OfficeService(
        workspace,
        server_url=settings.onlyoffice_url,
        jwt_secret=settings.onlyoffice_jwt_secret,
        public_base_url=settings.resolved_public_base_url(),
        backup_originals=settings.office_backup,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("workbench.starting", workspace=str(workspace.root), port=settings.port)
        app.state.http = httpx.AsyncClient(timeout=30)
        watcher.start()
        yield
        await session_manager.close_all()
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
