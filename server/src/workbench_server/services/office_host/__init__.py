"""Hosting real Office windows in Workbench panels.

Four modules, one seam: ``backend.py`` is the contract the native
implementation must satisfy, ``fake_backend.py`` is the in-process stand-in that
makes every state reachable in CI, ``state.py`` is the lifecycle, and
``service.py`` owns the hosts and publishes their events.
"""

from workbench_server.services.office_host.service import (
    HostNotFoundError,
    HostRefusedError,
    HostStateError,
    OfficeHostService,
    build_backend,
)

__all__ = [
    "HostNotFoundError",
    "HostRefusedError",
    "HostStateError",
    "OfficeHostService",
    "build_backend",
]
