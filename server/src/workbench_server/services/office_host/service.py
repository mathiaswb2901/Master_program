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

**Policy.** ``office_native`` gates hosting entirely. ``auto`` now resolves to
hosting natively *where it is possible*: the containment the owner made it
conditional on is built and measured (``host::mover`` in the shell — a hung
guest costs a resize frame ~1.5 ms instead of ~1 s), so the remaining conditions
are ones the machine answers, not ones a reviewer has to. Windows, an Office to
launch, and a desktop shell attached to the host channel: any of them missing is
reported by ``capabilities`` and the UI falls back to OnlyOffice.

**PowerPoint is hosted conditionally, and the condition is reported before it is
hit.** It is genuinely single-instance — its COM class factory is multi-use, so a
launch made while one is running returns the *user's* PowerPoint rather than a
private one (measured; see ``office_com``) — and a process we did not start must
never reach the job object that reaps ours. So the answer is neither "host it
anyway" nor "preview-only forever": host it when Workbench started the process,
refuse when it did not, and say which of those is true right now in
``capabilities().kind_notes`` so the UI degrades to preview from a fact rather
than from a failed launch. Nothing here touches the OnlyOffice path, which stays
exactly as it was.

**And the instance most often in the way is our own.** Hosting one deck consumes
the single process, so the second deck cannot dock either — the "one deck at a
time" limitation, which is a *consequence* of single-instance hosting rather than
a separate rule. It has to be answered from :attr:`_hosts` and not from a window
probe, because docking reparents the frame into a child window and a top-level
``EnumWindows`` walk stops being able to see it (``office_com``'s probe is
deliberately window-based so it can answer a REST request off the apartment
thread). Whose PowerPoint it is changes the only action the user can take —
"close that tab" versus "close PowerPoint" — so the two are different reasons
(``powerpoint_hosted_here`` / ``powerpoint_already_running``) and different
sentences, never one message that is right half the time.
"""

import asyncio
import contextlib
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import structlog

from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    LiveWorkbookStatus,
    SlideText,
    WordEdit,
    WordParagraphStyle,
    WordText,
    WordWriteOp,
)
from workbench_server.models.office_host import (
    HOSTABLE_KINDS,
    SINGLE_INSTANCE_KINDS,
    HostAppKind,
    HostReason,
    HostState,
    OfficeCapabilities,
    OfficeHostEvent,
    OfficeHostInfo,
    OfficeHostList,
    OfficeIdentity,
    OfficeNativeMode,
    PanelRect,
    host_app_kind,
)
from workbench_server.models.settings import SettingSource
from workbench_server.services.event_bus import EventBus
from workbench_server.services.office_host.backend import (
    HostBackend,
    HostBackendError,
    HostHandle,
)
from workbench_server.services.office_host.document_bridge import (
    DocNotHostedError,
    DocNotReadableError,
    DocumentBridge,
    RangeInvalidError,
)
from workbench_server.services.office_host.fake_backend import FakeHostBackend
from workbench_server.services.office_host.fake_document_bridge import FakeDocumentBridge
from workbench_server.services.office_host.identity import fake_identity, probe_identity
from workbench_server.services.office_host.office_com import application_window_exists
from workbench_server.services.office_host.real_document_bridge import ShellDocumentBridge
from workbench_server.services.office_host.shell_backend import ShellHostBackend
from workbench_server.services.office_host.shell_channel import ShellChannel
from workbench_server.services.office_host.state import ForeignProcessError, HostLifecycle
from workbench_server.services.workspace import Workspace

log = structlog.get_logger()

T = TypeVar("T")

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

#: How "off" is explained, by where the "off" actually came from — the second
#: half of the **Policy** note above, and the reason ``mode`` no longer arrives
#: alone. Since M7 V8 ``office_native`` is a setting as well as an environment
#: variable, so an operator's ``WORKBENCH_OFFICE_NATIVE`` and the user's own
#: answer in the Settings panel both reach this service as ``mode="off"``. This
#: sentence is what the Setup panel's Office row prints verbatim
#: (``services/setup.py``), so hard-coding the variable would tell someone who
#: never exported one to go and unset it instead of naming the switch they
#: flipped. Each knob naming the mechanism by which it really takes effect is the
#: whole point of the settings surface; a control that lies about its own
#: mechanism is worse than one that says nothing.
_OFF_DETAIL: Mapping[SettingSource, str] = {
    "environment": "native hosting is off (WORKBENCH_OFFICE_NATIVE=off)",
    "external": "native hosting is off (set outside the app for this launch)",
    "stored": "native hosting is off (turned off in Settings)",
}

#: Ceiling on one backend call. Launching Word is the slow one — about a second
#: normally, far worse on a cold machine — so it gets its own; everything else
#: is a window-manager call that either lands promptly or is not going to.
#:
#: A backstop, not the first line of defence: a backend is expected to time its
#: own work out and raise (:class:`~...backend.LaunchTimeoutError`). It exists
#: because a backend that forgets would otherwise hang the request coroutine
#: forever — and, through it, the lifespan shutdown that would have reaped the
#: window. Generous on purpose: this must never fire before the backend's own.
LAUNCH_TIMEOUT_S = 120.0
OPERATION_TIMEOUT_S = 30.0

_WORD_EXE_GLOBS = (
    "Microsoft Office/root/Office*/WINWORD.EXE",
    "Microsoft Office/Office*/WINWORD.EXE",
)

#: The application's own name, for the sentences ``kind_notes`` puts on screen.
#: :data:`~...office_host.HostAppKind` is a wire token — a user reads "PowerPoint",
#: not "powerpoint".
_APP_NAMES: dict[HostAppKind, str] = {
    "word": "Word",
    "excel": "Excel",
    "powerpoint": "PowerPoint",
}


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
        # `if`/`else` rather than an early return: mypy exempts a branch guarded
        # by `sys.platform` from `warn_unreachable`, but flags the tail an early
        # return leaves unreachable — and the 3-OS matrix type-checks this file
        # on linux and macos too.
        if sys.platform != "win32":
            return False
        else:
            roots = program_files_roots()
    for root in roots:
        for pattern in _WORD_EXE_GLOBS:
            with contextlib.suppress(OSError):
                if any(root.glob(pattern)):
                    return True
    return False


def build_backend(
    mode: OfficeNativeMode, fake: bool, channel: ShellChannel | None = None
) -> HostBackend | None:
    """The one place a backend is chosen. ``None`` means "cannot host here".

    ``auto`` is where the owner decision lives: it now resolves to the real
    backend, because hang isolation is proven (``host::mover`` on the Rust side,
    measured in ``hosting_tests::hang_isolation_measurement``). ``off`` still
    wins over everything. Off Windows there is nothing to host with, whatever
    the mode says — and that is reported, not raised.
    """
    if mode == "off":
        return None
    if fake:
        return FakeHostBackend()
    if channel is None:
        return None
    if sys.platform != "win32":  # nothing to host with — reported, not raised
        return None
    else:
        return ShellHostBackend(channel)


def build_bridge(
    mode: OfficeNativeMode, fake: bool, backend: HostBackend | None
) -> DocumentBridge | None:
    """The one place a document reader is chosen, sibling of :func:`build_backend`.

    ``None`` means "cannot read a hosted document here", which is not an error:
    the ``office_read`` tool says so and names how to open one. The fake shares
    the fake host backend so a read is answered for exactly the pids it launched;
    the real :class:`~...real_document_bridge.ShellDocumentBridge` shares the real
    one the same way, reading and writing the live COM document through the
    instance that backend is holding. A machine with no shell backend (a browser
    tab, or off Windows) has no live document to read, so it stays ``None`` and
    the tool degrades honestly.
    """
    if mode == "off":
        return None
    if fake and isinstance(backend, FakeHostBackend):
        return FakeDocumentBridge(backend)
    if isinstance(backend, ShellHostBackend):
        return ShellDocumentBridge(backend)
    return None


@dataclass
class _Host:
    lifecycle: HostLifecycle
    #: Set once the launch returns, and only for a process we started.
    handle: HostHandle | None = None
    #: Where the window belongs, and the single source of truth for it from the
    #: moment the host exists. Bounds that arrive while the launch or the embed
    #: is still in flight are written here rather than sent, and the embed reads
    #: *this* — never the rectangle the original open call happened to carry,
    #: which is a second, staler copy of the same answer.
    rect: PanelRect | None = None
    #: A ``backend.close`` for this handle is in flight or has succeeded. Guards
    #: the two paths that both want to reap it: an explicit close, and the drive
    #: coroutine finding the host already terminal when its await returns. Given
    #: back when a close *fails*, because a refused close reaped nothing.
    released: bool = False
    #: Whether the panel showing it is on screen. A hosted window is a real
    #: window and does not hide itself when its editor tab goes behind another
    #: one; like ``rect``, this is written whatever the state and read by the
    #: embed, so a tab switched away from during the launch does not come back
    #: as a Word over somebody else's document.
    visible: bool = True
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
        bridge: DocumentBridge | None = None,
        mode: OfficeNativeMode = "auto",
        mode_source: SettingSource = "stored",
        fake: bool = False,
        channel: ShellChannel | None = None,
        detector: Callable[[], bool] = detect_office,
        running_probe: Callable[[HostAppKind], bool] = application_window_exists,
        clock: Callable[[], float] = time.time,
        poll_interval_s: float = POLL_INTERVAL_S,
        launch_timeout_s: float = LAUNCH_TIMEOUT_S,
        operation_timeout_s: float = OPERATION_TIMEOUT_S,
    ) -> None:
        self._workspace = workspace
        self._bus = bus
        self._mode: OfficeNativeMode = mode
        #: Which mechanism decided that mode, so ``off`` can name the right one
        #: (:data:`_OFF_DETAIL`). ``create_app`` asks the settings service; the
        #: default is the user's own stored answer because that is the ordinary
        #: way it is off now, and it is the one wording that asserts no variable.
        self._mode_source: SettingSource = mode_source
        self._fake = fake
        #: Only for reporting whether a shell is attached. The backend is what
        #: actually uses it; the service holds it so ``capabilities`` can say
        #: *why* hosting is unavailable instead of only that it is.
        self._channel = channel
        # "off" wins over everything, including a backend somebody wired in.
        self._backend = None if mode == "off" else backend
        #: The read seam onto the live document. ``None`` where a document cannot
        #: be read (off, or a real backend before PR 2 ships the COM reader); the
        #: ``office_read`` tool reports that plainly rather than failing opaquely.
        self._bridge = None if mode == "off" else bridge
        self._office_detected = detector()
        #: "Is one of these already running?", for the single-instance kinds.
        #: Injected like ``detector`` so tests drive it without a real window —
        #: and called per ``capabilities()`` rather than cached, because the user
        #: can open PowerPoint at any moment and a cached "no" would send them to
        #: a launch that is going to be refused.
        self._running_probe = running_probe
        self._clock = clock
        self._poll_interval_s = poll_interval_s
        self._launch_timeout_s = launch_timeout_s
        self._operation_timeout_s = operation_timeout_s
        self._hosts: dict[str, _Host] = {}
        self._task: asyncio.Task[None] | None = None

    # ---- capabilities -------------------------------------------------------

    @property
    def hosting_available(self) -> bool:
        """A backend exists *and* it can host right now. The second half is not
        a formality: the real backend needs the desktop shell attached, and the
        same server serves a browser tab that can never host anything."""
        return self._backend is not None and self._backend.ready()

    def capabilities(self, onlyoffice_enabled: bool) -> OfficeCapabilities:
        """What this machine can actually do, said plainly.

        ``onlyoffice_enabled`` comes from the existing ``OfficeService`` — the
        two halves of "how does a document open here" are reported together so
        the UI never has to combine them itself.
        """
        native = self.hosting_available
        notes = self._kind_notes() if native else {}
        return OfficeCapabilities(
            office_native=self._mode,
            native_hosting=native,
            office_detected=self._office_detected,
            fake_backend=self._fake and native,
            shell_attached=self._shell_attached,
            hostable_kinds=[kind for kind in HOSTABLE_KINDS if kind not in notes] if native else [],
            kind_notes=notes,
            onlyoffice=onlyoffice_enabled,
            fallback="native" if native else ("onlyoffice" if onlyoffice_enabled else "preview"),
            detail=self._detail(onlyoffice_enabled),
        )

    def _kind_notes(self) -> dict[HostAppKind, str]:
        """Why a supported kind cannot be hosted *at this moment*, and by whom.

        Only the single-instance kinds can produce one, and there are two ways
        for the one process to be taken.

        **Workbench is already hosting one** — asked first, and asked of
        :attr:`_hosts` rather than of a probe. This is the common case (open a
        second deck) *and* the one a window probe structurally cannot answer:
        docking reparents the frame into a child window, so the top-level
        ``EnumWindows`` walk behind ``running_probe`` stops seeing it the moment
        the embed lands. Reading our own map is also exact rather than advisory —
        a host is live because we launched it — and it holds from ``launching``
        onwards, so two decks opened in the same second cannot both be told yes.
        The sentence names the document, because the window to close is a
        Workbench tab and possibly behind another one.

        **The user has one open outside Workbench** — the window probe, which is
        advisory in both directions: it cannot see an instance running without a
        window (an ``/AUTOMATION`` PowerPoint some other tool is driving), and
        the user can start one a second after the answer is sent. The launch
        pre-flight is the exact question and the ownership check behind it is the
        guarantee; this exists so the common case degrades to preview without a
        failed launch first.

        The fake short-circuits the *probe* only: it starts no processes, so
        under ``WORKBENCH_OFFICE_FAKE`` there is no machine-wide instance to
        collide with and CI stays deterministic with no Office and no window
        station. Our own hosts are not a probe — the fake creates them exactly as
        the real backend does — so the first branch applies there too, which is
        what lets the E2E suite walk the second-deck journey a user actually
        hits.
        """
        notes: dict[HostAppKind, str] = {}
        for kind in HOSTABLE_KINDS:
            if kind not in SINGLE_INSTANCE_KINDS:
                continue
            app = _APP_NAMES[kind]
            hosted = self._hosted_path_for(kind)
            if hosted is not None:
                notes[kind] = (
                    f"Workbench is already showing {hosted} in {app}, which runs one instance "
                    "for the whole session. Close that tab to dock this file; until then it "
                    "opens as a preview."
                )
            elif not self._fake and self._running_probe(kind):
                notes[kind] = (
                    f"{app} is already running outside Workbench. It runs one instance for "
                    "the whole session, so Workbench cannot start its own — close it and "
                    "reopen this file to dock it."
                )
        return notes

    def _hosted_path_for(self, kind: HostAppKind, *, other_than: str | None = None) -> str | None:
        """The document this application is already holding for us, if any.

        ``other_than`` excludes one host by id: the host being launched is in the
        map (and non-terminal) before its own launch returns, so a refusal has to
        ask about *the others* or it would always find itself.
        """
        return next(
            (
                host.lifecycle.path
                for host_id, host in self._hosts.items()
                if host.lifecycle.kind == kind
                and not host.lifecycle.terminal
                and host_id != other_than
            ),
            None,
        )

    def _whose_instance(self, host: _Host, reason: HostReason) -> HostReason:
        """Sharpen "the application is already running" into *whose* it is.

        The backend knows only that one is up — the pre-flight it raises from is
        a COM question about the machine, not about Workbench. The service knows
        the other half: whether the instance in the way is the one it is hosting
        itself. That changes the only action the user can take, so it changes the
        reason rather than being left to a card that says "close PowerPoint"
        about a window docked in a panel behind another tab.

        Reached on the race the capabilities report cannot close: ``kind_notes``
        is read when the panel mounts, and a deck docked in the moment between
        that read and this launch still lands here.
        """
        if reason != "powerpoint_already_running":
            return reason
        if self._hosted_path_for(host.lifecycle.kind, other_than=host.lifecycle.host_id) is None:
            return reason
        return "powerpoint_hosted_here"

    async def identity(self) -> OfficeIdentity:
        """Which Microsoft account this machine's Office is signed in as.

        The same fake/real split as the host backend: a deterministic synthetic
        account under ``WORKBENCH_OFFICE_FAKE`` (so CI is green with no Office),
        and a best-effort registry read otherwise — pushed off the event loop
        because a hosting request must never wait on it, and degrading to
        ``unknown`` rather than raising if the read cannot be made.
        """
        if self._fake:
            return fake_identity()
        return await asyncio.to_thread(probe_identity)

    @property
    def _shell_attached(self) -> bool:
        return self._channel is not None and self._channel.attached

    def _detail(self, onlyoffice_enabled: bool) -> str:
        if self._mode == "off":
            return _OFF_DETAIL[self._mode_source]
        if self.hosting_available and self._fake:
            return "the fake host backend is active: no real document is hosted"
        if self._backend is None:
            if not self._office_detected:
                return "no Microsoft Office was found on this machine"
            return "native hosting is not available here (it needs the Workbench desktop shell)"
        if not self.hosting_available:
            return (
                "native hosting is ready, but this window is a browser tab — "
                "only the Workbench desktop shell can dock a real document"
            )
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
        if kind not in HOSTABLE_KINDS:  # pragma: no cover - every kind is hostable today
            raise HostRefusedError(
                "unsupported_file", f"{kind} documents cannot be docked in this version"
            )
        file_path = self._workspace.safe_path(path)
        if not file_path.is_file():
            raise FileNotFoundError(path)

        existing = self._live_host_for(path)
        if existing is not None:
            return await self._reuse(existing, backend, rect)

        host = _Host(HostLifecycle(f"host-{uuid.uuid4().hex[:8]}", path, kind, clock=self._clock))
        # Written before anything is awaited, so a resize that lands during the
        # launch has somewhere to go that the embed will actually read.
        host.rect = rect
        self._hosts[host.lifecycle.host_id] = host
        self._prune()
        self._publish(host)
        log.info("office_host.opening", host=host.lifecycle.host_id, path=path, kind=kind)
        return await self._drive(host, backend, file_path)

    async def _drive(self, host: _Host, backend: HostBackend, file_path: Path) -> OfficeHostInfo:
        try:
            handle = await asyncio.wait_for(
                backend.launch(file_path, host.lifecycle.kind, host.lifecycle.host_id),
                self._launch_timeout_s,
            )
        except TimeoutError:
            # The backend's own timeout should have fired long before ours. It
            # did not, so nothing is coming back — and the launch is cancelled
            # rather than left running against a host nobody will ever settle.
            log.warning(
                "office_host.launch_timed_out",
                host=host.lifecycle.host_id,
                path=host.lifecycle.path,
                timeout_s=self._launch_timeout_s,
            )
            return self._settle(host, "failed", "launch_timeout")
        except HostBackendError as error:
            reason = self._whose_instance(host, error.reason)
            log.warning(
                "office_host.launch_failed",
                host=host.lifecycle.host_id,
                reason=reason,
                detail=str(error),
            )
            return self._settle(host, "failed", reason)
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
        return await self._embed(host, backend)

    async def _embed(self, host: _Host, backend: HostBackend) -> OfficeHostInfo:
        # ``host.rect`` only — never the rectangle the open call carried. That
        # one was written here before the launch started, and a resize arriving
        # during the ~1s launch has overwritten it since; reading the parameter
        # instead would put the window back where the panel no longer is.
        host.rect = embedded_at = host.rect or DEFAULT_RECT
        try:
            handle = self._owned(host)
            await asyncio.wait_for(backend.embed(handle, embedded_at), self._operation_timeout_s)
        except ForeignProcessError:
            return self._settle(host, "failed", "document_open_elsewhere")
        except TimeoutError:
            log.warning(
                "office_host.embed_timed_out",
                host=host.lifecycle.host_id,
                timeout_s=self._operation_timeout_s,
            )
            # Launched, never embedded: the process is ours and invisible.
            await self._release(host, backend)
            return self._settle(host, "failed", "backend_timeout")
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
        info = host.lifecycle.info()
        if host.rect != embedded_at:
            # Bounds that arrived *during* the embed. They were stored rather
            # than sent (there was no embedded window to move yet), so send them
            # now — otherwise a panel resized while Word was starting would sit
            # at the rectangle it had a second ago.
            info = await self.set_bounds(host.lifecycle.host_id, host.rect)
        if not host.visible and not host.lifecycle.terminal:
            # Same story for the tab the user switched away from while Word was
            # starting: the window has just been shown, and nobody asked for it.
            info = await self._guarded(
                host, "set_visible", lambda backend, handle: backend.set_visible(handle, False)
            )
        return info

    async def _reuse(
        self, host: _Host, backend: HostBackend, rect: PanelRect | None
    ) -> OfficeHostInfo:
        if host.lifecycle.state == "detached":
            if rect is not None:
                host.rect = rect
            host.lifecycle.to("embedding")
            self._publish(host)
            return await self._embed(host, backend)
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

    async def set_visible(self, host_id: str, visible: bool) -> OfficeHostInfo:
        """The panel went behind another tab, or came back.

        Remembered whatever the state, and applied only when there is a window:
        a tab switched away from *during* the launch must still be hidden when
        the embed lands, or a real Word appears over whatever the user is
        looking at now. The embed reads this the same way it reads ``rect``.
        """
        host = self._require(host_id)
        if host.lifecycle.terminal:
            raise HostStateError(host_id, host.lifecycle.state, "show or hide")
        if host.visible == visible:
            return host.lifecycle.info()
        host.visible = visible
        if host.lifecycle.state != "embedded":
            return host.lifecycle.info()
        return await self._guarded(
            host, "set_visible", lambda backend, handle: backend.set_visible(handle, visible)
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
            await asyncio.wait_for(call(backend, self._owned(host)), self._operation_timeout_s)
        except ForeignProcessError:
            log.warning("office_host.foreign_handle", host=host.lifecycle.host_id, action=action)
            return self._settle(host, "failed", "document_open_elsewhere")
        except TimeoutError:
            log.warning(
                "office_host.backend_call_timed_out",
                host=host.lifecycle.host_id,
                action=action,
                timeout_s=self._operation_timeout_s,
            )
            await self._release(host, backend)
            return self._settle(host, "failed", "backend_timeout")
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
        """Close the instance behind this host: one attempt at a time, and a
        *failed* attempt does not count as one.

        ``released`` is set before the await because it guards the two paths
        that both want to reap a host (an explicit close, and a drive coroutine
        finding it already terminal). It is given back if the close comes back
        an error, because a refused close reaped nothing: a "Save changes?"
        modal eating ``WM_CLOSE`` leaves a real Word on the user's screen, and a
        host that says ``closed`` while that is true would be lying. The record
        carries ``close_failed`` until a later sweep gets through.
        """
        if host.released or host.handle is None or backend is None:
            return
        host.released = True
        try:
            handle = self._owned(host)
        except ForeignProcessError as error:
            # Never ours, so never ours to close, and nothing to retry.
            log.warning(
                "office_host.release_skipped", host=host.lifecycle.host_id, detail=str(error)
            )
            return
        try:
            await asyncio.wait_for(backend.close(handle), self._operation_timeout_s)
        except (HostBackendError, TimeoutError) as error:
            host.released = False
            log.warning(
                "office_host.close_failed",
                host=host.lifecycle.host_id,
                pid=host.lifecycle.pid,
                detail=f"{type(error).__name__}: {error}",
            )
            self._mark_close_failed(host, failed=True)
            return
        self._mark_close_failed(host, failed=False)

    def _mark_close_failed(self, host: _Host, *, failed: bool) -> None:
        """Publish only when the answer changes: the sweep re-asks every couple
        of seconds and a flag that has not moved is not news."""
        if host.lifecycle.close_failed == failed:
            return
        host.lifecycle.close_failed = failed
        self._publish(host)

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
            if host.lifecycle.terminal:
                await self._retry_close(host, backend)
                continue
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

    async def _retry_close(self, host: _Host, backend: HostBackend) -> None:
        """Ask again for a close that was refused.

        The host has settled and settling is final — but the instance behind it
        is ours until it is actually gone, and the usual reason a close fails is
        a modal the user dismisses a moment later. So every sweep re-asks, and
        stops as soon as the process is gone, however it went.
        """
        if not host.lifecycle.close_failed or host.handle is None:
            return
        if await backend.poll(host.handle) == "gone":
            host.released = True
            self._mark_close_failed(host, failed=False)
            return
        await self._release(host, backend)

    # ---- reading ------------------------------------------------------------

    def snapshot(self) -> OfficeHostList:
        """Every host, live and settled — the reconnect and replay path."""
        return OfficeHostList(hosts=[host.lifecycle.info() for host in self._hosts.values()])

    def get(self, host_id: str) -> OfficeHostInfo:
        return self._require(host_id).lifecycle.info()

    # ---- reading the live document (the COM bridge seam) ---------------------

    async def document_structure(self, path: str) -> DocStructure:
        """The shape of the live document at ``path``: Word paragraph count, or
        the Excel sheets and their used-range dimensions.

        Raises :class:`~...document_bridge.DocNotHostedError` when nothing is
        docked for the path, and the other
        :class:`~...document_bridge.DocumentBridgeError` refusals the read seam
        reports.
        """
        host, bridge, handle = self._live_document(path, "reading")
        return await self._guarded_op(bridge.structure(handle, host.lifecycle.kind))

    async def read_document(
        self,
        path: str,
        *,
        max_chars: int,
        max_cells: int,
        sheet: str | None = None,
        a1_range: str | None = None,
        start_paragraph: int = 0,
        start_slide: int = 1,
    ) -> WordText | CellWindow | SlideText:
        """Read a window of the live document at ``path``.

        Word documents are read by ``start_paragraph``/``max_chars``; Excel
        worksheets by ``sheet``/``a1_range``, trimmed to ``max_cells`` cells and
        ``max_chars`` of aggregate cell text so one long cell cannot overrun the
        window; PowerPoint decks by ``start_slide``/``max_chars``, whole slides at
        a time. Reading
        an Excel document without a ``sheet`` is a caller error here — the tool
        returns the structure instead — so it is refused rather than guessed.
        """
        host, bridge, handle = self._live_document(path, "reading")
        if host.lifecycle.kind == "word":
            return await self._guarded_op(bridge.read_word(handle, start_paragraph, max_chars))
        if host.lifecycle.kind == "powerpoint":
            return await self._guarded_op(bridge.read_powerpoint(handle, start_slide, max_chars))
        if sheet is None:
            raise DocNotReadableError("name a sheet to read from an Excel document")
        return await self._guarded_op(
            bridge.read_excel(handle, sheet, a1_range, max_cells, max_chars)
        )

    async def write_document(
        self,
        path: str,
        *,
        content: str,
        op: WordWriteOp = "replace",
        paragraph: int | None = None,
        after_paragraph: int | None = None,
        style: WordParagraphStyle | None = None,
        sheet: str | None = None,
        cell: str | None = None,
    ) -> WordEdit | CellEdit:
        """Apply one targeted edit to the live document at ``path``.

        Word documents take ``paragraph`` (zero-based) and ``content`` — the new
        text of that one paragraph; Excel worksheets take ``sheet`` and ``cell``
        (A1) and ``content`` — the new text of that one cell. The edit is scoped
        to the addressed target and applied to the live in-memory instance (the
        user can undo it), never a whole-file rewrite. Missing the target that
        the document's kind requires is a caller error, refused rather than
        guessed. Raises the same
        :class:`~...document_bridge.DocumentBridgeError` refusals the read seam
        reports (not docked, still opening, foreign window, gone, unavailable).

        ``op="insert"`` is the Word-only second shape: ``content`` becomes a
        **new** paragraph after ``after_paragraph`` (omitted = the end of the
        document, ``-1`` = before the first paragraph), carrying ``style`` as its
        intent. It is the one write that moves the addresses of the paragraphs
        below it, which is why the :class:`~....models.office_bridge.WordEdit` it
        returns names both the new index and the new total.
        """
        host, bridge, handle = self._live_document(path, "writing")
        if host.lifecycle.kind == "powerpoint":
            # Read-only by design, not by omission: a slide has no single
            # addressable text target the way a paragraph or a cell does (its
            # text lives in positional, typed shapes), so there is nothing here
            # to write *to* yet. Said plainly so an agent stops rather than
            # retrying with a different guess at the arguments.
            raise RangeInvalidError(
                "PowerPoint decks are read-only in Workbench: a slide has no single text "
                "target to address. Read it with office_read, and edit it in the panel."
            )
        if host.lifecycle.kind == "word":
            if op == "insert":
                return await self._guarded_op(
                    bridge.insert_word(handle, after_paragraph, content, style)
                )
            if paragraph is None:
                raise RangeInvalidError("name a paragraph to write in a Word document")
            return await self._guarded_op(bridge.write_word(handle, paragraph, content))
        if op == "insert":
            # Named rather than silently treated as a set: inserting a *row* is a
            # different operation with different hazards (formulas and named
            # ranges that reference what moved), and quietly writing a cell
            # instead would be an edit the agent did not ask for.
            raise RangeInvalidError(
                "insert is Word-only: an Excel write sets one addressed cell. "
                "Pass sheet and cell to set a value."
            )
        if sheet is None or cell is None:
            raise RangeInvalidError("name a sheet and a cell to write in an Excel document")
        return await self._guarded_op(bridge.write_excel(handle, sheet, cell, content))

    # ---- the live workbook seam (the reconciliation front gate) -------------
    #
    # Four narrow methods for one caller: `services/reconciliation.py`, which
    # must never reconcile the file on disk against a workbook this service is
    # holding open with unsaved edits in it. They are public and typed rather
    # than that module reaching for `_live_host_for`, so the check never imports
    # the office host at all — it declares the shape it needs and this satisfies
    # it, the same `SlotLocator` posture the gates check already uses.

    def docked_workbook(self, path: str) -> str | None:
        """The docked host's own path string for the workbook at ``path``, or
        ``None`` when Workbench is not holding that document open.

        Matched by **resolved absolute path**, not by string equality: the host
        was opened with whatever the file tree handed it and the reconciliation
        spec carries whatever the analyst typed, so ``models/se3.xlsx`` and
        ``models\\se3.xlsx`` have to name the same workbook or the gate would
        wave through the very case it exists for. The host's *own* string is
        what comes back, because that is the key every other method here takes.
        """
        target = self._resolved(path)
        if target is None:
            return None
        return next(
            (
                host.lifecycle.path
                for host in self._hosts.values()
                if not host.lifecycle.terminal and self._resolved(host.lifecycle.path) == target
            ),
            None,
        )

    def _resolved(self, path: str) -> Path | None:
        """A workspace-relative path as an absolute one, or ``None`` when it does
        not resolve inside the workspace. A path that cannot be resolved cannot
        match anything, which is the honest answer to "is this docked?"."""
        try:
            return self._workspace.safe_path(path)
        except Exception as error:
            log.debug("office_host.path_unresolved", path=path, error=str(error))
            return None

    async def workbook_status(self, path: str) -> LiveWorkbookStatus:
        """Whether the live workbook at ``path`` has unsaved edits, and whether
        Excel has finished calculating.

        The cheap question, asked first and on purpose: it is two property reads
        with no range in them, so it can still be answered when a bulk value read
        cannot. Raises the same
        :class:`~...document_bridge.DocumentBridgeError` refusals the other
        reads do — and a status nobody could read is a refusal the caller must
        treat as "assume nothing", never as "assume clean".
        """
        _, bridge, handle = self._live_document(path, "reading")
        return await self._guarded_op(bridge.workbook_status(handle))

    async def read_workbook_cells(
        self, path: str, sheet: str | None, cells: Sequence[str]
    ) -> list[object]:
        """The **typed** values of the named A1 cells of the live workbook.

        Numbers stay numbers and dates stay naive ``datetime``s — the difference
        between this and ``read_document`` is the whole reason the reconciliation
        gate cannot reuse the text window (``office_com.excel_range_values``).
        """
        _, bridge, handle = self._live_document(path, "reading")
        return await self._guarded_op(bridge.read_cells(handle, sheet, cells))

    async def read_workbook_columns(
        self,
        path: str,
        sheet: str | None,
        ts_column: str,
        value_column: str,
        start_row: int,
        max_rows: int,
    ) -> list[tuple[object, object]]:
        """Two whole columns of the live workbook as typed ``(timestamp, value)``
        pairs, bounded by ``max_rows``."""
        _, bridge, handle = self._live_document(path, "reading")
        return await self._guarded_op(
            bridge.read_columns(handle, sheet, ts_column, value_column, start_row, max_rows)
        )

    def _live_document(self, path: str, gerund: str) -> tuple[_Host, DocumentBridge, HostHandle]:
        """The host, its bridge and its owned handle — or the refusal that says
        why the live document cannot be reached, phrased with ``gerund``
        ("reading"/"writing"). One guard, so read and write refuse identically."""
        host = self._live_host_for(path)
        if host is None:
            verb = {"reading": "read", "writing": "write"}[gerund]
            raise DocNotHostedError(
                f"{path} is not docked in Workbench; open it first, then {verb} it"
            )
        if self._bridge is None:
            raise DocNotReadableError(self._bridge_unavailable_detail(gerund))
        if host.lifecycle.state not in ("embedded", "detached"):
            raise DocNotReadableError(
                f"{path} is still opening (it is {host.lifecycle.state}); "
                "try again once it is docked"
            )
        try:
            handle = self._owned(host)
        except ForeignProcessError as error:
            raise DocNotReadableError(
                f"the window for {path} is not the instance Workbench launched"
            ) from error
        return host, self._bridge, handle

    def _bridge_unavailable_detail(self, gerund: str) -> str:
        if self._fake:  # pragma: no cover - the fake always has a bridge
            role = "writer" if gerund == "writing" else "reader"
            return f"the fake host backend has no document {role}"
        return f"{gerund} the live document is not available here (it needs the desktop shell)"

    async def _guarded_op(self, call: Awaitable[T]) -> T:
        """One bridge call, under the service's per-call ceiling. A call that
        never returns — a Word thinking about a modal — must not hang the request
        that started it, so it is cancelled and reported rather than awaited
        forever."""
        try:
            return await asyncio.wait_for(call, self._operation_timeout_s)
        except TimeoutError as error:
            raise DocNotReadableError("the operation did not return in time") from error

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
        # Readiness, not just existence. A backend with no shell attached would
        # launch a real Word and then have nowhere to put it — a window on the
        # user's desktop that Workbench claims to be hosting.
        if self._backend is None or not self._backend.ready():
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
        host is never pruned — it names a window somebody still has to close —
        and neither is one whose close was refused until there is nothing else
        to drop, for the same reason: it is the only handle to that process."""
        while len(self._hosts) > MAX_HOSTS:
            victim = self._prunable(owing_a_close=False) or self._prunable(owing_a_close=True)
            if victim is None:
                return
            del self._hosts[victim]

    def _prunable(self, *, owing_a_close: bool) -> str | None:
        return next(
            (
                host_id
                for host_id, host in self._hosts.items()
                if host.lifecycle.terminal and host.lifecycle.close_failed is owing_a_close
            ),
            None,
        )
