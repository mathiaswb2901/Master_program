"""Hosting real Office documents in Workbench panels — the domain layer.

Owns every host by id, drives the backend through the lifecycle in
``state.py``, publishes each change as an
:class:`~workbench_server.models.office_host.OfficeHostEvent` on the existing
event bus (so it rides ``/ws/events`` with no new plumbing), and reaps
everything on shutdown.

**Ownership: never adopt a process we did not launch.** Reparenting is a
destructive act — the window moves into our panel and its chrome is restyled —
so doing it to an instance the *user* started would hijack their session, and
closing it later would close their work. The rule is enforced twice: a backend
that admits it found the instance (``HostHandle.adopted``) is refused outright,
and the pid it did launch is bound to the host for life, so no later handle can
be substituted (:class:`~...state.ForeignProcessError`). A document already open
elsewhere is a first-class refusal with a reason the UI can show, never a silent
takeover.

**Policy.** ``office_native`` gates hosting entirely, and ``auto`` currently
resolves to *not* hosting natively: the Win32 backend does not ship yet, and it
becomes the default only once hang isolation is proven (owner decision,
2026-08-05). PowerPoint is refused whatever the mode — it is single-instance and
offers no window handle to prove ownership with, so preview is the honest answer
in v1. Nothing here touches the OnlyOffice path, which stays exactly as it was.
"""

