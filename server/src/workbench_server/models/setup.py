"""First-run / Setup schemas: an honest, typed answer to "what is wired up?".

The Setup capability greets a fresh workspace and tells a stranger, once and
plainly, which connections are live — is Claude signed in, can a real Office
document be docked, is the OnlyOffice fallback configured — and then gets out of
the way. Everything here is *detected and reported*, never performed: the app
cannot log a user into Claude (that is the CLI's ``claude /login`` and a
credential action the safety rules keep out of the app), so a check that is not
``ok`` carries an **instruction**, never a button that would run the login.

The readiness read reuses the authorities that already exist rather than
computing a second one: the Office/OnlyOffice/shell picture is echoed from
``GET /api/office/capabilities`` (:class:`OfficeCapabilities`), so the two can
never drift apart.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: The connections a first run reports on. ``claude_login`` is the one that
#: matters — the others are environmental (a browser tab cannot host a window,
#: a machine may have no Office) and their absence is reported, never nagged.
CheckId = Literal["claude_login", "office", "onlyoffice", "workspace", "shell"]

#: What a check found.
#:
#: * ``ok`` — wired up and ready.
#: * ``action_needed`` — the user can and should do one thing to wire it up; the
#:   only state that keeps the walkthrough from getting out of the way.
#: * ``unavailable`` — not wired up and not the user's to fix here (no Office on
#:   the machine, a browser tab with no window to host into). Reported honestly
#:   so the checklist is complete, but it never blocks :attr:`SetupStatus.all_ok`.
CheckState = Literal["ok", "action_needed", "unavailable"]

#: How a check's one action is carried out.
#:
#: * ``command`` — a *registered* window command the UI can run (its id is in
#:   ``command_id``); the safe, in-app half.
#: * ``instruction`` — an exact line the human runs themselves (its text is in
#:   ``instruction``), for anything the app must not do on their behalf. Claude
#:   login is the motivating case: ``claude /login`` in a terminal, never a
#:   button in here.
ActionKind = Literal["command", "instruction"]


class SetupAction(BaseModel):
    """The one thing that would move a check toward ``ok``.

    Exactly one of :attr:`command_id` / :attr:`instruction` is set, per
    :attr:`kind`. An action is a suggestion the user chooses to take — never
    something Setup does silently, and for ``instruction`` never something the
    app can do at all.
    """

    kind: ActionKind
    #: Button / row label, e.g. "Sign in from a terminal" or "Show the welcome".
    label: str
    #: A registered command id, for ``kind == "command"``.
    command_id: str | None = None
    #: The exact line the human runs, for ``kind == "instruction"`` — e.g.
    #: ``claude /login``. Rendered as copyable text, never wired to a handler.
    instruction: str | None = None


class SetupCheck(BaseModel):
    """One connection, as the first-run checklist shows it."""

    id: CheckId
    #: Short human label for the row, e.g. "Claude" / "Office".
    title: str
    state: CheckState
    #: One line the human reads — "Signed in", "Run claude /login in a terminal",
    #: "No Microsoft Office was found on this machine".
    detail: str
    #: The single action that addresses this check, or None when there is
    #: nothing to do (already ``ok``, or genuinely ``unavailable``).
    action: SetupAction | None = None


class SetupStatus(BaseModel):
    """GET /api/setup/status — the whole first-run picture, computed server-side.

    Every input has a fake so the entire first-run state is CI-drivable with no
    real Claude and no Office: a workspace with no ``.workbench/`` is
    :attr:`first_run`, ``WORKBENCH_OFFICE_FAKE`` / capabilities drive the Office
    checks, and the Claude check reports signed-out under the fake agent.
    """

    checks: list[SetupCheck] = Field(default_factory=list)
    #: No ``.workbench/`` state in this workspace yet — the same signal the
    #: welcome card's auto-open uses. Drives the auto-open of the walkthrough.
    first_run: bool
    #: Derived: nothing is ``action_needed``. The walkthrough gets out of the
    #: way (and the status-bar reading hides) when this is true.
    all_ok: bool
