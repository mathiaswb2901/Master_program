"""WebSocket endpoint bridging xterm.js to a PTY session."""

import asyncio
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from workbench_server.models.terminal import (
    TerminalExit,
    TerminalInput,
    TerminalOutput,
    terminal_client_message,
)
from workbench_server.services.local_auth import ws_subprotocol_to_echo
from workbench_server.services.pty_manager import PtyManager, PtySession
from workbench_server.services.terminal_stream import coalesced_output
from workbench_server.services.workspace import Workspace
from workbench_server.services.ws_lifecycle import drain_pump, send_frames

log = structlog.get_logger()

router = APIRouter()


async def _output_frames(session: PtySession) -> AsyncIterator[str]:
    """The session's output as wire frames, ending with the exit frame.

    One frame per *batch* of ConPTY reads, not per read — the batching policy
    (and why it costs no interactive latency) lives in `terminal_stream`.
    """
    async for chunk in coalesced_output(session):
        yield TerminalOutput(data=chunk).model_dump_json()
    yield TerminalExit().model_dump_json()


@router.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket) -> None:
    manager: PtyManager = ws.app.state.pty_manager
    # The live workspace object, not `settings.resolved_workspace()`: the root
    # can move now (M5 item 5), and reading the launch setting here would open
    # every new shell in the project the user had already left.
    workspace: Workspace = ws.app.state.workspace
    # Echo the offered token subprotocol (middleware already validated it), or a
    # browser fails the handshake. None when none was offered.
    await ws.accept(subprotocol=ws_subprotocol_to_echo(ws.scope))

    session = manager.spawn(cwd=workspace.root)
    pump = asyncio.create_task(
        send_frames(ws, _output_frames(session), stream="terminal", session_id=session.session_id)
    )
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = terminal_client_message.validate_json(raw)
            except ValidationError:
                log.warning("terminal.bad_message", session_id=session.session_id)
                continue
            if isinstance(msg, TerminalInput):
                session.write(msg.data)
            else:
                session.resize(msg.rows, msg.cols)
    except WebSocketDisconnect:
        pass
    finally:
        # The release is the line that hands the OS its child process back, so
        # it gets a `finally:` of its own — never a line *after* an `await` that
        # can still raise. And it goes off the loop thread: reaping a wedged
        # child blocks for up to a second, which on this thread is a second of
        # dead server for every other socket. Both rules: `services/ws_lifecycle`
        # and `PtyManager._TEARDOWN_THREADS`.
        try:
            await drain_pump(pump, stream="terminal", session_id=session.session_id)
        finally:
            await manager.release_async(session)
