"""The real ClaudeSDKClient factory + the context-bridge MCP server.

Isolated here so nothing else imports the SDK — sessions stay testable with fakes.
"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from workbench_server.config import Settings
from workbench_server.models.agents import UiState
from workbench_server.models.plans import PlanArtifact, plan_input_schema
from workbench_server.services.agent_sessions import (
    PlanAlreadyPendingError,
    SdkClient,
    SessionBridge,
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

_PRESENT_PLAN_DESCRIPTION = (
    "Show the user an interactive plan card in Workbench and wait for their "
    "decision. Use this instead of writing a plan as chat prose whenever you "
    "propose multi-step work or ask the user to choose between alternatives. "
    "Nodes render natively: option_group (the user picks one option), step_list "
    "(ordered steps, file_refs open real editor tabs), question, markdown. "
    "Returns JSON {plan_id, verdict, choices, annotations, comment}. verdict "
    "'approve' means proceed with the chosen options; 'revise' means rework the "
    "plan using their comments and present it again; 'reject' means drop this "
    "approach; 'no_decision' means the user never answered (timeout or "
    "interrupt) — stop and ask in chat, never treat it as approval."
)


class UiStateStore:
    """Latest UI state pushed by the frontend; read by agents via MCP."""

    def __init__(self) -> None:
        self.state = UiState()


async def handle_present_plan(bridge: SessionBridge, args: dict[str, Any]) -> dict[str, Any]:
    """The present_plan tool body, free of SDK imports so it is directly testable.

    ``plan_id`` is dropped before validation, not merely omitted from the tool's
    input schema: the schema is advisory, and ``PlanArtifact``'s default factory
    would keep any id the model sent. Since the tool result the agent reads
    *contains* the id, an agent that echoes it back when re-presenting a revised
    plan would collide with the settled card — the UI dedupes by ``plan_id`` and
    the user would be left with nothing to answer while the tool blocked for the
    full timeout. Minting here makes every presentation a fresh card.

    Validation errors come back as tool errors rather than exceptions: the agent
    reads them and fixes its own arguments on the next call.
    """
    try:
        artifact = PlanArtifact.model_validate(
            {key: value for key, value in args.items() if key != "plan_id"}
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:10]
        )
        return {
            "content": [{"type": "text", "text": f"Invalid plan — fix and retry: {problems}"}],
            "is_error": True,
        }
    try:
        response = await bridge.present_plan(artifact)
    except PlanAlreadyPendingError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
    return {"content": [{"type": "text", "text": response.model_dump_json()}]}


def build_context_bridge(store: UiStateStore, bridge: SessionBridge) -> Any:
    """In-process MCP server exposing the workbench tools to one session."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "get_workspace_state",
        "Current workbench UI state: the file the user is looking at, open tabs, "
        "and files with unsaved changes (do NOT edit those).",
        {},
    )
    async def get_workspace_state(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": store.state.model_dump_json(indent=2)}]}

    @tool("present_plan", _PRESENT_PLAN_DESCRIPTION, plan_input_schema())
    async def present_plan(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_present_plan(bridge, args)

    return create_sdk_mcp_server(
        name="workbench", version="1.0.0", tools=[get_workspace_state, present_plan]
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
            "mcp__workbench__get_workspace_state",
            "mcp__workbench__present_plan",
        ],
        permission_mode="acceptEdits",
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        mcp_servers={"workbench": build_context_bridge(store, bridge)},
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
