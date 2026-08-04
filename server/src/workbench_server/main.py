"""Application factory and entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from workbench_server.config import Settings, load_settings
from workbench_server.logging import configure_logging
from workbench_server.routers import health

log = structlog.get_logger()

# Vite dev server origin; irrelevant once the built UI is served by this process.
_DEV_ORIGINS = ["http://localhost:5173"]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "workbench.starting",
            workspace=str(settings.resolved_workspace()),
            port=settings.port,
        )
        yield
        log.info("workbench.stopped")

    app = FastAPI(title="Workbench", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    return app


def run() -> None:
    """Console entrypoint: `uv run workbench-server`."""
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
