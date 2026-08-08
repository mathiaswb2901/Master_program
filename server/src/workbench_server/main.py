"""Application factory and entrypoint."""

import os
import secrets
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
from workbench_server.models.orchestrator import OrchestratorBudget
from workbench_server.routers import (
    activity,
    agents,
    auth,
    commands,
    conversations,
    events,
    files,
    health,
    layouts,
    office,
    office_host,
    orchestrator,
    provenance,
    search,
    sessions,
    setup,
    shortcuts,
    terminal,
    usage,
    validation,
    workspaces,
    worktrees,
)
from workbench_server.services.activity import ActivityService
from workbench_server.services.agent_sessions import ClientFactory, SessionManager
from workbench_server.services.app_data import app_data_dir
from workbench_server.services.commands import CommandRelay
from workbench_server.services.conversations import ConversationBrowser
from workbench_server.services.documents import DocumentService
from workbench_server.services.event_bus import EventBus
from workbench_server.services.fake_agent import fake_client_factory
from workbench_server.services.layouts import LayoutsService
from workbench_server.services.local_auth import LocalAuthMiddleware, is_local_origin
from workbench_server.services.office import OfficeService
from workbench_server.services.office_host import (
    OfficeHostService,
    ShellChannel,
    build_backend,
    build_bridge,
)
from workbench_server.services.office_host.shell_backend import ShellHostBackend
from workbench_server.services.orchestrator import OrchestratorService
from workbench_server.services.provenance import ProvenanceService
from workbench_server.services.pty_manager import PtyManager
from workbench_server.services.reconciliation import ReconciliationCheck
from workbench_server.services.sdk_factory import UiStateStore, sdk_client_factory
from workbench_server.services.search import SearchService
from workbench_server.services.session_index import SessionIndex
from workbench_server.services.sessions import SessionsStore
from workbench_server.services.setup import SetupService, detect_claude_login
from workbench_server.services.shortcuts import ShortcutsService
from workbench_server.services.usage import UsageService
from workbench_server.services.validation import ValidationService
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
    # Relays a registered window command from a shell/agent to a connected window
    # (M5 item 14): the event bus run backwards. Holds only the manifest the UI
    # publishes and the futures awaiting each invoke — no workspace state, so it
    # needs no re-rooting on a workspace switch.
    command_relay = CommandRelay(event_bus)
    watcher = Watcher(workspace.root, event_bus)
    # Rides the watcher's bus for the workspace file; watches the global one itself.
    shortcuts_service = ShortcutsService(workspace.root, event_bus)
    # Correlates agent tool calls with the watcher's file events; in-memory only.
    provenance_service = ProvenanceService(workspace.root, event_bus)
    # The next question after provenance's "who wrote this file": "and is it
    # correct". A registry of checks that assembles a ValidationResult and
    # derives its risk from the evidence. In-memory and bounded (LRU keyed by
    # validation_id, like provenance), and it publishes each result on the shared
    # bus for a reconnecting client — the usage/activity precedent. Ships the
    # frame only; the reconciliation gate that plugs into it is a later PR.
    validation_service = ValidationService(workspace.root, event_bus)
    # M6 PR2: the first *domain* check. It reads the addressed .xlsx cells directly
    # with openpyxl (deterministic, no Office), comparing code-computed numbers
    # against the workbook unit- and DST-aware. Additive registration — the frame
    # dispatches to it when a spec names its id ("reconciliation").
    validation_service.register(ReconciliationCheck())
    # Workspace-wide content search (M7 V7b). Holds the live `Workspace`, so a
    # switch re-roots it with everything else and it owes no `set_workspace_root`.
    # Reuses the file tree's ignore rules (IGNORED_DIRS + CACHEDIR.TAG), so search
    # and the tree agree about what exists. Also handed to a session as the
    # `workspace_search` agent tool (narrowed to WorkspaceSearcher in the factory).
    search_service = SearchService(workspace)
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
    activity_service = ActivityService(
        workspace.root,
        event_bus,
        # A Mission Control worker's cwd is a pooled slot, which is outside the
        # workspace by design — so without this every row of a working worker
        # would read "(outside the workspace)". Naming, not opening: the feed's
        # clickable target is still workspace-only (services/activity.py).
        extra_roots=[("", worktree_service.root)],
    )
    ui_state_store = UiStateStore()
    # Native Office hosting, constructed before the session manager because a
    # session reads the live docked document through it (narrowed to
    # OfficeDocumentReader in the SDK factory). The backend is None on any
    # machine that cannot host (and whenever the policy says not to), which is
    # not an error: the capabilities endpoint reports it and the UI degrades to
    # OnlyOffice. The channel is built whatever the policy, so the endpoint
    # exists and a shell that connects to a server with hosting off is *told* so
    # by `capabilities` rather than meeting a socket that refuses. The read
    # bridge is the fake in fake mode and None otherwise for now — the real COM
    # reader lands in a later PR; hosting the window is shipped, reading its live
    # document is not.
    host_channel = ShellChannel()
    host_backend = build_backend(settings.office_native, settings.office_fake, host_channel)
    host_bridge = build_bridge(settings.office_native, settings.office_fake, host_backend)
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
        bridge=host_bridge,
        mode=settings.office_native,
        fake=settings.office_fake,
        channel=host_channel,
    )
    # Mission Control's second half. Built before the session manager because
    # the client factory needs it (an orchestrator session's toolset is created
    # with the client), and handed the manager through `bind` a few lines below.
    orchestrator_service = OrchestratorService(
        event_bus,
        usage_service,
        OrchestratorBudget(
            max_workers=settings.orchestrator_max_workers,
            max_worker_turns=settings.orchestrator_worker_turns,
            max_worker_cost_usd=settings.orchestrator_worker_cost_usd,
            max_fleet_turns=settings.orchestrator_fleet_turns,
            max_fleet_cost_usd=settings.orchestrator_fleet_cost_usd,
        ),
        worktree_service,
    )
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
        client_factory = fake_client_factory(orchestrator_service)
    else:
        client_factory = sdk_client_factory(
            ui_state_store,
            office_host_service,
            command_relay,
            # The reconciliation gate a session runs over a workbook it wrote. The
            # ValidationService, narrowed to ReconciliationRunner in the SDK factory;
            # the reconciliation check is registered on it a few lines above.
            validation_service,
            # Workspace content search, narrowed to WorkspaceSearcher, so a session
            # can grep its own folder through the `workspace_search` tool.
            search_service,
            settings,
            orchestrator_service,
            # What a worker has spent, read from the *one* accumulator: the same
            # figure the budget is enforced against and the board renders.
            lambda worker_id: (
                entry.cost_usd
                if (entry := usage_service.session_entry(worker_id)) is not None
                else 0.0
            ),
        )
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
    # The other half of the construction cycle above: the factory closes over
    # the orchestrator, and the orchestrator needs the manager the factory built.
    orchestrator_service.bind(session_manager)
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
    # Blank-document creation (M5 item 16). Stateless and workspace-agnostic: it
    # provides bundled templates and is handed the live `Workspace` per call, so
    # it copies no root of its own and owes no `set_workspace_root`.
    document_service = DocumentService()
    # Detachable working sessions (M5 item 15, PR2). A named session ties a
    # workspace + an arrangement + its live agents and leases into one thing a
    # user can leave and return to. GLOBAL on purpose: it lives once under the
    # machine's app data dir and is queried *by* workspace, so it is deliberately
    # NOT in the rootables below and owns no `set_workspace_root` — switching the
    # current project must not change which sessions exist, only which ones a
    # window asks to see. One project can host more than one named session.
    sessions_store = SessionsStore(settings.app_data_root)
    # First-run / Setup (M7 §2). Reads the live `Workspace` (so a switch re-roots
    # first-run detection with everything else — no `set_workspace_root` owed) and
    # echoes the office authority rather than computing a second one. The Claude
    # check is *presence only*: under the fake agent there is no real Claude, so
    # the probe reports signed-out deterministically for CI; otherwise it reads
    # the machine's existing credentials and never triggers a login.
    setup_service = SetupService(
        workspace,
        office_host_service,
        office_service,
        signed_in_probe=(
            (lambda: False)
            if settings.fake_agent
            else (lambda: detect_claude_login(Path.home(), os.environ))
        ),
    )
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
            # Holds results keyed by a workspace-relative path; a switch must
            # forget them, or a verdict about a file in the project just left
            # would attach to a same-named file in the one opened.
            validation_service,
            session_manager,
            # Half B of the session browser (M5 item 12): after a switch the
            # browser must judge each folder against the workspace the user just
            # opened, or it goes on refusing the conversation they switched to.
            conversation_browser,
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
        # Before `close_all`: stopping a crew *releases its worktree leases*,
        # and once the sessions are gone the roster has nothing left to release
        # them with — the slots would sit held until their leases expired.
        await orchestrator_service.stop_all()
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
    # Per-launch auth token (M5 item 8 / OSS-bar item 1). Minted fresh unless
    # one was configured out of band (an attaching desktop shell). Stashed on
    # app.state so the token-handout router can read it; the middleware is
    # handed its own copy at construction. Enforcement is ON by default now
    # (PR4); `WORKBENCH_ENFORCE_AUTH=0` forces the middleware back to a
    # pass-through for debugging.
    token = settings.auth_token or secrets.token_urlsafe(32)
    app.state.auth_token = token
    app.state.settings = settings
    app.state.pty_manager = pty_manager
    app.state.workspace = workspace
    app.state.event_bus = event_bus
    app.state.commands = command_relay
    app.state.ui_state_store = ui_state_store
    app.state.session_manager = session_manager
    app.state.session_index = session_index
    app.state.conversations = conversation_browser
    app.state.office = office_service
    app.state.office_host = office_host_service
    app.state.office_host_channel = host_channel
    app.state.shortcuts = shortcuts_service
    app.state.provenance = provenance_service
    app.state.validation = validation_service
    app.state.search = search_service
    app.state.layouts = layouts_service
    app.state.worktrees = worktree_service
    app.state.usage = usage_service
    app.state.activity = activity_service
    app.state.orchestrator = orchestrator_service
    app.state.workspaces = workspace_service
    app.state.documents = document_service
    app.state.sessions = sessions_store
    app.state.setup = setup_service
    # Order matters, and Starlette inverts it: add_middleware inserts at the head
    # of the list, so the LAST middleware added is the OUTERMOST layer. We add
    # LocalAuth *first* and CORS *second* so CORS ends up outer — a request the
    # auth layer rejects still passes back out through CORS, which attaches the
    # Access-Control-Allow-Origin header to the 403. Reversed, LocalAuth's 403
    # would never reach CORS and a browser XHR with a missing/wrong token would
    # surface as an opaque CORS failure instead of a readable 403 body now that
    # enforce_auth is on by default.
    #
    # Local-API security hardening (M5 item 8): require the per-launch token on
    # REST + WS and gate the WS handshake on Origin. Active by default now
    # (settings.enforce_auth is True); a pass-through only when forced off with
    # WORKBENCH_ENFORCE_AUTH=0 — see services/local_auth.py.
    app.add_middleware(
        LocalAuthMiddleware,
        token=token,
        enforce=settings.enforce_auth,
        is_local_origin=is_local_origin,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(terminal.router)
    app.include_router(files.router)
    app.include_router(events.router)
    app.include_router(commands.router)
    app.include_router(agents.router)
    app.include_router(agents.ws_router)
    app.include_router(conversations.router)
    app.include_router(office.router)
    app.include_router(office_host.router)
    app.include_router(office_host.ws_router)
    app.include_router(shortcuts.router)
    app.include_router(provenance.router)
    app.include_router(validation.router)
    app.include_router(search.router)
    app.include_router(layouts.router)
    app.include_router(worktrees.router)
    app.include_router(usage.router)
    app.include_router(activity.router)
    app.include_router(orchestrator.router)
    app.include_router(workspaces.router)
    app.include_router(sessions.router)
    app.include_router(setup.router)

    # Built frontend, when present (repo layout: <root>/ui/dist next to server/)
    ui_dist = Path(__file__).resolve().parents[3] / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

    return app


def runtime_token_path(settings: Settings) -> Path:
    """Where this instance drops its per-launch token for an attaching shell.

    Keyed by port so two servers on the same machine (different ports) do not
    clobber or delete each other's file: each writes ``auth-token-<port>`` and
    an attaching shell reads the one matching the port it is dialling. Without
    the discriminator, whichever process exited first would unlink the shared
    file out from under a still-running sibling.
    """
    root = settings.app_data_root or app_data_dir()
    return root / "runtime" / f"auth-token-{settings.port}"


def run() -> None:
    """Console entrypoint: `uv run workbench-server`."""
    import uvicorn

    settings = load_settings()
    app = create_app(settings)
    # Drop the per-launch token where a *later*-attaching desktop shell — one
    # that did not spawn this backend and so never saw the token in an env var —
    # can read it. Written owner-only, and only from the real entrypoint: the
    # in-process test app is built through create_app, which touches no disk, so
    # a test run never writes (or races on) this file. Removed on the way out so
    # a token never outlives the process that owns it.
    token_path = runtime_token_path(settings)
    _write_runtime_token(token_path, app.state.auth_token)
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    finally:
        token_path.unlink(missing_ok=True)


def _write_runtime_token(path: Path, token: str) -> None:
    """Write the launch token where only this user can read it back.

    The directory already sits under a per-user app-data root
    (``%LOCALAPPDATA%\\Workbench`` on Windows), so the token is not exposed to
    other accounts by its location. ``chmod(0o600)`` narrows it further where
    the OS honours POSIX bits; on NTFS it only clears the read-only flag, which
    is why the per-user directory — not this call — is the real guarantee.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        # Best effort: the file is still under a per-user directory. Log and
        # carry on rather than refuse to start over a permissions tightening.
        log.warning("auth.token_file_chmod_failed", path=str(path), error=str(exc))
