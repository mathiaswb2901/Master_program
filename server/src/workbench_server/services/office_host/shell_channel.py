"""The server's one way to reach a native window: the desktop shell.

Reparenting an `HWND` is Win32 work on the thread that owns the Tauri window,
which lives in another process entirely — `desktop/src-tauri/src/host/`. The
Python service owns the *lifecycle* (it launched the process, it knows the pid,
it reaps on shutdown), so the two have to talk, and the direction that does not
exist yet is server → shell.

**The webview is the wire.** A `#[tauri::command]` can only be called from the
page, so the path is: service → this channel → `/ws/office-host` →
`ui/src/officeHostChannel.ts` → Tauri IPC → Rust. One socket, one command at a
time, an ack for each. The alternative — a second local port spoken by the
shell — would be another listener, another lifetime and another thing to
secure, for a hop the webview already has.

**A command that is not acked is a failed command, never a silent one.**
Everything here is bounded: no shell attached fails immediately, and an attached
shell that stops answering fails on a timeout. The one exception is
:meth:`ShellChannel.post`, used for geometry, where waiting would put a network
round trip in a drag frame and the answer could not change anything anyway (the
Rust side moves guests fire-and-forget for the same reason, and liveness is the
poll's job).

**One shell at a time.** A second connection displaces the first rather than
being refused: the usual cause is a webview that reloaded before its old socket
was reaped, and refusing would leave hosting broken until a restart.
"""

import asyncio
import contextlib
import uuid
from typing import Protocol

import structlog

from workbench_server.models.office_host import (
    HostCommand,
    HostCommandAck,
    HostCommandAction,
    PanelRect,
)

log = structlog.get_logger()

#: How long an acked command may take. Generous next to the work itself (a
#: `SetParent` and a `SetWindowPos`), because it also covers the webview being
#: busy — but far under the service's own 30 s ceiling, so the refusal that
#: reaches the UI names the shell rather than "it never came back".
COMMAND_TIMEOUT_S = 10.0


class ShellSocket(Protocol):
    """The half of ``fastapi.WebSocket`` this module uses.

    Narrow on purpose: it is what lets every test here drive the channel with a
    plain object instead of a running server and a real client.
    """

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...


class ShellUnavailableError(Exception):
    """No shell is attached, so no window operation can happen at all."""


class ShellCommandError(Exception):
    """The shell answered, and the answer was no.

    ``code`` is the Rust ``HostErrorCode`` — ``embed_refused``,
    ``window_gone``, ``unknown_host``, ``invalid_rect``, ``main_thread``,
    ``win32``, ``unsupported`` — or None when the failure was ours (a timeout,
    a socket that closed mid-command).
    """

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code


class ShellChannel:
    """One attached shell, and the commands in flight to it."""

    def __init__(self, *, timeout_s: float = COMMAND_TIMEOUT_S) -> None:
        self._socket: ShellSocket | None = None
        self._timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future[HostCommandAck]] = {}
        #: Strong references to the fire-and-forget writes; asyncio keeps none
        #: of its own and a garbage-collected task is a dropped resize.
        self._posted: set[asyncio.Task[None]] = set()
        #: Serialises sends. Two coroutines writing to one WebSocket is a
        #: protocol error in Starlette, and a resize storm during an embed is
        #: exactly when that would happen.
        self._send_lock = asyncio.Lock()

    @property
    def attached(self) -> bool:
        return self._socket is not None

    # ---- the socket ---------------------------------------------------------

    async def serve(self, socket: ShellSocket) -> None:
        """Own ``socket`` until it closes, answering acks into their futures.

        Returns when the socket ends; the router turns that into a close. The
        caller has already accepted the connection — accepting here would put
        transport handling in a service.
        """
        previous = self._socket
        if previous is not None:
            log.info("office_host.shell_replaced")
        self._socket = socket
        log.info("office_host.shell_attached")
        try:
            while True:
                raw = await socket.receive_text()
                self._on_message(raw)
        finally:
            # Only if it is still *this* socket: a reconnect that displaced us
            # has already installed its own, and clearing it here would leave
            # the live shell invisible to the service.
            if self._socket is socket:
                self._socket = None
                self._fail_pending("the desktop shell disconnected")
            log.info("office_host.shell_detached")

    def _on_message(self, raw: str) -> None:
        try:
            ack = HostCommandAck.model_validate_json(raw)
        except ValueError:
            # A frame we cannot parse is the shell's bug, not a reason to drop
            # the connection every hosted window depends on.
            log.warning("office_host.shell_bad_frame", raw=raw[:200])
            return
        future = self._pending.pop(ack.command_id, None)
        if future is None or future.done():
            # A late ack for a command that already timed out. Nothing to do —
            # the caller has long since been told no.
            return
        future.set_result(ack)

    def _fail_pending(self, detail: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ShellCommandError(None, detail))
        self._pending.clear()

    # ---- sending ------------------------------------------------------------

    async def call(
        self,
        host_id: str,
        action: HostCommandAction,
        *,
        window_id: int | None = None,
        rect: PanelRect | None = None,
        visible: bool | None = None,
    ) -> None:
        """Send one command and wait for its ack. Raises on anything but ok."""
        socket = self._socket
        if socket is None:
            raise ShellUnavailableError("the desktop shell is not attached")
        command = HostCommand(
            command_id=uuid.uuid4().hex[:12],
            host_id=host_id,
            action=action,
            window_id=window_id,
            rect=rect,
            visible=visible,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[HostCommandAck] = loop.create_future()
        self._pending[command.command_id] = future
        try:
            await self._send(socket, command)
            ack = await asyncio.wait_for(future, self._timeout_s)
        except TimeoutError as error:
            raise ShellCommandError(
                None, f"the shell did not answer {action} within {self._timeout_s:g}s"
            ) from error
        finally:
            self._pending.pop(command.command_id, None)
        if not ack.ok:
            raise ShellCommandError(ack.code, ack.message or f"the shell refused {action}")

    def post(
        self,
        host_id: str,
        action: HostCommandAction,
        *,
        rect: PanelRect | None = None,
        visible: bool | None = None,
    ) -> None:
        """Send one command without waiting. Geometry only — see the module docs.

        Schedules the write rather than performing it, so a drag frame costs the
        caller a task creation and nothing else.
        """
        socket = self._socket
        if socket is None:
            raise ShellUnavailableError("the desktop shell is not attached")
        command = HostCommand(
            command_id=uuid.uuid4().hex[:12],
            host_id=host_id,
            action=action,
            rect=rect,
            visible=visible,
        )
        task = asyncio.create_task(self._send_quietly(socket, command))
        self._posted.add(task)
        task.add_done_callback(self._posted.discard)

    async def _send(self, socket: ShellSocket, command: HostCommand) -> None:
        async with self._send_lock:
            await socket.send_text(command.model_dump_json())

    async def _send_quietly(self, socket: ShellSocket, command: HostCommand) -> None:
        try:
            await self._send(socket, command)
        except Exception as error:  # a closed socket, mid-drag
            log.debug("office_host.shell_post_failed", action=command.action, detail=str(error))

    async def close(self) -> None:
        """Fail everything in flight. Server shutdown, where a coroutine waiting
        on a shell that is about to go away would hold the lifespan open."""
        self._fail_pending("the server is shutting down")
        for task in list(self._posted):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
