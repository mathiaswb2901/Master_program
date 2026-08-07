"""/ws/events: the workspace-wide push channel.

Carries watcher file events and agent ``session_status`` events — anything a
client must know about without having opened the panel or socket that produced
it. The router stays a dumb fan-out: producers publish typed models on the bus.
"""

import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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
    try:
        while True:
            event = await queue.get()
            await ws.send_text(event.model_dump_json())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            bus.unsubscribe(queue)
