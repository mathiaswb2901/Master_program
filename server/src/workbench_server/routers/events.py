"""/ws/events: pushes watcher events (and later: agent/session events) to the UI."""

import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from workbench_server.services.event_bus import EventBus

router = APIRouter()


@router.websocket("/ws/events")
async def events_ws(ws: WebSocket) -> None:
    bus: EventBus = ws.app.state.event_bus
    await ws.accept()
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
