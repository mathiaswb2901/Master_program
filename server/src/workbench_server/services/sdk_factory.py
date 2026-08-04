"""The real ClaudeSDKClient factory + the context-bridge MCP server.

Isolated here so nothing else imports the SDK — sessions stay testable with fakes.
"""

from pathlib import Path
from typing import Any

from workbench_server.models.agents import UiState
from workbench_server.services.agent_sessions import PermissionAsk, SdkClient

# Tools the agent may use inside its folder without asking. Deliberately file-only:
# anything else (Bash, web access, ...) falls through to can_use_tool and becomes
# a permission prompt in the UI. Auto-allowing them would shadow the callback.
_AUTO_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep"]


class UiStateStore:
    """Latest UI state pushed by the frontend; read by agents via MCP."""

    def __init__(self) -> None:
        self.state = UiState()


def build_context_bridge(store: UiStateStore) -> Any:
    """In-process MCP server exposing get_workspace_state to every session."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "get_workspace_state",
        "Current workbench UI state: the file the user is looking at, open tabs, "
        "and files with unsaved changes (do NOT edit those).",
        {},
    )
    async def get_workspace_state(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": store.state.model_dump_json(indent=2)}]}

    return create_sdk_mcp_server(name="workbench", version="1.0.0", tools=[get_workspace_state])


def sdk_client_factory(store: UiStateStore) -> Any:
    """Returns a ClientFactory closure for SessionManager."""

    def factory(folder: Path, resume_session_id: str | None, ask: PermissionAsk) -> SdkClient:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            PermissionResultDeny,
        )

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
            if await ask(tool_name, tool_input):
                return PermissionResultAllow()
            return PermissionResultDeny(message="User declined in the workbench UI")

        options = ClaudeAgentOptions(
            cwd=str(folder),
            resume=resume_session_id,
            allowed_tools=[*_AUTO_ALLOWED, "mcp__workbench__get_workspace_state"],
            permission_mode="acceptEdits",
            include_partial_messages=True,
            can_use_tool=can_use_tool,
            mcp_servers={"workbench": build_context_bridge(store)},
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    "You are running inside Workbench. Before editing files, call "
                    "get_workspace_state and avoid editing files listed as dirty "
                    "(unsaved user changes)."
                ),
            },
        )
        client: SdkClient = ClaudeSDKClient(options=options)
        return client

    return factory
