"""The host backend contract.

:class:`HostBackend` is the seam the risky native work plugs into. Everything
above it — the state machine, the service, the REST surface, the events — is
plain Python that runs and is tested on a machine with no Microsoft Office and
no Rust. Below it there will eventually be exactly two implementations: the
in-process :mod:`~workbench_server.services.office_host.fake_backend`, and the
Win32/COM one that reparents a real Word window into the Tauri shell.

**The contract is deliberately small, and deliberately honest.** Every method
here is something a real ``SetParent``-based implementation can actually do.
There is no synchronous screenshot, no "read the document", no arbitrary
mutation: reading and writing the *live* document is COM work that arrives with
the bridge in a later PR, behind its own seam. Inventing capability here would
mean the domain layer passes its tests and the native implementation cannot be
written to match.

Every method is ``async`` because none of it is cheap: launching Word takes
about a second, and the real implementation will run the blocking Win32 calls in
a worker thread. Making that explicit in the Protocol keeps the service written
for the real cost from the start.

**Every method must come back.** An implementation is expected to bound its own
work and raise (:class:`LaunchTimeoutError` exists for exactly that), because it
is the only layer that knows what "too long" means for the call it is making.
The service does not trust that: it applies its own ceiling to every call and
cancels the coroutine when it runs out — otherwise one backend that forgets
hangs the request that started it, and with it the shutdown that would have
reaped the window. A backend must therefore treat cancellation as a real
outcome and leave nothing running behind it.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from workbench_server.models.office_host import HostAppKind, HostReason, PanelRect

#: What :meth:`HostBackend.poll` reports. ``gone`` means the process or its
#: window no longer exists — the application quit, or the user closed it from
#: its own File menu.
HostLiveness = Literal["alive", "gone"]


@dataclass(frozen=True)
class HostHandle:
    """One launched application instance.

    ``pid`` is the ownership proof. The service records it and refuses to bind
    anything else afterwards, so a backend that returns a different process
    later cannot make us reparent — or close — a window the user opened
    themselves.

    ``window_id`` is the top-level window (an ``HWND`` on Windows), opaque here.

    ``adopted`` is how a backend admits it did **not** create this process. The
    service refuses such a handle outright rather than take over a document the
    user already had open; see ``service.py``. A backend that lies about this
    field is the one thing this layer cannot defend against, which is precisely
    why the field exists instead of a heuristic.
    """

    pid: int
    window_id: int
    adopted: bool = False


class HostBackendError(Exception):
    """Base for the refusals a backend is allowed to report.

    Carries the :data:`~workbench_server.models.office_host.HostReason` the host
    settles with, so the service maps failures without a chain of ``isinstance``
    checks and a new failure mode cannot arrive without naming itself.
    """

    reason: HostReason = "launch_failed"


class LaunchTimeoutError(HostBackendError):
    """The application started but never produced a window we could host."""

    reason: HostReason = "launch_timeout"


class LaunchFailedError(HostBackendError):
    """The application could not be started at all."""

    reason: HostReason = "launch_failed"


class EmbedRefusedError(HostBackendError):
    """The window exists but would not be reparented into the panel."""

    reason: HostReason = "embed_refused"


class DocumentOpenElsewhereError(HostBackendError):
    """The document is already open in an instance we did not launch.

    A first-class refusal: the alternative is reparenting a window the user
    opened themselves, which is a silent takeover of their session.
    """

    reason: HostReason = "document_open_elsewhere"


class InstanceBusyError(HostBackendError):
    """The application is already running and only runs one of itself.

    PowerPoint's COM server is multi-use, so a launch made while one is running
    would bind to the user's own instance rather than start ours. Refused as its
    own reason because the fix is different from every other refusal here: close
    PowerPoint, not the document.
    """

    reason: HostReason = "powerpoint_already_running"


class HostBackend(Protocol):
    """What the native implementation must provide, and nothing more."""

    def ready(self) -> bool:
        """Can this backend host *right now*?

        Not the same question as "does this backend exist". The real one needs
        the desktop shell attached to have a window to host into, and a browser
        tab is not one — so "can I dock a document" has an answer that changes
        while the server runs, and the capabilities endpoint has to be able to
        ask it rather than infer it from a user agent.
        """
        ...

    async def launch(self, path: Path, kind: HostAppKind, host_id: str) -> HostHandle:
        """Start ``kind`` on ``path`` and return a handle to *our* instance.

        ``host_id`` is the service's id for this host, passed so the backend can
        name the same window the same way everywhere — in its own logs, and in
        whatever registry the native side keeps. It is not an ownership token:
        that is the pid in the returned handle.

        Raises :class:`LaunchFailedError`, :class:`LaunchTimeoutError`,
        :class:`DocumentOpenElsewhereError`, or :class:`InstanceBusyError`.
        """
        ...

    async def embed(self, handle: HostHandle, rect: PanelRect) -> None:
        """Reparent the handle's window into the panel at ``rect``.

        Raises :class:`EmbedRefusedError`.
        """
        ...

    async def set_bounds(self, handle: HostHandle, rect: PanelRect) -> None:
        """Move/resize an embedded window. Cheap, and called on every drag."""
        ...

    async def set_visible(self, handle: HostHandle, visible: bool) -> None:
        """Show or hide the hosted window without giving it back.

        A hosted window is a real window: it does not stop existing because the
        element describing it went ``display: none``. Switching to another
        editor tab has to say so, or a real Word stays painted over the panel
        that replaced it.
        """
        ...

    async def detach(self, handle: HostHandle) -> None:
        """Release the window back to the desktop, leaving the document open."""
        ...

    async def close(self, handle: HostHandle) -> None:
        """Close the instance we launched. Must be safe to call twice."""
        ...

    async def poll(self, handle: HostHandle) -> HostLiveness:
        """Is this instance still there? The only crash signal there is —
        nothing calls us back when Word disappears."""
        ...
