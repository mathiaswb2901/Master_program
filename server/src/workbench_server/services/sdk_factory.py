"""The real ClaudeSDKClient factory + the context-bridge MCP server.

Isolated here so nothing else imports the SDK — sessions stay testable with fakes.
"""

from pathlib import Path
from typing import Any

from workbench_server.config import Settings
from workbench_server.models.agents import UiState
from workbench_server.services.agent_sessions import SdkClient, SessionBridge
from workbench_server.services.agent_tools import (
    GET_WORKSPACE_STATE,
    MCP_SERVER_NAME,
    PRESENT_PLAN,
    allowed_tool_names,
    handle_present_plan,
    workspace_state_result,
)
from workbench_server.services.skills_bundle import PLUGIN_NAME, bundled_plugin_path

# Tools the agent may use inside its folder without asking. Deliberately file-only:
# anything else (Bash, web access, ...) falls through to can_use_tool and becomes
# a permission prompt in the UI. Auto-allowing them would shadow the callback.
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


def build_context_bridge(store: UiStateStore, bridge: SessionBridge) -> Any:
    """In-process MCP server exposing the workbench tools to one session.

    Names, descriptions, input schemas and bodies all come from the tool
    registry (``services/agent_tools.py``); this function is only the SDK
    wiring, so adding a tool never means editing an allow-list here as well.
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

    return create_sdk_mcp_server(
        name=MCP_SERVER_NAME, version="1.0.0", tools=[get_workspace_state, present_plan]
    )


def build_agent_options(
    store: UiStateStore,
    settings: Settings,
    folder: Path,
    resume_session_id: str | None,
    bridge: SessionBridge,
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
            *_AUTO_ALLOWED,
            *(_AUTO_ALLOWED_SKILLS if plugins else []),
            *allowed_tool_names(),
        ],
        permission_mode="acceptEdits",
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        mcp_servers={MCP_SERVER_NAME: build_context_bridge(store, bridge)},
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


def sdk_client_factory(store: UiStateStore, settings: Settings | None = None) -> Any:
    """Returns a ClientFactory closure for SessionManager."""
    resolved = settings or Settings()

    def factory(folder: Path, resume_session_id: str | None, bridge: SessionBridge) -> SdkClient:
        from claude_agent_sdk import ClaudeSDKClient

        options = build_agent_options(store, resolved, folder, resume_session_id, bridge)
        client: SdkClient = ClaudeSDKClient(options=options)
        return client

    return factory
