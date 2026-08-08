"""First-run / Setup detection, up close.

Everything here runs with no real Claude and no Office. The Claude check is
*presence only* — it reports a credential that already exists and never triggers
a login — so it is driven by an injected probe (both answers) and by a direct
test of the credential reader against a throwaway home. The office readiness is
*echoed* from the capabilities authority, so a fake capabilities object drives
the ok / unavailable branches without a Document Server.
"""

from collections.abc import Mapping
from pathlib import Path

from httpx import AsyncClient

from workbench_server.models.office_host import OfficeCapabilities, OfficeNativeMode
from workbench_server.models.setup import CheckId, SetupCheck, SetupStatus
from workbench_server.services.setup import (
    CLAUDE_LOGIN_COMMAND,
    SetupService,
    detect_claude_login,
)
from workbench_server.services.workspace import Workspace

# ---- fakes for the two office authorities ----------------------------------


def _caps(
    *,
    native_hosting: bool = False,
    office_detected: bool = False,
    onlyoffice: bool = False,
    shell_attached: bool = False,
    office_native: OfficeNativeMode = "auto",
    detail: str = "detail line",
) -> OfficeCapabilities:
    return OfficeCapabilities(
        office_native=office_native,
        native_hosting=native_hosting,
        office_detected=office_detected,
        fake_backend=native_hosting,
        shell_attached=shell_attached,
        hostable_kinds=["word", "excel"] if native_hosting else [],
        onlyoffice=onlyoffice,
        fallback="native" if native_hosting else ("onlyoffice" if onlyoffice else "preview"),
        detail=detail,
    )


class _FakeHost:
    def __init__(self, caps: OfficeCapabilities) -> None:
        self._caps = caps

    def capabilities(self, onlyoffice_enabled: bool) -> OfficeCapabilities:
        return self._caps


class _FakeOffice:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled


def _service(
    root: Path,
    *,
    signed_in: bool = False,
    caps: OfficeCapabilities | None = None,
    onlyoffice_enabled: bool = False,
) -> SetupService:
    return SetupService(
        Workspace(root),
        _FakeHost(caps or _caps()),
        _FakeOffice(onlyoffice_enabled),
        signed_in_probe=lambda: signed_in,
    )


def _check(status: SetupStatus, check_id: CheckId) -> SetupCheck:
    return next(c for c in status.checks if c.id == check_id)


# ---- fresh vs configured workspace -----------------------------------------


def test_fresh_workspace_is_first_run(tmp_path: Path) -> None:
    status = _service(tmp_path).status()
    assert status.first_run is True


def test_workspace_with_dot_workbench_is_not_first_run(tmp_path: Path) -> None:
    (tmp_path / ".workbench").mkdir()
    status = _service(tmp_path).status()
    assert status.first_run is False


# ---- Claude login: detected, never performed -------------------------------


def test_claude_signed_in_true_reports_ok(tmp_path: Path) -> None:
    status = _service(tmp_path, signed_in=True).status()
    claude = _check(status, "claude_login")
    assert claude.state == "ok"
    assert claude.action is None


def test_claude_signed_out_needs_action_with_instruction_not_a_button(tmp_path: Path) -> None:
    status = _service(tmp_path, signed_in=False).status()
    claude = _check(status, "claude_login")
    assert claude.state == "action_needed"
    assert claude.action is not None
    # An instruction the human runs — never a command the app could fire.
    assert claude.action.kind == "instruction"
    assert claude.action.command_id is None
    assert claude.action.instruction == CLAUDE_LOGIN_COMMAND


def test_detect_claude_login_env_key(tmp_path: Path) -> None:
    assert detect_claude_login(tmp_path, {"ANTHROPIC_API_KEY": "sk-ant-123"}) is True
    assert detect_claude_login(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}) is True


def test_detect_claude_login_blank_env_is_not_signed_in(tmp_path: Path) -> None:
    assert detect_claude_login(tmp_path, {"ANTHROPIC_API_KEY": "  "}) is False


def test_detect_claude_login_credentials_file(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / ".credentials.json").write_text("{}", encoding="utf-8")
    assert detect_claude_login(tmp_path, {}) is True


def test_detect_claude_login_config_oauth_account(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "a@b.c"}}', "utf-8")
    assert detect_claude_login(tmp_path, {}) is True


