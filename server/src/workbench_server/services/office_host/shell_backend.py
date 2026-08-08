"""The real host backend: COM starts it, the shell docks it.

This is the implementation the ``HostBackend`` Protocol was written for, and the
split inside it is the honest one rather than a tidy one:

* **The process is the server's.** Launching, proving ownership, polling and
  reaping are COM and Win32 calls made here, through
  :mod:`~workbench_server.services.office_host.office_com`, because the service
  above already owns the pid, the lifecycle, and the shutdown that has to reap.
* **The window is the shell's.** ``SetParent`` has to run on the thread that
  owns the Tauri window, in another process, so ``embed``/``set_bounds``/
  ``set_visible``/``detach`` and the window half of ``close`` are commands sent
  over :mod:`~workbench_server.services.office_host.shell_channel`.

**All COM on one thread.** COM objects belong to the apartment that created
them, so every call runs on a single worker thread that ``CoInitialize``s itself
once — never on the event loop, which would block every other request behind a
Word that is thinking, and never on a pool, which would hand a proxy to a thread
that cannot use it.

**Every call is bounded here, not only above it.** The service applies its own
ceiling as a backstop, but a backend that let its worker thread block forever
would still have leaked a thread and a Word. Launch is the slow one and has its
own timeout; the shell commands carry the channel's.

**A shell that goes away is not a lost document.** The panels live in the Rust
process, not in the webview, so a page reload drops the socket and finds
everything still docked. Only geometry is affected in the gap, which is why
``set_bounds`` alone treats "no shell" as something to log rather than as a
reason to fail the host.
"""

import asyncio
import concurrent.futures
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TypeVar

import structlog

from workbench_server.models.office_host import HostAppKind, HostCommandAction, PanelRect
from workbench_server.services.office_host import office_com
from workbench_server.services.office_host.backend import (
    DocumentOpenElsewhereError,
    EmbedRefusedError,
    HostBackendError,
    HostHandle,
    HostLiveness,
    LaunchFailedError,
    LaunchTimeoutError,
)
from workbench_server.services.office_host.office_com import OfficeInstance
from workbench_server.services.office_host.shell_channel import (
    ShellChannel,
    ShellCommandError,
    ShellUnavailableError,
)

log = structlog.get_logger()

T = TypeVar("T")

#: How long a launch may take before this backend gives up on its own — the
#: first line of defence the Protocol asks for.
#:
#: Word is ~1.6 s warm (measured), so this is generous for a cold start and
#: still short enough to be a *panel state* rather than an abandoned user. It is
#: deliberately not longer: the thing it usually catches is Office showing a
#: modal nobody asked for — "the last time you opened this, it caused a serious
#: error" was the one that turned up in testing — and a prompt that only the
#: person at the keyboard can answer does not get better with more waiting.
LAUNCH_TIMEOUT_S = 30.0


@dataclass
class _Bound:
    """One launched instance and the host it belongs to."""

    instance: OfficeInstance
    #: The service's host id, which is also the shell's registry key: one
    #: vocabulary for the same window in the log, in the REST payloads and in
    #: ``host_list`` on the Rust side.
    host_id: str


