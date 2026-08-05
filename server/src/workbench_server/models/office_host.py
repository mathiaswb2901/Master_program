"""Office host schemas: a *real* Word/Excel window docked into a Workbench panel.

This is the wire vocabulary for the lifecycle of one hosted application
instance. The domain is deliberately modelled in full before any native code
exists (see ``services/office_host/``): every state below is reachable, and
testable in CI, against the in-process fake backend on a machine with no
Microsoft Office installed.

Two owner decisions are encoded in the types themselves rather than left to a
comment somewhere:

* ``powerpoint`` is a legal :data:`HostAppKind` — the file type is real and the
  UI must be able to name it — but it is **not** in :data:`HOSTABLE_KINDS`.
  PowerPoint is single-instance and exposes no ``Application.Hwnd`` we could use
  to prove the window we found is one we started, so hosting it risks
  reparenting the user's own open presentation. It stays preview-only in v1 and
  the service refuses it with :data:`HostReason` ``powerpoint_preview_only``.
* ``document_open_elsewhere`` exists as a first-class terminal reason because
  "the document is already open in an instance we did not launch" must be a
  visible refusal, never a silent takeover.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from workbench_server.models.office import EXTENSION_TYPES, DocumentType

#: Which application hosts a document. Mirrors the OnlyOffice document types in
#: ``models/office.py`` but names the *program*, because that is what gets
#: launched and reparented.
HostAppKind = Literal["word", "excel", "powerpoint"]

#: Applications this version will host. PowerPoint is deliberately absent; see
#: the module docstring.
HOSTABLE_KINDS: tuple[HostAppKind, ...] = ("word", "excel")

_TYPE_KINDS: dict[DocumentType, HostAppKind] = {
    "word": "word",
    "cell": "excel",
    "slide": "powerpoint",
}

#: Extension -> application, derived from the OnlyOffice table so the two
#: cannot drift apart as formats are added.
HOST_APP_KINDS: dict[str, HostAppKind] = {
    extension: _TYPE_KINDS[doc_type] for extension, doc_type in EXTENSION_TYPES.items()
}


def host_app_kind(path: str) -> HostAppKind | None:
    """Which application would open this document, or None if it is not one."""
    return HOST_APP_KINDS.get(Path(path).suffix.lower())


#: The lifecycle of one hosted document.
#:
#: * ``launching``  — the application is starting with the document.
#: * ``embedding``  — its window exists and is being reparented into the panel.
#: * ``embedded``   — live inside the panel; the normal working state.
#: * ``detached``   — released back to the desktop, still open and still ours.
#: * ``closed``     — terminal: we closed it (user, or server shutdown).
#: * ``crashed``    — terminal: the process went away underneath us.
#: * ``failed``     — terminal: a step refused, and nothing is hosted.
HostState = Literal["closed", "launching", "embedding", "embedded", "detached", "crashed", "failed"]

#: Why a host reached the state it is in. Only terminal states carry one.
HostReason = Literal[
    "user_closed",
    "server_shutdown",
    "launch_timeout",
    "launch_failed",
    "embed_refused",
    # A backend call ran past the service's own ceiling and was cancelled. Its
    # own timeout is the first line of defence (``launch_timeout``); this is the
    # backstop for an implementation that has none, and the difference matters
    # when reading a log: nobody refused anything, it simply never came back.
    "backend_timeout",
    "process_exited",
    "document_open_elsewhere",
    "powerpoint_preview_only",
    "unsupported_file",
    "native_hosting_disabled",
]

#: ``WORKBENCH_OFFICE_NATIVE``. ``auto`` currently resolves to *not* hosting
#: natively: the window-hosting backend does not ship yet, and it only becomes
#: the default once hang isolation is proven (owner decision, 2026-08-05).
OfficeNativeMode = Literal["auto", "on", "off"]


class PanelRect(BaseModel):
    """Where the panel is, in physical pixels relative to the host window.

    Negative origins are legal (a panel can be scrolled or dragged partly out of
    view); a zero-area rectangle is not — there is no window to give it to.
    """

    x: int = Field(ge=-100_000, le=100_000)
    y: int = Field(ge=-100_000, le=100_000)
    width: int = Field(ge=1, le=100_000)
    height: int = Field(ge=1, le=100_000)


class OfficeHostInfo(BaseModel):
    """One hosted document, as the UI sees it."""

    host_id: str
    #: Workspace-relative POSIX path, as everywhere else on the wire.
    path: str
    kind: HostAppKind
    state: HostState
    #: Set on terminal states, None while the host is live.
    reason: HostReason | None = None
    #: The OS process **we launched**. None until the launch returns, and never
    #: a process we merely found (see ``services/office_host/service.py``).
    pid: int | None = None
    #: Unix seconds at which the current state was entered.
    since: float
    #: We asked the instance to quit and it did not. The host is settled either
    #: way — but the process behind it may well still be on screen, so "closed"
    #: alone would be a claim we cannot make. Cleared when a later sweep gets
    #: the close through, or finds the process gone by itself.
    close_failed: bool = False


class OfficeHostEvent(BaseModel):
    """Broadcast on /ws/events on every state change of a host.

    Rides the existing bus, so a window that never issued the open request still
    tracks the document — and a reconnecting client re-reads the same truth from
    ``GET /api/office/hosts``.
    """

    type: Literal["office_host"] = "office_host"
    host: OfficeHostInfo


class OfficeHostList(BaseModel):
    """GET /api/office/hosts — every host this server knows about.

    In-memory only: a server restart returns an empty list, and any Office
    window it had reparented has already been reaped by shutdown.
    """

    hosts: list[OfficeHostInfo] = Field(default_factory=list)


class OpenHostRequest(BaseModel):
    """POST /api/office/host. The application is derived from the extension —
    the client does not get to nominate one, so an ``.xlsx`` can never be
    launched into Word."""

    path: str = Field(min_length=1)
    #: Where to put the window. Optional: a host can be launched before the
    #: panel has been laid out, and the bounds arrive with the first resize.
    rect: PanelRect | None = None


class SetBoundsRequest(BaseModel):
    """POST /api/office/host/{host_id}/bounds — the panel moved or resized."""

    rect: PanelRect


class OfficeCapabilities(BaseModel):
    """GET /api/office/capabilities — what this machine can actually do.

    Reported honestly so the UI can degrade without guessing: ``native_hosting``
    is the only field that answers "can I dock a real document right now", and
    ``fake_backend`` says when that answer is being given by a stand-in that
    hosts nothing.
    """

    #: The configured policy, before resolution.
    office_native: OfficeNativeMode
    #: Can a host be opened at all right now (policy AND a usable backend).
    native_hosting: bool
    #: Best-effort probe for an installed Microsoft Office.
    office_detected: bool
    #: The in-process fake backend is answering (``WORKBENCH_OFFICE_FAKE=1``).
    fake_backend: bool
    #: Applications that can be hosted right now; empty when hosting is off.
    hostable_kinds: list[HostAppKind] = Field(default_factory=list)
    #: The OnlyOffice Document Server is configured and reachable-in-principle.
    onlyoffice: bool
    #: What a document should open in, given everything above.
    fallback: Literal["native", "onlyoffice", "preview"]
    #: One line naming the reason for the verdict, for the UI and the logs.
    detail: str
