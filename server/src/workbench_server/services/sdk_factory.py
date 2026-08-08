"""The real ClaudeSDKClient factory + the context-bridge MCP server.

Isolated here so nothing else imports the SDK — sessions stay testable with fakes.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from workbench_server.config import Settings
from workbench_server.models.agents import SessionKind, UiState
from workbench_server.services.agent_sessions import SdkClient, SessionBridge
from workbench_server.services.agent_tools import (
    GET_WORKSPACE_STATE,
    LIST_WORKERS,
    MCP_SERVER_NAME,
    OFFICE_READ,
    OFFICE_RECONCILE,
    OFFICE_WRITE,
    PRESENT_PLAN,
    READ_WORKER,
    RUN_COMMAND,
    SEND_TO_WORKER,
    SPAWN_WORKER,
    STOP_WORKER,
    WORKSPACE_SEARCH,
    CommandInvoker,
    OfficeDocumentAccess,
    OrchestratorHandle,
    ReconciliationRunner,
    WorkspaceSearcher,
    allowed_tool_names,
    handle_list_workers,
    handle_office_read,
    handle_office_reconcile,
    handle_office_write,
    handle_present_plan,
    handle_read_worker,
    handle_run_command,
    handle_send_to_worker,
    handle_spawn_worker,
    handle_stop_worker,
    handle_workspace_search,
    workspace_state_result,
)
from workbench_server.services.permission_broker import build_pre_tool_use_hook
from workbench_server.services.skills_bundle import PLUGIN_NAME, bundled_plugin_path

# Tools the agent may use inside its folder without asking. Deliberately
# file-only, and deliberately the *whole* reason editing does not prompt: a
# whole-tool entry here auto-approves the tool before can_use_tool is consulted,
# which is exactly what we want for the four file tools provenance correlates.
#
# Everything else — shell, web — is absent, so it reaches a human. For the shell
# launchers that guarantee is enforced twice: absence from this list, *and* the
# PreToolUse broker in ``services/permission_broker.py``, which binds even when a
# permission mode or a workspace settings file would otherwise auto-approve.
_AUTO_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep"]

# The two bundled skills something already tells the agent to reach for on its
# own: the system-prompt append names plan-visual, and remember's own description
# has it read workspace memory at the start of unfamiliar work. Without these
# rules that guidance opens every plan — and every session — with a permission
# dialog, and a user who declines gets exactly the output the guidance existed to
# prevent. These are narrow ``Skill(name)`` specifiers, never a bare ``Skill``:
# only a whole-tool entry shadows can_use_tool, so every other skill — the user's
# own included, and our own workbench-dev — still reaches the UI prompt.
_AUTO_ALLOWED_SKILLS = [f"Skill({PLUGIN_NAME}:plan-visual)", f"Skill({PLUGIN_NAME}:remember)"]


class UiStateStore:
    """Latest UI state pushed by the frontend; read by agents via MCP."""

    def __init__(self) -> None:
        self.state = UiState()


def build_context_bridge(
    store: UiStateStore,
    bridge: SessionBridge,
    reader: OfficeDocumentAccess,
    commands: CommandInvoker,
    reconciler: ReconciliationRunner,
    searcher: WorkspaceSearcher,
    kind: SessionKind = "chat",
    orchestrator: OrchestratorHandle | None = None,
    session_cost: Callable[[str], float] | None = None,
) -> Any:
    """In-process MCP server exposing the workbench tools to one session.

    Names, descriptions, input schemas and bodies all come from the tool
    registry (``services/agent_tools.py``); this function is only the SDK
    wiring, so adding a tool never means editing an allow-list here as well.

    An ``orchestrator`` session gets five more tools. They are added here rather
    than gated inside each body because a tool the model can *see* is a tool it
    will try, and five "you are not an orchestrator" errors are five round trips
    plus five schemas paid on every request of every chat session.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        GET_WORKSPACE_STATE.name,
        GET_WORKSPACE_STATE.description,
        GET_WORKSPACE_STATE.input_schema,
    )
    async def get_workspace_state(args: dict[str, Any]) -> dict[str, Any]:
        return workspace_state_result(store.state)

    @tool(PRESENT_PLAN.name, PRESENT_PLAN.description, PRESENT_PLAN.input_schema)
    async def present_plan(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_present_plan(bridge, args)

    @tool(OFFICE_READ.name, OFFICE_READ.description, OFFICE_READ.input_schema)
    async def office_read(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_office_read(reader, args)

    @tool(OFFICE_WRITE.name, OFFICE_WRITE.description, OFFICE_WRITE.input_schema)
    async def office_write(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_office_write(reader, args)

    @tool(OFFICE_RECONCILE.name, OFFICE_RECONCILE.description, OFFICE_RECONCILE.input_schema)
    async def office_reconcile(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_office_reconcile(reconciler, args)

    @tool(RUN_COMMAND.name, RUN_COMMAND.description, RUN_COMMAND.input_schema)
    async def run_command(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_run_command(commands, args)

    @tool(WORKSPACE_SEARCH.name, WORKSPACE_SEARCH.description, WORKSPACE_SEARCH.input_schema)
    async def workspace_search(args: dict[str, Any]) -> dict[str, Any]:
        return handle_workspace_search(searcher, args)

    tools: list[Any] = [
        get_workspace_state,
        present_plan,
        office_read,
        office_write,
        office_reconcile,
        run_command,
        workspace_search,
    ]
    if kind == "orchestrator" and orchestrator is not None:
        cost = session_cost or (lambda _worker_id: 0.0)

        @tool(SPAWN_WORKER.name, SPAWN_WORKER.description, SPAWN_WORKER.input_schema)
        async def spawn_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await handle_spawn_worker(orchestrator, bridge.session_id, args)

        @tool(LIST_WORKERS.name, LIST_WORKERS.description, LIST_WORKERS.input_schema)
        async def list_workers(args: dict[str, Any]) -> dict[str, Any]:
            return handle_list_workers(orchestrator, bridge.session_id, cost)

        @tool(READ_WORKER.name, READ_WORKER.description, READ_WORKER.input_schema)
        async def read_worker(args: dict[str, Any]) -> dict[str, Any]:
            return handle_read_worker(orchestrator, bridge.session_id, args)

        @tool(SEND_TO_WORKER.name, SEND_TO_WORKER.description, SEND_TO_WORKER.input_schema)
        async def send_to_worker(args: dict[str, Any]) -> dict[str, Any]:
            return handle_send_to_worker(orchestrator, bridge.session_id, args)

        @tool(STOP_WORKER.name, STOP_WORKER.description, STOP_WORKER.input_schema)
        async def stop_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await handle_stop_worker(orchestrator, bridge.session_id, args)

        tools += [spawn_worker, list_workers, read_worker, send_to_worker, stop_worker]

    return create_sdk_mcp_server(name=MCP_SERVER_NAME, version="1.0.0", tools=tools)


def build_agent_options(
    store: UiStateStore,
    settings: Settings,
    folder: Path,
    resume_session_id: str | None,
    bridge: SessionBridge,
    reader: OfficeDocumentAccess,
    commands: CommandInvoker,
    reconciler: ReconciliationRunner,
    searcher: WorkspaceSearcher,
    kind: SessionKind = "chat",
    orchestrator: OrchestratorHandle | None = None,
    session_cost: Callable[[str], float] | None = None,
) -> Any:
    """The single construction point for ``ClaudeAgentOptions``.

    Split out of the factory closure so the whole configuration — permissions,
    the context bridge, the bundled plugin, which filesystem settings a session
    inherits — is assertable in tests without connecting a client.

    Skills are *not* enabled through ``options.skills``: ``"all"`` appends a bare
    ``Skill`` entry to ``allowed_tools``, which shadows ``can_use_tool`` and
    silently auto-allows every discovered skill, and a list would double as a
    context filter that hides the user's own project skills. The plugin is
    passed on its own instead, so bundled skills are simply *available* under
    the ``workbench:`` prefix; only the two named in ``_AUTO_ALLOWED_SKILLS``
    carry a narrow allow rule, and every other invocation still reaches the UI's
    permission prompt like any other non-file tool.
    """
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        HookMatcher,
        PermissionResultAllow,
        PermissionResultDeny,
        SdkPluginConfig,
        SettingSource,
    )

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        if await bridge.ask_permission(tool_name, tool_input):
            return PermissionResultAllow()
        return PermissionResultDeny(message="User declined in the workbench UI")

    plugins: list[SdkPluginConfig] = []
    if settings.bundled_skills:
        plugin_path = bundled_plugin_path()
        if plugin_path is not None:
            plugins.append(SdkPluginConfig(type="local", path=str(plugin_path)))

    # ``None`` is the SDK's implicit default: load every filesystem settings
    # source, including the user's global ~/.claude — its hooks and permission
    # rules, not only its skills. ``["project", "local"]`` keeps everything the
    # *workspace* configures: CLAUDE.md and .claude/settings.json, plus the
    # machine-local .claude/settings.local.json where Claude Code records
    # "always allow" rules, local hooks and local MCP servers — so a folder
    # behaves the same in a Workbench session as it does in plain Claude Code.
    # Only the global machine scope is dropped.
    setting_sources: list[SettingSource] | None = (
        None if settings.inherit_user_settings else ["project", "local"]
    )

    return ClaudeAgentOptions(
        cwd=str(folder),
        resume=resume_session_id,
        allowed_tools=[
            # Never widened for a worker, and that is the whole permission story
            # of Mission Control: no shell launcher is here, so a worker's shell
            # request blocks and escalates to the board for a human to answer.
            # An orchestrator adds its own five tools below and nothing else —
            # it gains no way to run anything.
            *_AUTO_ALLOWED,
            *(_AUTO_ALLOWED_SKILLS if plugins else []),
            *allowed_tool_names(kind),
        ],
        # NOT ``acceptEdits``. That mode auto-approves a Bash call whose base
        # command is one of ``mkdir touch rm rmdir mv cp sed`` inside cwd, and an
        # auto-approved call never reaches ``can_use_tool`` — so ``rm -rf`` and
        # ``sed -i`` in the workspace used to run with no permission card, no
        # provenance claim and no activity row. Measured, not inferred; the
        # regression test is ``test_permission_broker.py``.
        #
        # Dropping it costs nothing: the four file tools are auto-approved by
        # ``allowed_tools`` above, which shadows ``can_use_tool`` on its own, so
        # ordinary Write/Edit still does not prompt (also measured). ``default``
        # is therefore the *same* editing ergonomics minus the shell fast path.
        permission_mode="default",
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        # The broker that binds regardless of mode. It is not redundant with
        # ``can_use_tool``: a hook is dispatched for every tool call, while the
        # callback is only reached for calls the CLI resolves to "ask" — so a
        # ``permissions.allow`` rule in a folder the user did not write, or a
        # mode a subagent inherited, cannot walk around this one.
        # ``cast`` because the broker is deliberately SDK-free (this module is
        # the only one that imports ``claude_agent_sdk`` at all), so it types its
        # hook input as a plain dict rather than the SDK's TypedDict union. The
        # keys it reads — ``tool_name``, ``tool_input``, ``agent_id`` — are the
        # ones ``PreToolUseHookInput`` declares.
        hooks={
            "PreToolUse": [
                HookMatcher(
                    hooks=[
                        cast(
                            "Any",
                            build_pre_tool_use_hook(bridge.ask_permission, bridge.session_id),
                        )
                    ]
                )
            ]
        },
        mcp_servers={
            MCP_SERVER_NAME: build_context_bridge(
                store,
                bridge,
                reader,
                commands,
                reconciler,
                searcher,
                kind,
                orchestrator,
                session_cost,
            )
        },
        plugins=plugins,
        setting_sources=setting_sources,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "You are running inside Workbench. Before editing files, call "
                "get_workspace_state and avoid editing files listed as dirty "
                "(unsaved user changes). When you propose multi-step work or "
                "alternatives, call present_plan instead of writing the plan "
                "as prose — the user answers it as an interactive card. Use the "
                "bundled workbench:plan-visual skill when authoring that card."
            ),
        },
    )


def sdk_client_factory(
    store: UiStateStore,
    reader: OfficeDocumentAccess,
    commands: CommandInvoker,
    reconciler: ReconciliationRunner,
    searcher: WorkspaceSearcher,
    settings: Settings | None = None,
    orchestrator: OrchestratorHandle | None = None,
    session_cost: Callable[[str], float] | None = None,
) -> Any:
    """Returns a ClientFactory closure for SessionManager.

    ``reader`` is the office-host service, narrowed to :class:`OfficeDocumentAccess`
    so a session can read *and* edit the live docked document without this module
    importing the service — the same one-way dependency the rest of the tools keep.
    ``commands`` is the command relay, narrowed to :class:`CommandInvoker`, so a
    session can invoke a registered window command the same way. ``reconciler`` is
    the validation service, narrowed to :class:`ReconciliationRunner`, so a session
    can run the reconciliation gate over a workbook it just wrote.
    """
    resolved = settings or Settings()

    def factory(
        folder: Path,
        resume_session_id: str | None,
        bridge: SessionBridge,
        kind: SessionKind = "chat",
    ) -> SdkClient:
        from claude_agent_sdk import ClaudeSDKClient

        options = build_agent_options(
            store,
            resolved,
            folder,
            resume_session_id,
            bridge,
            reader,
            commands,
            reconciler,
            searcher,
            kind,
            orchestrator,
            session_cost,
        )
        client: SdkClient = ClaudeSDKClient(options=options)
        return client

    return factory
