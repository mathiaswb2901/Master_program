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

**Two layers validate a parameterised invocation, and they check different
things.** Here, before the bus: the *shape*, against the ``params_schema`` the
window published — an unknown field, a missing argument or a value of the wrong
type is refused immediately, naming the field. In the window, at ``run()``: what
the argument *means* — whether that layout name exists, whether that folder is
on the recent list. The relay deliberately holds no opinion on the second: the
window owns the registry, and a second authority on what a workspace is would be
one more thing to keep honest with nothing reading it.
"""

import asyncio
from typing import Any
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

    def item(self, command_id: str) -> CommandManifestItem | None:
        return next((item for item in self._manifest if item.id == command_id), None)

    # ---- parameter validation (before the bus) ------------------------------

    def params_refusal(self, command_id: str, params: dict[str, Any]) -> str | None:
        """Why these arguments are not this command's, or ``None`` if they are.

        Every refusal names the offending field *and* what would be accepted —
        a caller told only "invalid params" spends a round trip discovering
        which one, and a round trip is the cost this whole seam exists to avoid
        (AXI shape 3). Checked against the published manifest, so an id nobody
        published never reaches here (the router 404s first).
        """
        item = self.item(command_id)
        if item is None:  # pragma: no cover - the router refuses first
            return f"{command_id!r} is not a registered command."
        schema = item.params_schema
        if schema is None:
            if not params:
                return None
            named = ", ".join(sorted(params))
            return f"{command_id} takes no parameters (got {named})."
        known = {spec.name: spec for spec in schema.params}
        accepted = ", ".join(known) or "none"
        for name in sorted(params):
            if name not in known:
                return f"{command_id} has no parameter {name!r}. It takes: {accepted}."
        for spec in schema.params:
            if spec.name not in params:
                if spec.required:
                    hint = f" ({spec.detail})" if spec.detail else ""
                    return f"{command_id} needs {spec.name!r}{hint}. It takes: {accepted}."
                continue
            value = params[spec.name]
            if not isinstance(value, str):
                kind = type(value).__name__
                return f"{command_id}: {spec.name!r} must be a string, got {kind}."
            if len(value) > spec.limit():
                return (
                    f"{command_id}: {spec.name!r} is {len(value)} characters; "
                    f"the limit is {spec.limit()}."
                )
        return None

    # ---- invoke / result ----------------------------------------------------

    async def invoke(self, command_id: str, params: dict[str, Any]) -> CommandInvokeResult:
        """Relay one command and wait for the window's verdict.

        Refuses before touching the bus when no window has published a manifest:
        with nothing connected there is nothing to run it, and the honest answer
        is "connect a window", not a ten-second wait that times out (AXI shape 2).
        Arguments that do not match the published schema are refused in the same
        place and for the same reason.
        """
        invocation_id = uuid4().hex
        if not self._manifest:
            return CommandInvokeResult(
                invocation_id=invocation_id,
                dispatched=False,
                ok=False,
                detail="No Workbench window is connected to run commands.",
            )
        refusal = self.params_refusal(command_id, params)
        if refusal is not None:
            log.info("commands.params_refused", command_id=command_id, detail=refusal)
            return CommandInvokeResult(
                invocation_id=invocation_id, dispatched=False, ok=False, detail=refusal
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
