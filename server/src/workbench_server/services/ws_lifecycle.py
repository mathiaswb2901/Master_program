"""What a WebSocket endpoint owes the resource behind it when the peer vanishes.

Both live sockets — ``/ws/terminal`` and ``/ws/agent/{id}`` — have the same
shape: a background *pump* task sending frames out while the handler reads
frames in, and a ``finally:`` that hands a real resource back (an OS shell
process, a listener queue). Two rules make that teardown actually run, and both
live here because each router getting them right independently is how one of
them stops doing so.

**1. A pump never raises.** ``ws.send_text`` racing the peer disappearing is the
*normal* way a pump ends, and which exception it raises depends on how far the
disconnect has travelled. Starlette raises ``RuntimeError`` once the ASGI server
has already reported the close; below that, an abrupt TCP drop surfaces as the
transport's own error — an ``OSError`` (``ConnectionResetError``) from the
socket, anyio's ``BrokenResourceError``/``ClosedResourceError`` under the
memory-stream path a ``TestClient`` drives, or ``websockets``' own
``ConnectionClosed`` under uvicorn's default implementation. Catching only
``RuntimeError`` (what both routers shipped with) let the other three out.

**2. Draining the pump can never skip the release.** ``await task`` re-raises
whatever the task *stored*, and a pump that already died mid-send stored that —
not ``CancelledError``. So ``contextlib.suppress(asyncio.CancelledError)``
around the await, with the release on the line after it, meant an abrupt
disconnect propagated out of the ``finally:`` and the release never ran: the
shell process was orphaned for the life of the server and its session stayed in
``PtyManager._sessions`` forever. ``drain_pump`` consumes the outcome instead,
and the callers nest the release in a ``finally:`` of its own so no future
``await`` can get in front of it either.

The two are belt and braces on purpose: rule 1 keeps the log honest about *why*
a socket ended, rule 2 makes the teardown survive a pump that ends some way rule
1 did not anticipate.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import anyio
import structlog
from fastapi import WebSocket

log = structlog.get_logger()

_lost: list[type[BaseException]] = [
    # Starlette, once the ASGI server has already handed it the disconnect.
    RuntimeError,
    # The socket itself: ConnectionResetError/BrokenPipeError are OSErrors.
    OSError,
    # anyio's object streams — what a starlette TestClient sends frames over.
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
]
try:  # ships with uvicorn[standard]; a wsproto-only install has no such module
    from websockets.exceptions import WebSocketException
except ImportError:  # pragma: no cover - installed here via uvicorn[standard]
    pass
else:
    _lost.append(WebSocketException)

#: Every way "the peer is gone" reaches a send. See rule 1 above.
CONNECTION_LOST: tuple[type[BaseException], ...] = tuple(_lost)


async def send_frames(ws: WebSocket, frames: AsyncIterator[str], **context: object) -> None:
    """Send `frames` until they run out or the peer goes; never raises for either.

    The body of both pumps. `frames` is an async generator so each router keeps
    its own framing (coalesced PTY output plus a terminal exit frame; a session's
    event queue) while sharing the one rule that matters to teardown.
    """
    try:
        async for frame in frames:
            await ws.send_text(frame)
    except CONNECTION_LOST as exc:
        log.debug("ws.peer_gone", error=type(exc).__name__, **context)


async def drain_pump(task: asyncio.Task[None], **context: object) -> None:
    """Stop a pump task and consume its outcome; never raises on its account.

    Anything that is not a disconnect is logged with its traceback rather than
    swallowed silently: the socket is over either way, but a serialization bug
    must not be mistaken for a client closing its tab.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        try:
            await task
        except CONNECTION_LOST as exc:
            log.debug("ws.pump_peer_gone", error=type(exc).__name__, **context)
        except Exception:
            log.exception("ws.pump_failed", **context)
