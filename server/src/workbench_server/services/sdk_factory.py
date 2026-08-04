"""The real ClaudeSDKClient factory + the context-bridge MCP server.

Isolated here so nothing else imports the SDK — sessions stay testable with fakes.
"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from workbench_server.models.agents import UiState
from workbench_server.models.plans import PlanArtifact, plan_input_schema
from workbench_server.services.agent_sessions import (
    PlanAlreadyPendingError,
    SdkClient,
    SessionBridge,
)

# Tools the agent may use inside its folder without asking. Deliberately file-only:
# anything else (Bash, web access, ...) falls through to can_use_tool and becomes
# a permission prompt in the UI. Auto-allowing them would shadow the callback.
_AUTO_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep"]

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

    Validation errors come back as tool errors rather than exceptions: the agent
    reads them and fixes its own arguments on the next call.
    """
    try:
        artifact = PlanArtifact.model_validate(args)
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


def sdk_client_factory(store: UiStateStore) -> Any:
    """Returns a ClientFactory closure for SessionManager."""

    def factory(folder: Path, resume_session_id: str | None, bridge: SessionBridge) -> SdkClient:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            PermissionResultDeny,
        )

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
            if await bridge.ask_permission(tool_name, tool_input):
                return PermissionResultAllow()
            return PermissionResultDeny(message="User declined in the workbench UI")

        options = ClaudeAgentOptions(
            cwd=str(folder),
            resume=resume_session_id,
            allowed_tools=[
                *_AUTO_ALLOWED,
                "mcp__workbench__get_workspace_state",
                "mcp__workbench__present_plan",
            ],
            permission_mode="acceptEdits",
            include_partial_messages=True,
            can_use_tool=can_use_tool,
            mcp_servers={"workbench": build_context_bridge(store, bridge)},
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    "You are running inside Workbench. Before editing files, call "
                    "get_workspace_state and avoid editing files listed as dirty "
                    "(unsaved user changes). When you propose multi-step work or "
                    "alternatives, call present_plan instead of writing the plan "
                    "as prose — the user answers it as an interactive card."
                ),
            },
        )
        client: SdkClient = ClaudeSDKClient(options=options)
        return client

    return factory
