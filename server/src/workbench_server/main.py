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
    activity,
    agents,
    conversations,
    events,
    files,
    health,
    layouts,
    office,
    office_host,
    provenance,
    shortcuts,
    terminal,
    usage,
    workspaces,
    worktrees,
)
from workbench_server.services.activity import ActivityService
from workbench_server.services.agent_sessions import ClientFactory, SessionManager
from workbench_server.services.conversations import ConversationBrowser
from workbench_server.services.documents import DocumentService
from workbench_server.services.event_bus import EventBus
from workbench_server.services.fake_agent import fake_client_factory
from workbench_server.services.layouts import LayoutsService
from workbench_server.services.office import OfficeService
from workbench_server.services.office_host import OfficeHostService, ShellChannel, build_backend
from workbench_server.services.office_host.shell_backend import ShellHostBackend
from workbench_server.services.provenance import ProvenanceService
from workbench_server.services.pty_manager import PtyManager
from workbench_server.services.sdk_factory import UiStateStore, sdk_client_factory
from workbench_server.services.session_index import SessionIndex
from workbench_server.services.shortcuts import ShortcutsService
from workbench_server.services.usage import UsageService
from workbench_server.services.watcher import Watcher
from workbench_server.services.workspace import Workspace
from workbench_server.services.workspaces import RecentsStore, WorkspaceService
from workbench_server.services.worktrees import WorktreeService

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
    # Borrowed git worktrees, one writer per checkout. The pool root lives under
    # the machine's app data dir and NOT under the workspace, so the tree never
    # walks a slot and the watcher never sees a checkout land in one.
    worktree_service = WorktreeService(
        workspace.root,
        event_bus,
        pool_root=settings.worktree_root,
        capacity=settings.worktree_pool_size,
        lease_seconds=settings.worktree_lease_seconds,
    )
    # The account's plan limits, as far as a live session's stream reports them.
    # In-memory by design: live state about an account, not workspace data.
    usage_service = UsageService(event_bus)
    # What the fleet is touching right now. The other end of provenance: that
    # one says who wrote a file after the fact, this one says what is happening
    # this second, across every session, whether or not a window has that
    # conversation open. In-memory and bounded, like usage.
    activity_service = ActivityService(workspace.root, event_bus)
    ui_state_store = UiStateStore()
    session_index = SessionIndex(settings.resolved_projects_dir())
    # The same storage, browsed whole instead of one folder at a time. Read-only
    # and lazy: nothing scans until a client asks, so this costs a bare object
    # on a workspace whose owner never opens the panel.
    conversation_browser = ConversationBrowser(settings.resolved_projects_dir(), workspace.root)
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
        # Rate-limit transitions and per-turn cost: an account-wide figure that
        # only ever arrives on a session's stream.
        usage_observer=usage_service,
        # Every announced tool call *and* its result, fanned out fleet-wide on
        # the shared bus — the frames themselves only reach the sockets that
        # opened each conversation.
        activity_observer=activity_service,
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
    # capabilities endpoint reports it and the UI degrades to OnlyOffice. The
    # channel is built whatever the policy, so the endpoint exists and a shell
    # that connects to a server with hosting off is *told* so by `capabilities`
    # rather than meeting a socket that refuses.
    host_channel = ShellChannel()
    host_backend = build_backend(settings.office_native, settings.office_fake, host_channel)
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
        channel=host_channel,
    )
    # Blank-document creation (M5 item 16). Stateless and workspace-agnostic: it
    # provides bundled templates and is handed the live `Workspace` per call, so
    # it copies no root of its own and owes no `set_workspace_root`.
    document_service = DocumentService()
    # The workspace is no longer fixed at launch (M5 item 5). Everything above
    # that copied `workspace.root` into a field of its own is listed here, and
    # this list is the *only* place a switch is coordinated — a service added
    # later that copies the root and is not named here keeps serving the project
    # the user left. Sync first (the jail, then the files that are simply re-read
    # from a different path), async last (restarts: the watcher's watch and the
    # pool's cross-process lock).
    workspace_service = WorkspaceService(
        workspace,
        event_bus,
        sync_rootables=[
            shortcuts_service,
            layouts_service,
            provenance_service,
            session_manager,
        ],
        async_rootables=[watcher, worktree_service],
        # Whether anybody *chose* this root, which the UI has to say rather than
        # imply: with no setting the server falls back to its launch directory,
        # and a first run must show what it is showing.
        explicit=settings.workspace_root is not None,
        recents=RecentsStore(settings.app_data_root),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("workbench.starting", workspace=str(workspace.root), port=settings.port)
        app.state.http = httpx.AsyncClient(timeout=30)
        # Before the watcher, so the folder the server opened in is already the
        # top of the recent list the first time anyone asks for it.
        workspace_service.start()
        watcher.start()
        shortcuts_service.start()
        provenance_service.start()
        # Binds to this loop, which is what lets it coalesce a burst of tool
        # calls into one frame instead of one frame per call.
        activity_service.start()
        office_host_service.start()
        # Reads the pool state and reconciles it with the disk. Never raises: a
        # machine with no git, or a workspace that is not a repository, reports
        # a `problem` on GET /api/worktrees and the rest of the app starts.
        await worktree_service.start()
        yield
        await session_manager.close_all()
        # Before the watcher: a hosted window outliving the server would be an
        # Office instance nobody owns, still wearing our panel's chrome.
        await office_host_service.shutdown()
        await host_channel.close()
        if isinstance(host_backend, ShellHostBackend):
            # Every instance has been reaped above; this only lets the COM
            # apartment thread go.
            await host_backend.aclose()
        await provenance_service.stop()
        await activity_service.stop()
        await shortcuts_service.stop()
        # Releases the pool's cross-process lock, so the next server on this
        # workspace can serve slots immediately instead of waiting for this
        # process to be reaped.
        await worktree_service.stop()
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
    app.state.conversations = conversation_browser
    app.state.office = office_service
    app.state.office_host = office_host_service
    app.state.office_host_channel = host_channel
    app.state.shortcuts = shortcuts_service
    app.state.provenance = provenance_service
    app.state.layouts = layouts_service
    app.state.worktrees = worktree_service
    app.state.usage = usage_service
    app.state.activity = activity_service
    app.state.workspaces = workspace_service
    app.state.documents = document_service
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
    app.include_router(conversations.router)
    app.include_router(office.router)
    app.include_router(office_host.router)
    app.include_router(office_host.ws_router)
    app.include_router(shortcuts.router)
    app.include_router(provenance.router)
    app.include_router(layouts.router)
    app.include_router(worktrees.router)
    app.include_router(usage.router)
    app.include_router(activity.router)
    app.include_router(workspaces.router)

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
