"""The ``PreToolUse`` broker — the gate a shell request cannot walk around.

Why this exists rather than ``can_use_tool`` alone
--------------------------------------------------
``can_use_tool`` is only reached for calls the CLI's own permission evaluation
resolves to *ask*. Anything already resolved to *allow* — by ``allowed_tools``,
by ``permission_mode``, or by a ``permissions.allow`` rule in a workspace
settings file — never reaches the callback at all. A ``PreToolUse`` hook is
dispatched for **every** tool call before that evaluation matters, and a hook
deny binds even in permissive modes.

That distinction was not theoretical. Measured against the bundled CLI
(claude-agent-sdk 0.2.129) with the options this server shipped:

    permission_mode="acceptEdits", Bash "mkdir probe_dir" inside cwd
        -> can_use_tool: NOT called
        -> command executed, directory created
        -> no PermissionRequest, no board card, no provenance claim

The CLI's mode layer allows a Bash call outright when its base command is one of
``mkdir touch rm rmdir mv cp sed`` and ``permission_mode`` is ``acceptEdits``, so
``rm -rf`` and ``sed -i`` inside the workspace ran with no human in the loop.
Subagents inherit the parent's mode, so a worker inherited it too.

The fix has two halves, and both are load-bearing:

1. ``sdk_factory`` no longer sets ``acceptEdits`` (see there for why the editing
   ergonomics it appeared to buy were already provided by ``allowed_tools``).
2. This module brokers every shell/exec launcher through the *same*
   ``ask_permission`` future Mission Control's board answers — so the guarantee
   survives a future mode change, an inherited subagent mode, and a
   ``permissions.allow`` rule in a folder the user did not write.

The broker is deliberately narrow: it decides for the launchers in
:data:`BROKERED_TOOLS` and returns *no decision* for everything else, leaving
every other tool exactly where it was. Widening it is a separate, argued change
-- this one only tightens.

No SDK import lives here on purpose: ``sdk_factory`` is the single module that
touches ``claude_agent_sdk``, so the decision logic below stays testable with
plain dicts.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

log = structlog.get_logger()

# The tools that start a shell. ``Bash`` is the POSIX launcher; ``PowerShell``
# is what the CLI exposes in its place on Windows (verified: a Windows session's
# tool list carries ``PowerShell`` and omits ``Bash`` unless Git Bash is found,
# in which case it carries both). Naming only ``Bash`` would have left the
# Windows-first platform this project targets ungated, so both are listed.
#
# Deliberately *not* here: ``BashOutput`` and ``KillShell``, which read from or
# stop a shell that was already brokered when it started, and the file tools,
# which are auto-allowed by ``allowed_tools`` and tracked by provenance.
BROKERED_TOOLS = frozenset({"Bash", "PowerShell"})

_ALLOW = "allow"
_DENY = "deny"


def is_brokered(tool_name: str) -> bool:
    """Whether this tool must be answered by a human rather than a rule."""
    return tool_name in BROKERED_TOOLS


def broker_decision(tool_name: str, allowed: bool) -> dict[str, Any]:
    """The ``PreToolUse`` payload for a decision the human already made.

    Returning an explicit *allow* here is what keeps the prompt count at one:
    a ``PreToolUse`` allow decision skips ``can_use_tool``, so the request that
    this broker just escalated is not escalated a second time. That is not a
    reading of the docs — it is asserted against the bundled CLI by
    ``TestTheEndToEndRepro::test_a_shell_command_costs_exactly_one_human_answer``
    (the ``approved`` case), which registers this hook and ``can_use_tool``
    together on one options object, routes both through the same answer future,
    and requires that future to be awaited exactly once.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": _ALLOW if allowed else _DENY,
            "permissionDecisionReason": (
                f"Approved in the workbench UI ({tool_name})"
                if allowed
                else f"Declined in the workbench UI ({tool_name})"
            ),
        }
    }


def no_decision() -> dict[str, Any]:
    """Defer to the CLI's normal permission flow.

    An empty payload is *not* an approval — it means this broker has no opinion,
    so ``allowed_tools``, the permission mode and ``can_use_tool`` decide as
    they always have.
    """
    return {}


AskPermission = Callable[[str, dict[str, Any]], Awaitable[bool]]
PreToolUseHook = Callable[[dict[str, Any], str | None, Any], Awaitable[dict[str, Any]]]


def build_pre_tool_use_hook(ask_permission: AskPermission, session_id: str) -> PreToolUseHook:
    """A ``PreToolUse`` callback that escalates shell requests to the board.

    ``ask_permission`` is :meth:`AgentSession.ask_permission` — the same future
    the chat card and ``POST /api/agents/sessions/{id}/permission`` resolve, so a
    brokered shell request is answered by the doors that already exist rather
    than by a second mechanism.
    """

    async def pre_tool_use(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name") or "")
        if not is_brokered(tool_name):
            return no_decision()
        raw_input = input_data.get("tool_input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        allowed = await ask_permission(tool_name, tool_input)
        log.info(
            "agent.shell_brokered",
            session=session_id,
            tool=tool_name,
            allowed=allowed,
            # The agent id is present only for a subagent's calls; logging it is
            # how a worker's shell request is distinguishable from its parent's.
            agent_id=input_data.get("agent_id"),
        )
        return broker_decision(tool_name, allowed)

    return pre_tool_use
