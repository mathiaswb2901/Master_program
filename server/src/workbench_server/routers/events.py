"""/ws/events: the workspace-wide push channel.

Carries watcher file events and agent ``session_status`` events — anything a
client must know about without having opened the panel or socket that produced
it. The router stays a dumb fan-out: producers publish typed models on the bus.
"""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket

from workbench_server.services.event_bus import EventBus
from workbench_server.services.local_auth import ws_subprotocol_to_echo

router = APIRouter()


@router.websocket("/ws/events")
async def events_ws(ws: WebSocket) -> None:
    bus: EventBus = ws.app.state.event_bus
    # Echo the token subprotocol the client offered, or a browser fails the
    # handshake; the middleware already validated it (services/local_auth.py).
    await ws.accept(subprotocol=ws_subprotocol_to_echo(ws.scope))
    queue = bus.subscribe()

    async def pump() -> None:
        with contextlib.suppress(RuntimeError):  # ws already closed mid-send
            while True:
                event = await queue.get()
                await ws.send_text(event.model_dump_json())

    pump_task = asyncio.create_task(pump())
    try:
        # This socket carries nothing *from* the client — and it still has to
        # read. An ASGI server delivers `websocket.disconnect` to a receive and
        # nowhere else, so a handler that only ever sends never learns the
        # client is gone: it parks on the queue for the life of the process,
        # holding a subscription the bus keeps fanning out to, and a graceful
        # shutdown waits on it forever (uvicorn will not stop while a connection
        # task is live — Ctrl-C, or the desktop shell asking the backend to
        # quit, hangs). `agent_ws` and `terminal_ws` have a receive loop because
        # their clients talk; this one has it to hear the silence.
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
        bus.unsubscribe(queue)
