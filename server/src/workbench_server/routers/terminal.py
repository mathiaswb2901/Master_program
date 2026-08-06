"""WebSocket endpoint bridging xterm.js to a PTY session."""

import asyncio
import contextlib

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from workbench_server.models.terminal import (
    TerminalExit,
    TerminalInput,
    TerminalOutput,
    terminal_client_message,
)
from workbench_server.services.pty_manager import PtyManager, PtySession
from workbench_server.services.terminal_stream import coalesced_output
from workbench_server.services.workspace import Workspace

log = structlog.get_logger()

router = APIRouter()


async def _pump_output(session: PtySession, ws: WebSocket) -> None:
    """PTY -> WebSocket until the process exits.

    One frame per *batch* of ConPTY reads, not per read — the batching policy
    (and why it costs no interactive latency) lives in `terminal_stream`.
    """
    with contextlib.suppress(RuntimeError):  # ws already closed mid-send
        async for chunk in coalesced_output(session):
            await ws.send_text(TerminalOutput(data=chunk).model_dump_json())
        await ws.send_text(TerminalExit().model_dump_json())


@router.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket) -> None:
    manager: PtyManager = ws.app.state.pty_manager
    # The live workspace object, not `settings.resolved_workspace()`: the root
    # can move now (M5 item 5), and reading the launch setting here would open
    # every new shell in the project the user had already left.
    workspace: Workspace = ws.app.state.workspace
    await ws.accept()

    session = manager.spawn(cwd=workspace.root)
    pump = asyncio.create_task(_pump_output(session, ws))
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
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        manager.release(session)
