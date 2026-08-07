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

#: ``WORKBENCH_OFFICE_NATIVE``. ``auto`` resolves to hosting natively wherever
#: it is actually possible — Windows, an Office to launch, and the desktop shell
#: attached. It was gated on hang isolation being proven (owner decision,
#: 2026-08-05); that landed with the shell's window-less geometry worker and the
#: measurement in ``hosting_tests::hang_isolation_measurement``.
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


class SetVisibleRequest(BaseModel):
    """POST /api/office/host/{host_id}/visible — the panel went behind another
    tab, or came back.

    A hosted window is a real window: it does not disappear because the ``div``
    that describes it got ``display: none``. Something has to say so.
    """

    visible: bool


#: What the desktop shell is asked to do with a hosted window. One action per
#: window-facing method of :class:`~...office_host.backend.HostBackend`, because
#: the shell is where those four actually happen — ``launch``, ``poll`` and the
#: process half of ``close`` are the server's own work and never come here.
HostCommandAction = Literal["embed", "set_bounds", "set_visible", "detach", "close"]


class HostCommand(BaseModel):
    """One window operation, pushed to the shell over ``/ws/office-host``.

    **Every rectangle here is in physical pixels**, like :class:`PanelRect`
    everywhere else on the wire. The shell divides by its own
    ``devicePixelRatio`` before calling into Rust, which takes CSS pixels and
    multiplies by the window's scale factor again — the same number both ways,
    so the physical rectangle the server asked for is the one that lands.
    """

    type: Literal["host_command"] = "host_command"
    #: Correlates the ack. Unique per command, not per host.
    command_id: str
    host_id: str
    action: HostCommandAction
    #: The guest's ``HWND``, for ``embed``. None for everything else — the shell
    #: has known the window since the embed.
    window_id: int | None = None
    #: Where the panel is, for ``embed`` and ``set_bounds``.
    rect: PanelRect | None = None
    #: For ``set_visible``.
    visible: bool | None = None


class HostCommandAck(BaseModel):
    """The shell's answer to one :class:`HostCommand`.

    ``code`` is the shell's own refusal code (``embed_refused``,
    ``window_gone``, ``unknown_host``, …), kept as free text rather than an
    enum: it is produced by the Rust side, and a shell one version ahead must
    not fail to parse here. The server maps the ones it knows and treats the
    rest as a plain failure.
    """

    type: Literal["host_command_ack"] = "host_command_ack"
    command_id: str
    ok: bool
    code: str | None = None
    message: str | None = None


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
    #: The desktop shell is connected to the host channel. A browser tab cannot
    #: reparent a window, so without this there is nothing to host *into* —
    #: which is why it is reported rather than inferred from the user agent.
    shell_attached: bool = False
    #: Applications that can be hosted right now; empty when hosting is off.
    hostable_kinds: list[HostAppKind] = Field(default_factory=list)
    #: The OnlyOffice Document Server is configured and reachable-in-principle.
    onlyoffice: bool
    #: What a document should open in, given everything above.
    fallback: Literal["native", "onlyoffice", "preview"]
    #: One line naming the reason for the verdict, for the UI and the logs.
    detail: str


#: Whether Office considers itself licensed to edit. ``unknown`` is a first-class
#: answer, not a failure: the honest report when the machine will not say (a
#: registry that does not carry it, a probe that could not run, a non-Windows
#: host). Never guessed — an ``unknown`` degrades the UI to "sign into Word to
#: edit", which is safe, where a fabricated ``licensed`` would strand the user
#: at a read-only window with no explanation.
LicenseState = Literal["licensed", "unlicensed", "unknown"]


class OfficeAccount(BaseModel):
    """One Microsoft account this machine's Office has cached.

    Both halves are best-effort: Office stores a ``FriendlyName`` and an
    ``EmailAddress`` per identity, but either can be absent, so either can be
    ``None``. An account with neither is not reported at all — it would name
    nobody.
    """

    #: The account's display name, e.g. "Ada Lovelace". None when Office has
    #: cached no friendly name for it.
    display_name: str | None = None
    #: The account's email / UPN. None when Office has cached no address.
    email: str | None = None


class OfficeIdentity(BaseModel):
    """GET /api/office/identity — which Microsoft account the machine's Office is
    signed in as, and whether it is licensed to edit.

    Reported honestly, never guessed, in the same spirit as
    :class:`OfficeCapabilities`: where a value cannot be determined it is
    ``None`` / ``unknown``, not fabricated. A docked native Office is the user's
    own local install and runs as their signed-in account — this is the read
    that lets the UI show "signed in as X" and degrade an unsigned or unlicensed
    instance to a "sign into Word to edit" card rather than a silently limited
    one.

    This PR **reads** identity only. Multi-account *switching* — pinning a hosted
    instance to a chosen account — is a separate, spike-first problem (there is
    no clean COM property for it), and nothing here attempts it.
    """

    #: A Microsoft account is signed into Office on this machine. False when none
    #: is cached, when the probe could not run, or off Windows.
    signed_in: bool
    #: The account a launched instance would run as, when it can be told apart
    #: from the rest. None when nobody is signed in, or when several accounts are
    #: signed in and the registry alone does not say which is active — an honest
    #: "cannot tell", never a guess.
    active: OfficeAccount | None = None
    #: Every Microsoft account Office has cached on this machine. Empty when none
    #: is signed in — read the ``detail`` to tell "none signed in" from "could
    #: not read".
    accounts: list[OfficeAccount] = Field(default_factory=list)
    #: Whether Office reports itself licensed to edit. ``unknown`` where the
    #: machine will not say; the UI degrades on ``unknown`` exactly as on
    #: ``unlicensed``.
    license: LicenseState = "unknown"
    #: One line naming what was found, for the UI and the logs, in the same
    #: honest style as :attr:`OfficeCapabilities.detail`.
    detail: str
