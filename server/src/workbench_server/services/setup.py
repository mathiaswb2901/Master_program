"""First-run detection: an honest read of what is wired up, computed not guessed.

Three inputs, each with a fake so the whole first-run state is CI-drivable:

* **A fresh workspace** — no ``.workbench/`` directory yet. The same signal the
  welcome card keys its auto-open on, read live from the (mutable) workspace so
  a switch re-roots the question with everything else.
* **Claude login presence** — *detected, never performed*. The app cannot log a
  user in; this only reports whether a login already exists on the machine (an
  ``ANTHROPIC_API_KEY`` / ``CLAUDE_CODE_OAUTH_TOKEN`` in the environment, or the
  CLI's cached credentials under ``~/.claude``). The probe is injected so the
  wiring can force "signed out" under the fake agent — there is no real Claude
  behind a CI run — and so a test can drive both answers without touching a real
  home directory.
* **Office / OnlyOffice readiness** — echoed from the existing
  :class:`~workbench_server.models.office_host.OfficeCapabilities`, never a
  second computation of the same fact.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from workbench_server.models.office_host import OfficeCapabilities
from workbench_server.models.setup import SetupAction, SetupCheck, SetupStatus
from workbench_server.services.workspace import Workspace


class CapabilitiesSource(Protocol):
    """The one thing Setup asks the office host: what can this machine do. Narrowed
    to the method Setup reads so the service depends on the authority, not on the
    whole host lifecycle (``OfficeHostService`` satisfies it structurally)."""

    def capabilities(self, onlyoffice_enabled: bool) -> OfficeCapabilities: ...


class OnlyOfficeState(Protocol):
    """Whether the OnlyOffice Document Server is configured (``OfficeService``)."""

    @property
    def enabled(self) -> bool: ...


#: The workspace's own state directory. Its absence is a fresh workspace.
WORKBENCH_DIR = ".workbench"

#: The exact line a signed-out user runs — an instruction, never a button.
CLAUDE_LOGIN_COMMAND = "claude /login"

#: Environment variables that mean a Claude credential is already present.
_CLAUDE_ENV_KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def detect_claude_login(home: Path, environ: Mapping[str, str]) -> bool:
    """Whether a Claude credential already exists on this machine.

    Presence only: this reads, it never authenticates. True when an API key or
    OAuth token is in the environment, or when the Claude CLI has cached
    credentials under ``~/.claude`` (``.credentials.json``, or an account on
    ``.claude.json``). A best-effort read that answers False rather than raising
    when a path cannot be read — an unreadable home is "cannot tell", which the
    UI shows as signed-out with the sign-in instruction, never a fabricated yes.
    """
    for key in _CLAUDE_ENV_KEYS:
        if environ.get(key, "").strip():
            return True
    if (home / ".claude" / ".credentials.json").is_file():
        return True
    # Newer CLIs keep the OAuth account inline on ``~/.claude.json``; its mere
    # presence is not enough (it exists after any run), so require the account.
    config = home / ".claude.json"
    try:
        if config.is_file():
            import json

            data = json.loads(config.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("oauthAccount"):
                return True
    except (OSError, ValueError):
        pass
    return False


class SetupService:
    """Computes :class:`SetupStatus` from the live workspace and the office
    authority. Holds the mutable :class:`Workspace` rather than copying its root,
    so a workspace switch re-roots first-run detection for free (CLAUDE.md).
    """

    def __init__(
        self,
        workspace: Workspace,
        office_host: CapabilitiesSource,
        office: OnlyOfficeState,
        *,
        signed_in_probe: Callable[[], bool],
    ) -> None:
        self._workspace = workspace
        self._office_host = office_host
        self._office = office
        self._signed_in_probe = signed_in_probe

    def status(self) -> SetupStatus:
        caps = self._office_host.capabilities(self._office.enabled)
        checks = [
            self._claude_check(),
            self._office_check(caps),
            self._onlyoffice_check(caps),
            self._workspace_check(),
            self._shell_check(caps),
        ]
        all_ok = not any(check.state == "action_needed" for check in checks)
        return SetupStatus(checks=checks, first_run=self._is_first_run(), all_ok=all_ok)

    def _is_first_run(self) -> bool:
        return not (self._workspace.root / WORKBENCH_DIR).exists()

    def _claude_check(self) -> SetupCheck:
        if self._signed_in_probe():
            return SetupCheck(
                id="claude_login",
                title="Claude",
                state="ok",
                detail="Claude is signed in on this machine.",
            )
        return SetupCheck(
            id="claude_login",
            title="Claude",
            state="action_needed",
            detail=f"Claude is not signed in — run {CLAUDE_LOGIN_COMMAND} in a terminal.",
            action=SetupAction(
                kind="instruction",
                label="Sign in from a terminal",
                instruction=CLAUDE_LOGIN_COMMAND,
            ),
        )

    def _office_check(self, caps: OfficeCapabilities) -> SetupCheck:
        if caps.native_hosting:
            return SetupCheck(
                id="office",
                title="Office",
                state="ok",
                detail=caps.detail,
            )
        # Not native here is not the user's error to fix in a checklist: a
        # browser tab has no window to host into, and a machine may have no
        # Office. Reported honestly as unavailable (never blocks all_ok), with a
        # pointer only when it names the real cause. "Open the desktop app" is a
        # no-op — and so misleading — when the shell is already attached (the
        # user is in the desktop app) or when native hosting was deliberately
        # turned off, whether in the Settings panel or by WORKBENCH_OFFICE_NATIVE
        # (`caps.office_native` is the resolved answer and does not say which);
        # neither is fixed by launching an app that is already running or was
        # switched off on purpose. Which one switched it off is `caps.detail`'s
        # job to say, and it does.
        action = (
            SetupAction(
                kind="instruction",
                label="Open the desktop app to dock a document",
                instruction="Run the Workbench desktop app (cd desktop && npm run tauri dev).",
            )
            if caps.office_detected and not caps.shell_attached and caps.office_native != "off"
            else None
        )
        return SetupCheck(
            id="office",
            title="Office",
            state="unavailable",
            detail=caps.detail,
            action=action,
        )

    def _onlyoffice_check(self, caps: OfficeCapabilities) -> SetupCheck:
        if caps.onlyoffice:
            return SetupCheck(
                id="onlyoffice",
                title="OnlyOffice",
                state="ok",
                detail="OnlyOffice is configured for document preview and diff.",
            )
        return SetupCheck(
            id="onlyoffice",
            title="OnlyOffice",
            state="unavailable",
            detail="OnlyOffice preview is not configured (optional).",
            action=SetupAction(
                kind="instruction",
                label="Configure the OnlyOffice fallback",
                instruction="Set WORKBENCH_ONLYOFFICE_URL and WORKBENCH_ONLYOFFICE_JWT_SECRET.",
            ),
        )

    def _workspace_check(self) -> SetupCheck:
        name = self._workspace.root.name or str(self._workspace.root)
        return SetupCheck(
            id="workspace",
            title="Workspace",
            state="ok",
            detail=f"Working in {name}.",
        )

    def _shell_check(self, caps: OfficeCapabilities) -> SetupCheck:
        if caps.shell_attached:
            return SetupCheck(
                id="shell",
                title="Desktop shell",
                state="ok",
                detail="The Workbench desktop shell is attached.",
            )
        return SetupCheck(
            id="shell",
            title="Desktop shell",
            state="unavailable",
            detail="Running in a browser tab — the desktop shell adds native window hosting.",
        )
