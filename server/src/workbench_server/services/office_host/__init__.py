"""Hosting real Office windows in Workbench panels.

One seam, either side of it:

* ``backend.py`` — the contract the native implementation must satisfy;
  ``state.py`` — the lifecycle; ``service.py`` — the hosts and their events.
* ``fake_backend.py`` — the in-process stand-in that makes every state reachable
  in CI, with no Office and no windows.
* ``shell_backend.py`` — the real one: ``office_com.py`` starts and reaps a
  private Word/Excel, ``shell_channel.py`` asks the desktop shell to dock its
  window, because ``SetParent`` cannot be called from here.
"""

from workbench_server.services.office_host.service import (
    HostNotFoundError,
    HostRefusedError,
    HostStateError,
    OfficeHostService,
    build_backend,
    build_bridge,
)
from workbench_server.services.office_host.shell_channel import ShellChannel

__all__ = [
    "HostNotFoundError",
    "HostRefusedError",
    "HostStateError",
    "OfficeHostService",
    "ShellChannel",
    "build_backend",
    "build_bridge",
]
