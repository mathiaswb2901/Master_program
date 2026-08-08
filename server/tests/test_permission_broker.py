"""The permission boundary, asserted on the object the SDK is actually handed.

Background (measured against claude-agent-sdk 0.2.129's bundled CLI, not
inferred from docs): with ``permission_mode="acceptEdits"`` a ``Bash`` call whose
base command is one of ``mkdir touch rm rmdir mv cp sed`` is allowed by the mode
layer, and an auto-approved call never reaches ``can_use_tool``. ``mkdir`` inside
the workspace therefore ran with no permission card at all. ``TestTheEndToEndRepro``
below is that reproduction, driven through the real CLI; the rest of this file
pins the fix in the fast lane.
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from workbench_server.config import Settings
from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    WordEdit,
    WordText,
)
from workbench_server.services.commands import CommandRelay
from workbench_server.services.event_bus import EventBus
from workbench_server.services.office_host.document_bridge import DocNotHostedError
from workbench_server.services.permission_broker import (
    BROKERED_TOOLS,
    broker_decision,
    is_brokered,
    no_decision,
)
from workbench_server.services.sdk_factory import UiStateStore, build_agent_options
from workbench_server.services.search import SearchService
from workbench_server.services.validation import ValidationService
from workbench_server.services.workspace import Workspace


class StubBridge:
    """Records what the broker escalated, and answers however the test says."""

    def __init__(self, answer: bool = False) -> None:
        self.session_id = "sess-1"
        self.answer = answer
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        self.asked.append((tool, tool_input))
        return self.answer

    def emit(self, event: Any) -> None:  # pragma: no cover - unused here
        pass


class StubReader:
    async def document_structure(self, path: str) -> DocStructure:  # pragma: no cover
        raise DocNotHostedError(path)

    async def read_document(  # pragma: no cover
        self,
        path: str,
        *,
        max_chars: int,
        max_cells: int,
        sheet: str | None = None,
        a1_range: str | None = None,
        start_paragraph: int = 0,
    ) -> WordText | CellWindow:
        raise DocNotHostedError(path)

    async def write_document(  # pragma: no cover
        self,
        path: str,
        *,
        content: str,
        paragraph: int | None = None,
        sheet: str | None = None,
        cell: str | None = None,
    ) -> WordEdit | CellEdit:
        raise DocNotHostedError(path)


def options_for(kind: str = "chat", bridge: Any = None) -> Any:
    return build_agent_options(
        UiStateStore(),
        Settings(),
        Path.cwd(),
        None,
        cast("Any", bridge or StubBridge()),
        StubReader(),
        CommandRelay(EventBus()),
        ValidationService(Path.cwd(), EventBus()),
        SearchService(Workspace(Path.cwd())),
        kind,  # type: ignore[arg-type]
    )


def hook_of(options: Any) -> Any:
    matchers = options.hooks["PreToolUse"]
    assert len(matchers) == 1
    return matchers[0].hooks[0]


class TestTheModeNoLongerAutoApprovesShell:
    def test_accept_edits_is_not_the_permission_mode(self) -> None:
        """The one-line cause of the hole, pinned so it cannot come back.

        ``acceptEdits`` reads like it only concerns file edits. It does not: it
        also allows shell filesystem commands, below ``can_use_tool``.
        """
        assert options_for().permission_mode != "acceptEdits"

    def test_no_shell_launcher_is_auto_allowed(self) -> None:
        for kind in ("chat", "orchestrator", "worker"):
            allowed = set(options_for(kind).allowed_tools)
            assert not (allowed & BROKERED_TOOLS), kind

    def test_editing_ergonomics_are_preserved_by_the_allow_list(self) -> None:
        """The reason dropping ``acceptEdits`` costs the user nothing.

        A whole-tool ``allowed_tools`` entry auto-approves before the callback is
        consulted, so ordinary Write/Edit still never prompts — that behaviour
        was never coming from the permission mode.
        """
        allowed = set(options_for().allowed_tools)
        assert {"Read", "Edit", "Write", "Glob", "Grep"} <= allowed


class TestTheBrokerBinds:
    def test_a_pre_tool_use_hook_is_registered(self) -> None:
        assert callable(hook_of(options_for()))

    @pytest.mark.parametrize("tool", sorted(BROKERED_TOOLS))
    async def test_a_shell_request_is_escalated_not_auto_approved(self, tool: str) -> None:
        bridge = StubBridge(answer=False)
        hook = hook_of(options_for(bridge=bridge))
        out = await hook({"tool_name": tool, "tool_input": {"command": "rm -rf ."}}, None, None)
        assert bridge.asked == [(tool, {"command": "rm -rf ."})]
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.mark.parametrize("tool", sorted(BROKERED_TOOLS))
    async def test_a_human_yes_allows_it_exactly_once(self, tool: str) -> None:
        """An explicit allow is what stops the request being asked twice: the
        CLI skips ``can_use_tool`` when a PreToolUse hook decides."""
        bridge = StubBridge(answer=True)
        hook = hook_of(options_for(bridge=bridge))
        out = await hook({"tool_name": tool, "tool_input": {"command": "mkdir x"}}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert len(bridge.asked) == 1

    async def test_the_broker_holds_no_opinion_on_file_tools(self) -> None:
        """No regression to the editing flow: the hook must not intercept the
        tools ``allowed_tools`` auto-approves and provenance correlates."""
        bridge = StubBridge(answer=True)
        hook = hook_of(options_for(bridge=bridge))
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Glob", "Grep"):
            assert await hook({"tool_name": tool, "tool_input": {}}, None, None) == no_decision()
        assert bridge.asked == []

    async def test_a_worker_gains_no_capability_its_parent_lacks(self) -> None:
        """Mission Control's first constraint, on the worker's own options."""
        bridge = StubBridge(answer=False)
        hook = hook_of(options_for("worker", bridge=bridge))
        out = await hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert bridge.asked == [("Bash", {"command": "ls"})]

    async def test_a_malformed_tool_input_still_escalates(self) -> None:
        """A missing or non-dict ``tool_input`` must not become an approval."""
        bridge = StubBridge(answer=False)
        hook = hook_of(options_for(bridge=bridge))
        for payload in ({"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": None}):
            out = await hook(payload, None, None)
            assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert len(bridge.asked) == 2


class TestTheBrokeredSet:
    def test_both_platforms_shell_launchers_are_covered(self) -> None:
        """The CLI exposes ``Bash`` on POSIX and substitutes ``PowerShell`` on
        Windows (verified from a real session's init tool list). Naming only
        ``Bash`` would leave the Windows-first target ungated."""
        assert {"Bash", "PowerShell"} <= BROKERED_TOOLS

    def test_reading_a_running_shell_is_not_re_brokered(self) -> None:
        """``BashOutput``/``KillShell`` act on a shell already approved when it
        started; brokering them would prompt twice for one command."""
        assert not is_brokered("BashOutput")
        assert not is_brokered("KillShell")

    def test_a_decision_names_the_tool_it_answered(self) -> None:
        reason = broker_decision("Bash", False)["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Bash" in reason


# ---------------------------------------------------------------------------
# The end-to-end reproduction.
# ---------------------------------------------------------------------------

# Which launcher the CLI exposes is a property of the host, not a choice: it
# substitutes ``PowerShell`` for ``Bash`` on Windows.
SHELL_TOOL = "PowerShell" if os.name == "nt" else "Bash"


class _FakeAnthropic(BaseHTTPRequestHandler):
    """Stands in for the Messages API so the real CLI can be driven offline.

    Only the model's token stream is faked. The permission evaluation, the hook
    dispatch, the ``can_use_tool`` dispatch and the execution below it are the
    shipped CLI doing its real work -- which is the whole point: the claim under
    test is about that code, not about ours.
    """

    turns = 0
    tool_name = SHELL_TOOL
    command = "mkdir probe_dir"

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, event: str, data: dict[str, Any]) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        type(self).turns += 1
        first = type(self).turns == 1
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self._send(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "type": "message",
                    "role": "assistant",
                    "model": "probe",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        )
        if first:
            self._send(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"toolu_{uuid.uuid4().hex[:12]}",
                        "name": type(self).tool_name,
                        "input": {},
                    },
                },
            )
            self._send(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            {"command": type(self).command, "description": "probe"}
                        ),
                    },
                },
            )
        else:
            self._send(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            self._send(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "done"},
                },
            )
        self._send("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._send(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if first else "end_turn"},
                "usage": {"output_tokens": 1},
            },
        )
        self._send("message_stop", {"type": "message_stop"})


@pytest.mark.skipif(
    os.environ.get("WORKBENCH_PERMISSION_REPRO") != "1",
    reason=(
        "spawns the bundled CLI against a stand-in API; set "
        "WORKBENCH_PERMISSION_REPRO=1 to run the full reproduction"
    ),
)
class TestTheEndToEndRepro:
    """Drives a real shell tool call through the real CLI, as an agent would.

    Skipped by default because it spawns the 266 MB bundled CLI and depends on
    which shell tool that CLI exposes on the host. It is the reproduction the
    fix was written against, kept runnable rather than described.

    Platform caveat, stated rather than hidden: the ``acceptEdits`` fast path is
    keyed on the *Bash* tool, so only the POSIX run of this test discriminates
    between the old configuration and the new one. On Windows the CLI hands out
    ``PowerShell``, which reached ``can_use_tool`` even under ``acceptEdits``
    (measured), so the Windows run asserts the broker works rather than that it
    was needed. The Windows evidence for the hole came from driving the same
    harness with ``CLAUDE_CODE_GIT_BASH_PATH`` set, which makes the CLI offer
    ``Bash`` as well; that path is machine-specific and so is not baked in here.
    """

    async def test_a_shell_command_does_not_run_without_a_human(self, tmp_path: Path) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

        bridge = StubBridge(answer=False)
        shipped = options_for(bridge=bridge)
        _FakeAnthropic.turns = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropic)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            options = ClaudeAgentOptions(
                cwd=str(tmp_path),
                allowed_tools=shipped.allowed_tools,
                permission_mode=shipped.permission_mode,
                can_use_tool=shipped.can_use_tool,
                hooks={"PreToolUse": [HookMatcher(hooks=[hook_of(shipped)])]},
                setting_sources=[],
                env={
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_API_KEY": "probe",
                },
            )
            async with ClaudeSDKClient(options=options) as client:
                await client.query("probe")
                async for _ in client.receive_response():
                    pass
        finally:
            server.shutdown()

        assert bridge.asked, "the shell request never reached the broker"
        assert not (tmp_path / "probe_dir").is_dir(), "a declined command still ran"