import asyncio
import contextlib
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from workbench_server.models.office_host import (
    HOSTABLE_KINDS,
    HostReason,
    HostState,
    OfficeCapabilities,
    OfficeHostEvent,
    OfficeHostInfo,
    OfficeHostList,
    OfficeNativeMode,
    PanelRect,
    host_app_kind,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.office_host.backend import (
    HostBackend,
    HostBackendError,
    HostHandle,
)
from workbench_server.services.office_host.fake_backend import FakeHostBackend
from workbench_server.services.office_host.state import ForeignProcessError, HostLifecycle
from workbench_server.services.workspace import Workspace

log = structlog.get_logger()

#: How often live hosts are checked for liveness. Polling is the only crash
#: signal there is: nothing calls us back when Word disappears.
POLL_INTERVAL_S = 2.0

#: Cap on remembered hosts. Terminal records are kept — they are the answer to
#: "what happened to that document" and the reconnect path reads them — but a
#: long session must not grow the map without limit, so the oldest settled ones
#: are dropped first.
MAX_HOSTS = 32

#: Where a window goes when the caller has not laid the panel out yet. The first
#: real bounds replace it; a window has to be given *some* rectangle.
DEFAULT_RECT = PanelRect(x=0, y=0, width=800, height=600)

_WORD_EXE_GLOBS = (
    "Microsoft Office/root/Office*/WINWORD.EXE",
    "Microsoft Office/Office*/WINWORD.EXE",
)


class HostRefusedError(Exception):
    """The request will not be served, and no host was created.

    Carries the :data:`~workbench_server.models.office_host.HostReason` so the
    router maps it to a status code without knowing the policy.
    """

    def __init__(self, reason: HostReason, message: str) -> None:
        super().__init__(message)
        self.reason: HostReason = reason


class HostNotFoundError(Exception):
    """No host with that id (or it was pruned)."""


class HostStateError(Exception):
    """The host is not in a state where this operation means anything."""

    def __init__(self, host_id: str, state: HostState, action: str) -> None:
        super().__init__(f"cannot {action} a host that is {state}")
        self.host_id = host_id
        self.state = state


def program_files_roots() -> list[Path]:
    """Where Office installs itself. ``os.environ`` upper-cases its keys on
    Windows, which is the only platform these variables exist on."""
    return [
        Path(value)
        for value in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"))
        if value
    ]


def detect_office(roots: Sequence[Path] | None = None) -> bool:
    """Best-effort "is Microsoft Office installed on this machine".

    Deliberately a filesystem probe and nothing else: no new dependency, no COM,
    no registry walk. It informs the capabilities report — it never gates a
    launch, so a false negative costs a line of text and not a feature.
    """
    if roots is None:
        if sys.platform != "win32":
            return False
        roots = program_files_roots()
    for root in roots:
        for pattern in _WORD_EXE_GLOBS:
            with contextlib.suppress(OSError):
                if any(root.glob(pattern)):
                    return True
    return False


def build_backend(mode: OfficeNativeMode, fake: bool) -> HostBackend | None:
    """The one place a backend is chosen. ``None`` means "cannot host here".

    The Win32/COM implementation lands behind this call in a later PR; until
    then ``on`` without the fake backend is honestly reported as unavailable
    rather than pretending.
    """
    if mode == "off":
        return None
    if fake:
        return FakeHostBackend()
    return None


@dataclass
class _Host:
    lifecycle: HostLifecycle
    #: Set once the launch returns, and only for a process we started.
    handle: HostHandle | None = None
    rect: PanelRect | None = None
    #: ``backend.close`` has been called for this handle. Guards the two paths
    #: that both want to reap it: an explicit close, and the drive coroutine
    #: finding the host already terminal when its await returns.
    released: bool = False
    #: Reserved for the panel PR: the dockview panel currently showing it.
    panel_id: str | None = field(default=None)


class OfficeHostService:
    """Hosts by id, the legal transitions between their states, and the events."""

    def __init__(
        self,
        workspace: Workspace,
        bus: EventBus,
        backend: HostBackend | None,
        *,
        mode: OfficeNativeMode = "auto",
        fake: bool = False,
        detector: Callable[[], bool] = detect_office,
        clock: Callable[[], float] = time.time,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._workspace = workspace
        self._bus = bus
        self._mode: OfficeNativeMode = mode
        self._fake = fake
        # "off" wins over everything, including a backend somebody wired in.
        self._backend = None if mode == "off" else backend
        self._office_detected = detector()
        self._clock = clock
        self._poll_interval_s = poll_interval_s
        self._hosts: dict[str, _Host] = {}
        self._task: asyncio.Task[None] | None = None

    # ---- capabilities -------------------------------------------------------

    @property
    def hosting_available(self) -> bool:
        return self._backend is not None

    def capabilities(self, onlyoffice_enabled: bool) -> OfficeCapabilities:
        """What this machine can actually do, said plainly.

        ``onlyoffice_enabled`` comes from the existing ``OfficeService`` — the
        two halves of "how does a document open here" are reported together so
        the UI never has to combine them itself.
        """
        native = self.hosting_available
        return OfficeCapabilities(
            office_native=self._mode,
            native_hosting=native,
            office_detected=self._office_detected,
            fake_backend=self._fake and native,
            hostable_kinds=list(HOSTABLE_KINDS) if native else [],
            onlyoffice=onlyoffice_enabled,
            fallback="native" if native else ("onlyoffice" if onlyoffice_enabled else "preview"),
            detail=self._detail(onlyoffice_enabled),
        )

    def _detail(self, onlyoffice_enabled: bool) -> str:
        if self._mode == "off":
            return "native hosting is off (WORKBENCH_OFFICE_NATIVE=off)"
        if self.hosting_available and self._fake:
            return "the fake host backend is active: no real document is hosted"
        if self._mode == "auto":
            return (
                "auto: native hosting stays off until the window host ships and "
                "hang isolation is proven"
            )
        if not self.hosting_available:
            return "native hosting requested, but no host backend is available on this machine"
        if onlyoffice_enabled:
            return "native hosting available; OnlyOffice remains for preview and diff"
        return "native hosting available"

    # ---- opening ------------------------------------------------------------

    async def open(self, path: str, rect: PanelRect | None = None) -> OfficeHostInfo:
        """Host ``path``, or say why not.

        A live host for the same document is *reused* rather than duplicated —
        two panels reparenting one window is not a thing that can work — which
        makes this the re-embed path for a detached host too.
        """
        backend = self._require_backend()
        kind = host_app_kind(path)
        if kind is None:
            raise HostRefusedError("unsupported_file", f"{path} is not an Office document")
        if kind not in HOSTABLE_KINDS:
            raise HostRefusedError(
                "powerpoint_preview_only",
                "PowerPoint is preview-only in this version: it is single-instance and "
                "offers no way to prove a window is one we launched",
            )
        file_path = self._workspace.safe_path(path)
        if not file_path.is_file():
            raise FileNotFoundError(path)

        existing = self._live_host_for(path)
        if existing is not None:
            return await self._reuse(existing, backend, rect)

        host = _Host(HostLifecycle(f"host-{uuid.uuid4().hex[:8]}", path, kind, clock=self._clock))
        self._hosts[host.lifecycle.host_id] = host
        self._prune()
        self._publish(host)
        log.info("office_host.opening", host=host.lifecycle.host_id, path=path, kind=kind)
        return await self._drive(host, backend, file_path, rect)

    async def _drive(
        self, host: _Host, backend: HostBackend, file_path: Path, rect: PanelRect | None
    ) -> OfficeHostInfo:
        try:
            handle = await backend.launch(file_path, host.lifecycle.kind)
        except HostBackendError as error:
            log.warning(
                "office_host.launch_failed",
                host=host.lifecycle.host_id,
                reason=error.reason,
                detail=str(error),
            )
            return self._settle(host, "failed", error.reason)
        if handle.adopted:
            # The document is already open in an instance we did not start.
            # Refuse it: reparenting here would take over the user's own window,
            # and closing our panel would then close their work. Nothing is
            # closed on the way out — it was never ours to close.
            log.warning(
                "office_host.refused_foreign_process",
                host=host.lifecycle.host_id,
                path=host.lifecycle.path,
                pid=handle.pid,
            )
            return self._settle(host, "failed", "document_open_elsewhere")
        host.lifecycle.bind_pid(handle.pid)
        host.handle = handle
        if host.lifecycle.terminal:
            # Closed (or shut down) while the launch was in flight. The process
            # is ours, so it still has to be reaped.
            await self._release(host, backend)
            return host.lifecycle.info()
        host.lifecycle.to("embedding")
        self._publish(host)
        return await self._embed(host, backend, rect)

    async def _embed(
        self, host: _Host, backend: HostBackend, rect: PanelRect | None
    ) -> OfficeHostInfo:
        host.rect = embedded_at = rect or host.rect or DEFAULT_RECT
        try:
            handle = self._owned(host)
            await backend.embed(handle, embedded_at)
        except ForeignProcessError:
            return self._settle(host, "failed", "document_open_elsewhere")
        except HostBackendError as error:
            log.warning(
                "office_host.embed_failed",
                host=host.lifecycle.host_id,
                reason=error.reason,
                detail=str(error),
            )
            # We launched this instance, so a refused embed still leaves us
            # holding a process nobody can see. Reap it before settling.
            await self._release(host, backend)
            return self._settle(host, "failed", error.reason)
        if host.lifecycle.terminal:
            # Closed while embedding: the window is ours and now unwanted.
            await self._release(host, backend)
            return host.lifecycle.info()
        host.lifecycle.to("embedded")
        self._publish(host)
        log.info(
            "office_host.embedded",
            host=host.lifecycle.host_id,
            path=host.lifecycle.path,
            pid=host.lifecycle.pid,
        )
        if host.rect != embedded_at:
            # Bounds that arrived *during* the embed. They were stored rather
            # than sent (there was no embedded window to move yet), so send them
            # now — otherwise a panel resized while Word was starting would sit
            # at the rectangle it had a second ago.
            return await self.set_bounds(host.lifecycle.host_id, host.rect)
        return host.lifecycle.info()

    async def _reuse(
        self, host: _Host, backend: HostBackend, rect: PanelRect | None
    ) -> OfficeHostInfo:
        if host.lifecycle.state == "detached":
            host.lifecycle.to("embedding")
            self._publish(host)
            return await self._embed(host, backend, rect)
        if rect is not None:
            return await self.set_bounds(host.lifecycle.host_id, rect)
        return host.lifecycle.info()

    # ---- moving, releasing, closing -----------------------------------------

    async def set_bounds(self, host_id: str, rect: PanelRect) -> OfficeHostInfo:
        """The panel moved or resized. Accepted in any live state: bounds that
        arrive while the window is still being embedded are remembered and used
        by the embed itself, rather than lost to a race the UI cannot see."""
        host = self._require(host_id)
        if host.lifecycle.terminal:
            raise HostStateError(host_id, host.lifecycle.state, "move")
        host.rect = rect
        if host.lifecycle.state != "embedded":
            return host.lifecycle.info()
        return await self._guarded(
            host, "set_bounds", lambda backend, handle: backend.set_bounds(handle, rect)
        )

    async def detach(self, host_id: str) -> OfficeHostInfo:
        """Release the window back to the desktop, leaving the document open.

        The panel is gone but the user's document is not — which is why
        ``detached`` is a live state and not a terminal one.
        """
        host = self._require(host_id)
        if host.lifecycle.state != "embedded":
            raise HostStateError(host_id, host.lifecycle.state, "detach")
        info = await self._guarded(host, "detach", lambda backend, handle: backend.detach(handle))
        if host.lifecycle.terminal:
            return info
        host.lifecycle.to("detached")
        self._publish(host)
        return host.lifecycle.info()

    async def close(self, host_id: str) -> OfficeHostInfo:
        """Close the instance we launched. Idempotent: closing a settled host is
        the answer the UI already has, not a 409."""
        host = self._require(host_id)
        if host.lifecycle.terminal:
            return host.lifecycle.info()
        return await self._close(host, "user_closed")

    async def _close(self, host: _Host, reason: HostReason) -> OfficeHostInfo:
        # Terminal *first*, then the backend call: anything still in flight
        # (a launch, an embed) sees the terminal state when its await returns
        # and stops rather than embedding a window we have just given up.
        host.lifecycle.to("closed", reason=reason)
        self._publish(host)
        await self._release(host, self._backend)
        log.info("office_host.closed", host=host.lifecycle.host_id, reason=reason)
        return host.lifecycle.info()

    async def _guarded(
        self,
        host: _Host,
        action: str,
        call: Callable[[HostBackend, HostHandle], Awaitable[None]],
    ) -> OfficeHostInfo:
        """One backend call on a live host, with the ownership check and the
        failure handling every operation shares."""
        backend = self._backend
        if backend is None:  # pragma: no cover - a live host implies a backend
            raise HostRefusedError("native_hosting_disabled", "native hosting is not available")
        try:
            await call(backend, self._owned(host))
        except ForeignProcessError:
            log.warning("office_host.foreign_handle", host=host.lifecycle.host_id, action=action)
            return self._settle(host, "failed", "document_open_elsewhere")
        except HostBackendError as error:
            log.warning(
                "office_host.backend_call_failed",
                host=host.lifecycle.host_id,
                action=action,
                reason=error.reason,
            )
            await self._release(host, backend)
            return self._settle(host, "failed", error.reason)
        return host.lifecycle.info()

    async def _release(self, host: _Host, backend: HostBackend | None) -> None:
        """Close the instance behind this host, at most once."""
        if host.released or host.handle is None or backend is None:
            return
        host.released = True
        try:
            await backend.close(self._owned(host))
        except (HostBackendError, ForeignProcessError) as error:
            log.warning("office_host.close_failed", host=host.lifecycle.host_id, detail=str(error))

    def _owned(self, host: _Host) -> HostHandle:
        """The handle for this host, or :class:`ForeignProcessError`.

        The pid was bound at launch and never changes, so a backend cannot swap
        in a window we did not start — not even for an operation as innocent as
        a resize.
        """
        handle = host.handle
        if handle is None or handle.adopted or handle.pid != host.lifecycle.pid:
            raise ForeignProcessError(
                f"handle {handle} is not the process host {host.lifecycle.host_id} launched"
            )
        return handle

    # ---- liveness -----------------------------------------------------------

    async def poll_once(self) -> None:
        """One liveness sweep. Public so tests drive it without a clock."""
        backend = self._backend
        if backend is None:
            return
        for host in list(self._hosts.values()):
            # Only hosts whose window is ours and idle: a launch or an embed in
            # flight owns its handle, and its own result is the better signal.
            if host.lifecycle.state not in ("embedded", "detached") or host.handle is None:
                continue
            if await backend.poll(host.handle) == "gone":
                # The process is already gone; there is nothing left to close.
                host.released = True
                log.warning(
                    "office_host.crashed",
                    host=host.lifecycle.host_id,
                    path=host.lifecycle.path,
                    pid=host.lifecycle.pid,
                )
                self._settle(host, "crashed", "process_exited")

    # ---- reading ------------------------------------------------------------

    def snapshot(self) -> OfficeHostList:
        """Every host, live and settled — the reconnect and replay path."""
        return OfficeHostList(hosts=[host.lifecycle.info() for host in self._hosts.values()])

    def get(self, host_id: str) -> OfficeHostInfo:
        return self._require(host_id).lifecycle.info()

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._backend is None:
            return
        self._task = asyncio.create_task(self._run(), name="office-host-poll")

    async def shutdown(self) -> None:
        """Reap everything. A hosted window outliving the server would be an
        Office instance nobody owns, with our panel's chrome still applied."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for host in list(self._hosts.values()):
            if not host.lifecycle.terminal:
                await self._close(host, "server_shutdown")

    async def _run(self) -> None:
        log.info("office_host.poll_started", interval_s=self._poll_interval_s)
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.poll_once()
            except Exception:  # a dead task would stop noticing crashes, silently
                log.exception("office_host.poll_failed")

    # ---- internals ----------------------------------------------------------

    def _require_backend(self) -> HostBackend:
        if self._backend is None:
            raise HostRefusedError(
                "native_hosting_disabled", self._detail(onlyoffice_enabled=False)
            )
        return self._backend

    def _require(self, host_id: str) -> _Host:
        host = self._hosts.get(host_id)
        if host is None:
            raise HostNotFoundError(host_id)
        return host

    def _live_host_for(self, path: str) -> _Host | None:
        for host in self._hosts.values():
            if host.lifecycle.path == path and not host.lifecycle.terminal:
                return host
        return None

    def _settle(self, host: _Host, state: HostState, reason: HostReason) -> OfficeHostInfo:
        """Move to a terminal state, unless something already did."""
        if host.lifecycle.terminal:
            return host.lifecycle.info()
        host.lifecycle.to(state, reason=reason)
        self._publish(host)
        return host.lifecycle.info()

    def _publish(self, host: _Host) -> None:
        self._bus.publish(OfficeHostEvent(host=host.lifecycle.info()))

    def _prune(self) -> None:
        """Keep the map bounded, dropping settled hosts oldest-first. A live
        host is never pruned — it names a window somebody still has to close."""
        while len(self._hosts) > MAX_HOSTS:
            victim = next(
                (host_id for host_id, host in self._hosts.items() if host.lifecycle.terminal),
                None,
            )
            if victim is None:
                return
            del self._hosts[victim]
