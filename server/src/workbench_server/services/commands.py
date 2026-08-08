"""The command relay: the event bus, run backwards.

The window owns the command registry, so the backend cannot execute a command —
it *relays* one to a connected window and waits to hear back. Three moving parts:

* the **manifest** the window publishes on connect (``set_manifest``), which is
  the whole of what may be invoked — an id absent from it is refused, never
  guessed at (``is_registered``);
* an **invoke** (``invoke``) that mints a correlation id, publishes a
  :class:`CommandInvokeEvent` on the shared bus, and awaits the window's report;
* a **result** (``resolve``) the window posts back, which completes the awaiting
  invoke.

This is loop-affine: ``invoke`` and ``resolve`` run on the server's event loop,
so the pending-future map needs no lock. A window that never answers is bounded
by the invoke timeout, so a closed window cannot wedge a caller forever.
"""

import asyncio
from uuid import uuid4

import structlog

from workbench_server.models.commands import (
    CommandInvokeEvent,
    CommandInvokeResult,
    CommandManifest,
    CommandManifestItem,
)
from workbench_server.services.event_bus import EventBus

log = structlog.get_logger()

#: How long an invoke waits for the window to report back before giving up. A
#: command runs in the browser in milliseconds; this is the ceiling for a window
#: that received the event but never answered (mid-close, wedged), so a CLI or
#: agent gets a clear "did not confirm" instead of hanging.
INVOKE_TIMEOUT_SECONDS = 10.0


class CommandRelay:
    """Relay a registered window command from a shell/agent to a live window."""

    def __init__(self, event_bus: EventBus, *, timeout: float = INVOKE_TIMEOUT_SECONDS) -> None:
        self._bus = event_bus
        self._timeout = timeout
        self._manifest: list[CommandManifestItem] = []
        self._pending: dict[str, asyncio.Future[CommandInvokeResult]] = {}

    # ---- the manifest the window publishes ----------------------------------

    def set_manifest(self, manifest: CommandManifest) -> None:
        """Replace the invocable-command list. Last window to connect wins — in
        the one-window app that is the current window, which is what a CLI or
        agent means by "the commands available right now"."""
        self._manifest = list(manifest.commands)
        log.info("commands.manifest_published", count=len(self._manifest))

    def manifest(self) -> CommandManifest:
        return CommandManifest(commands=list(self._manifest))

    def is_registered(self, command_id: str) -> bool:
        return any(item.id == command_id for item in self._manifest)

    # ---- invoke / result ----------------------------------------------------

    async def invoke(self, command_id: str, params: dict[str, object]) -> CommandInvokeResult:
        """Relay one command and wait for the window's verdict.

        Refuses before touching the bus when no window has published a manifest:
        with nothing connected there is nothing to run it, and the honest answer
        is "connect a window", not a ten-second wait that times out (AXI shape 2).
        """
        invocation_id = uuid4().hex
        if not self._manifest:
            return CommandInvokeResult(
                invocation_id=invocation_id,
                dispatched=False,
                ok=False,
                detail="No Workbench window is connected to run commands.",
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CommandInvokeResult] = loop.create_future()
        self._pending[invocation_id] = future
        self._bus.publish(
            CommandInvokeEvent(invocation_id=invocation_id, command_id=command_id, params=params)
        )
        try:
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            return CommandInvokeResult(
                invocation_id=invocation_id,
                dispatched=True,
                ok=False,
                detail=f"The window did not confirm within {self._timeout:.0f}s.",
            )
        finally:
            self._pending.pop(invocation_id, None)

    def resolve(self, invocation_id: str, *, ok: bool, detail: str) -> bool:
        """Complete an awaiting invoke with the window's report. Returns whether
        an invoke was actually waiting — a stale or duplicate result (the invoke
        already timed out and moved on) is a no-op, not an error."""
        future = self._pending.get(invocation_id)
        if future is None or future.done():
            return False
        future.set_result(
            CommandInvokeResult(invocation_id=invocation_id, dispatched=True, ok=ok, detail=detail)
        )
        return True