def test_detect_claude_login_config_without_account_is_not_signed_in(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text('{"projects": {}}', encoding="utf-8")
    assert detect_claude_login(tmp_path, {}) is False


def test_detect_claude_login_empty_home_is_not_signed_in(tmp_path: Path) -> None:
    assert detect_claude_login(tmp_path, {}) is False


def test_detect_claude_login_survives_unreadable_config(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text("}}} not json", encoding="utf-8")
    assert detect_claude_login(tmp_path, {}) is False


# ---- office readiness echoes the capabilities authority --------------------


def test_office_ok_when_native_hosting(tmp_path: Path) -> None:
    caps = _caps(native_hosting=True, detail="native hosting available")
    status = _service(tmp_path, caps=caps).status()
    office = _check(status, "office")
    assert office.state == "ok"
    assert office.detail == "native hosting available"


def test_office_unavailable_when_no_office_and_never_blocks_all_ok(tmp_path: Path) -> None:
    caps = _caps(native_hosting=False, office_detected=False, detail="no Office found")
    status = _service(tmp_path, signed_in=True, caps=caps).status()
    office = _check(status, "office")
    assert office.state == "unavailable"
    assert office.detail == "no Office found"
    # Signed in and no other action needed → the walkthrough gets out of the way,
    # even though Office is not available on this machine.
    assert status.all_ok is True


def test_office_detected_in_browser_offers_desktop_pointer(tmp_path: Path) -> None:
    caps = _caps(native_hosting=False, office_detected=True, detail="use the desktop shell")
    status = _service(tmp_path, caps=caps).status()
    office = _check(status, "office")
    assert office.state == "unavailable"
    assert office.action is not None and office.action.kind == "instruction"


def test_office_detected_with_shell_attached_omits_desktop_pointer(tmp_path: Path) -> None:
    # Already inside the desktop app: "open the desktop app" would be a no-op, so
    # the pointer must not be offered even though Office is detected.
    caps = _caps(
        native_hosting=False,
        office_detected=True,
        shell_attached=True,
        detail="native hosting disabled",
    )
    status = _service(tmp_path, caps=caps).status()
    office = _check(status, "office")
    assert office.state == "unavailable"
    assert office.action is None


def test_office_native_off_omits_misleading_pointer(tmp_path: Path) -> None:
    # An explicit operator override (WORKBENCH_OFFICE_NATIVE=off) is not fixed by
    # launching the desktop app, so no "open the app" nudge is attached.
    caps = _caps(
        native_hosting=False,
        office_detected=True,
        office_native="off",
        detail="native hosting off by policy",
    )
    status = _service(tmp_path, caps=caps).status()
    office = _check(status, "office")
    assert office.state == "unavailable"
    assert office.action is None


def test_onlyoffice_ok_when_configured(tmp_path: Path) -> None:
    caps = _caps(onlyoffice=True)
    status = _service(tmp_path, caps=caps, onlyoffice_enabled=True).status()
    assert _check(status, "onlyoffice").state == "ok"


def test_onlyoffice_unavailable_when_not_configured(tmp_path: Path) -> None:
    assert _check(_service(tmp_path).status(), "onlyoffice").state == "unavailable"


def test_shell_ok_when_attached(tmp_path: Path) -> None:
    caps = _caps(shell_attached=True)
    assert _check(_service(tmp_path, caps=caps).status(), "shell").state == "ok"


def test_shell_unavailable_in_browser_tab(tmp_path: Path) -> None:
    assert _check(_service(tmp_path).status(), "shell").state == "unavailable"


def test_workspace_check_names_the_folder(tmp_path: Path) -> None:
    workspace = _check(_service(tmp_path).status(), "workspace")
    assert workspace.state == "ok"
    assert tmp_path.name in workspace.detail


# ---- all_ok is exactly "nothing is action_needed" --------------------------


def test_all_ok_false_when_signed_out(tmp_path: Path) -> None:
    assert _service(tmp_path, signed_in=False).status().all_ok is False


def test_all_ok_true_when_signed_in_and_rest_environmental(tmp_path: Path) -> None:
    assert _service(tmp_path, signed_in=True).status().all_ok is True


# ---- the router, end to end ------------------------------------------------


async def test_status_endpoint_reports_fresh_and_signed_out_under_fake_agent(
    client: AsyncClient,
) -> None:
    # The default test client builds the app with the fake agent off; assert the
    # endpoint answers with the honest shape regardless of the machine's Claude.
    response = await client.get("/api/setup/status")
    assert response.status_code == 200
    body = response.json()
    assert "checks" in body and isinstance(body["checks"], list)
    ids = {c["id"] for c in body["checks"]}
    assert ids == {"claude_login", "office", "onlyoffice", "workspace", "shell"}
    # A brand-new tmp workspace has no .workbench yet.
    assert body["first_run"] is True


def test_mapping_type_is_accepted(tmp_path: Path) -> None:
    # detect_claude_login takes a Mapping, so os.environ and a plain dict both fit.
    env: Mapping[str, str] = {"ANTHROPIC_API_KEY": "x"}
    assert detect_claude_login(tmp_path, env) is True
