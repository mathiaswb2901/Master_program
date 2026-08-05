"""In-process fake host backend (``WORKBENCH_OFFICE_FAKE=1``).

The counterpart of ``services/fake_agent.py``, one layer down: a
:class:`~workbench_server.services.office_host.backend.HostBackend` that walks
the same lifecycle deterministically and **never touches a real process**. No
Office, no windows, no Rust — the pids it hands out are counters, not OS
processes, and nothing in this module starts, signals or kills anything.

It exists so every branch of the domain layer is reachable in CI: the happy path
and, chosen either programmatically (unit tests) or by the *name of the document
being opened* (the fake-agent trigger precedent, so a future E2E can drive a
failure from the UI without a special API):

* ``fail-launch``       -> :class:`LaunchFailedError`
* ``hang-launch``       -> :class:`LaunchTimeoutError`
* ``refuse-embed``      -> :class:`EmbedRefusedError`
* ``crash-after-embed`` -> embeds, then reports ``gone`` on the next poll
* ``already-open``      -> a handle marked ``adopted``: the fake found the
  document open in an instance it did not launch. Returned rather than raised,
  so what gets exercised is the *service's* own never-adopt rule.

Never enabled by default (``Settings.office_fake``), and ``main.py`` logs a
warning on startup when it is: a panel showing a document that is not really
open would be a worse lie than a panel that fails.
"""

import asyncio
import itertools
from pathlib import Path
from typing import Literal

import structlog

from workbench_server.models.office_host import HostAppKind, PanelRect
from workbench_server.services.office_host.backend import (
    EmbedRefusedError,
    HostHandle,
    HostLiveness,
    LaunchFailedError,
    LaunchTimeoutError,
)

log = structlog.get_logger()

#: What the fake can be told to do instead of working.
FakeFailure = Literal[
    "launch_failed",
    "launch_timeout",
    "embed_refused",
    "crash_after_embed",
    "already_open",
]

#: Filename fragment -> failure, matched case-insensitively on the whole path.
FAILURE_TRIGGERS: dict[str, FakeFailure] = {
    "fail-launch": "launch_failed",
    "hang-launch": "launch_timeout",
    "refuse-embed": "embed_refused",
    "crash-after-embed": "crash_after_embed",
    "already-open": "already_open",
}

#: Fake pids start well above the ones a test machine hands out, so a number
#: that leaks into a log is obviously not a process anybody can kill.
FIRST_FAKE_PID = 900_001
FIRST_FAKE_WINDOW = 700_001


def failure_for(path: Path) -> FakeFailure | None:
    """Which branch this document's name asks for, if any."""
    text = str(path).lower().replace("\\", "/")
    for trigger, failure in FAILURE_TRIGGERS.items():
        if trigger in text:
            return failure
    return None


class FakeHostBackend:
    """A scripted backend. Satisfies the ``HostBackend`` protocol."""

    def __init__(self, *, failure: FakeFailure | None = None) -> None:
        #: Forces one branch regardless of the document name (unit tests).
        self.failure = failure
        #: Every call, in order, for assertions: ``("launch", "report.docx")``.
        self.calls: list[tuple[str, str]] = []
        #: Held by a test that needs to act *while* an embed is in flight.
        self.embed_gate: asyncio.Event | None = None
        self._pids = itertools.count(FIRST_FAKE_PID)
        self._windows = itertools.count(FIRST_FAKE_WINDOW)
        self._alive: dict[int, bool] = {}
        # The branch chosen at launch, kept per pid: the document name is not an
        # argument to embed() or poll(), and a real backend has the same
        # amnesia — whatever it knows after launch, it knows from the handle.
        self._branches: dict[int, FakeFailure | None] = {}

    async def launch(self, path: Path, kind: HostAppKind) -> HostHandle:
        self.calls.append(("launch", path.name))
        branch = self.failure or failure_for(path)
        if branch == "launch_failed":
            raise LaunchFailedError(f"fake backend could not start {kind}")
        if branch == "launch_timeout":
            raise LaunchTimeoutError(f"fake {kind} never produced a window")
        pid = next(self._pids)
        handle = HostHandle(
            pid=pid, window_id=next(self._windows), adopted=branch == "already_open"
        )
        self._alive[pid] = True
        self._branches[pid] = branch
        log.debug("office_host.fake_launch", path=str(path), kind=kind, pid=pid)
        return handle

    async def embed(self, handle: HostHandle, rect: PanelRect) -> None:
        self.calls.append(("embed", f"{rect.width}x{rect.height}"))
        if self.embed_gate is not None:
            await self.embed_gate.wait()
        branch = self._branches.get(handle.pid)
        if branch == "embed_refused":
            raise EmbedRefusedError("fake window refused to be reparented")
        if branch == "crash_after_embed":
            # Embedded, then the application dies. The service only learns this
            # from its next poll, exactly as with a real crash.
            self._alive[handle.pid] = False

    async def set_bounds(self, handle: HostHandle, rect: PanelRect) -> None:
        self.calls.append(("set_bounds", f"{rect.x},{rect.y} {rect.width}x{rect.height}"))

    async def detach(self, handle: HostHandle) -> None:
        self.calls.append(("detach", str(handle.pid)))

    async def close(self, handle: HostHandle) -> None:
        self.calls.append(("close", str(handle.pid)))
        self._alive[handle.pid] = False

    async def poll(self, handle: HostHandle) -> HostLiveness:
        return "alive" if self._alive.get(handle.pid, False) else "gone"

    def kill(self, pid: int) -> None:
        """Make the next poll report this instance gone — the fake equivalent of
        the user quitting Word from its own File menu."""
        self._alive[pid] = False