class ShellHostBackend:
    """Win32/COM host backend. Satisfies the ``HostBackend`` protocol."""

    def __init__(
        self, channel: ShellChannel, *, launch_timeout_s: float = LAUNCH_TIMEOUT_S
    ) -> None:
        self._channel = channel
        self._launch_timeout_s = launch_timeout_s
        #: One thread, for the life of the process. Created lazily, so importing
        #: this module on a machine that never hosts costs nothing.
        self._com: concurrent.futures.ThreadPoolExecutor | None = None
        #: pid -> what we launched. The pid is what the service binds a host to,
        #: so it is what everything here is keyed by.
        self._bound: dict[int, _Bound] = {}

    # ---- readiness -----------------------------------------------------------

    def ready(self) -> bool:
        """Hosting needs a shell to host *into*. A browser tab cannot reparent a
        window, and the honest answer before one is attached is "not now"."""
        return self._channel.attached

    # ---- HostBackend ---------------------------------------------------------

    async def launch(self, path: Path, kind: HostAppKind, host_id: str) -> HostHandle:
        # Where the apartment thread publishes the instance the moment it exists
        # — which is *before* the call that can block behind a modal Office
        # decided to show. Without it a launch that never returns would leave a
        # Word nobody can see and a worker thread nobody can use again.
        started: list[OfficeInstance] = []
        try:
            instance = await asyncio.wait_for(
                self._run(partial(office_com.launch, path, kind, started.append)),
                self._launch_timeout_s,
            )
        except TimeoutError as error:
            for abandoned in started:
                office_com.abandon(abandoned)
            raise LaunchTimeoutError(
                f"{kind} did not open the document within {self._launch_timeout_s:g}s"
            ) from error
        except office_com.DocumentBusyError as error:
            raise DocumentOpenElsewhereError(str(error)) from error
        except office_com.OfficeComError as error:
            raise LaunchFailedError(str(error)) from error
        if not instance.adopted:
            self._bound[instance.pid] = _Bound(instance, host_id)
        return HostHandle(pid=instance.pid, window_id=instance.window_id, adopted=instance.adopted)

    async def embed(self, handle: HostHandle, rect: PanelRect) -> None:
        await self._command(handle, "embed", window_id=handle.window_id, rect=rect)

    async def set_bounds(self, handle: HostHandle, rect: PanelRect) -> None:
        # Fire and forget: a drag frame must not wait for a round trip, and the
        # only failure a move can have is "the window is gone", which the poll
        # sweep asks about every couple of seconds anyway.
        host_id = self._host_id(handle)
        try:
            self._channel.post(host_id, "set_bounds", rect=rect)
        except ShellUnavailableError:
            log.debug("office_host.bounds_dropped", host=host_id)

    async def set_visible(self, handle: HostHandle, visible: bool) -> None:
        await self._command(handle, "set_visible", visible=visible)

    async def detach(self, handle: HostHandle) -> None:
        await self._command(handle, "detach")

    async def close(self, handle: HostHandle) -> None:
        """Give the window back, then end the process.

        Order matters. A window still parented into a panel whose process is
        being killed leaves our clip child holding a destroyed handle and the
        panel painting a hole; releasing first means the worst case is a real
        Word briefly on the desktop — which is exactly what the user should see
        if the close then refuses.

        The shell half is best-effort: a shell that has gone away has already
        taken its panels with it, and the process still has to be reaped.
        """
        with contextlib.suppress(ShellUnavailableError, ShellCommandError):
            await self._channel.call(self._host_id(handle), "close")
        bound = self._bound.get(handle.pid)
        if bound is None:
            return
        try:
            await self._run(partial(office_com.close, bound.instance))
        except office_com.OfficeComError as error:
            raise HostBackendError(str(error)) from error
        self._bound.pop(handle.pid, None)

    async def poll(self, handle: HostHandle) -> HostLiveness:
        bound = self._bound.get(handle.pid)
        if bound is None:
            return "gone"
        alive = await self._run(partial(office_com.is_running, bound.instance))
        return "alive" if alive else "gone"

    # ---- the document bridge's reach into the held instance ------------------

    def instance_for(self, pid: int) -> OfficeInstance | None:
        """The live COM instance this backend launched for ``pid``, or ``None``.

        ``None`` means the process was never ours or has since been closed and
        reaped — the document bridge reads it as "gone" rather than reaching for
        a proxy that is not there. The bridge shares this map rather than keeping
        a second one, exactly as the fake bridge shares the fake backend's.
        """
        bound = self._bound.get(pid)
        return bound.instance if bound is not None else None

    async def run_com(self, call: Callable[[], T]) -> T:
        """Run one blocking COM call on *this* backend's apartment thread.

        The document bridge cannot make its own thread: a COM proxy belongs to
        the apartment that created it, so a read of the live document has to run
        where the launch ran. Sharing the one executor is what keeps every COM
        call — launch, poll, read, write — on the single thread the module
        docstring requires."""
        return await self._run(call)

    # ---- shutdown ------------------------------------------------------------

    async def aclose(self) -> None:
        """Stop the apartment thread. Every instance has been closed by the
        service before this runs; anything still standing is reaped by its job
        object when this process exits."""
        executor, self._com = self._com, None
        if executor is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.get_running_loop().run_in_executor(
                executor, office_com.uninitialize_apartment
            )
        executor.shutdown(wait=False)

    # ---- internals -----------------------------------------------------------

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._com is None:
            self._com = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="office-com",
                initializer=office_com.initialize_apartment,
            )
        return self._com

    async def _run(self, call: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor(), call)

    def _host_id(self, handle: HostHandle) -> str:
        bound = self._bound.get(handle.pid)
        # A handle with no binding is one the service already refused (adopted)
        # or one whose instance has been closed. The window handle still names
        # it unambiguously, and the shell answers `unknown_host` rather than
        # touching something else.
        return bound.host_id if bound is not None else f"hwnd-{handle.window_id}"

    async def _command(
        self,
        handle: HostHandle,
        action: HostCommandAction,
        *,
        window_id: int | None = None,
        rect: PanelRect | None = None,
        visible: bool | None = None,
    ) -> None:
        host_id = self._host_id(handle)
        try:
            await self._channel.call(
                host_id, action, window_id=window_id, rect=rect, visible=visible
            )
        except ShellUnavailableError as error:
            raise EmbedRefusedError(f"the desktop shell is not attached: {error}") from error
        except ShellCommandError as error:
            log.warning("office_host.shell_refused", host=host_id, action=action, code=error.code)
            raise EmbedRefusedError(str(error)) from error
