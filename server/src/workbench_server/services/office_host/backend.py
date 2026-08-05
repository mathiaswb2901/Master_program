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


class HostBackend(Protocol):
    """What the native implementation must provide, and nothing more."""

    async def launch(self, path: Path, kind: HostAppKind) -> HostHandle:
        """Start ``kind`` on ``path`` and return a handle to *our* instance.

        Raises :class:`LaunchFailedError`, :class:`LaunchTimeoutError`, or
        :class:`DocumentOpenElsewhereError`.
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
