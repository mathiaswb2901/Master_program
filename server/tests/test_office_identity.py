"""Reading the docked Office account identity and its license state.

Everything here runs with no Microsoft Office and no registry read that depends
on the machine: the fake mode is deterministic, and the real probe is exercised
by scripting its one registry helper, so the suite gives the same answer on the
author's Windows box (which has Office) and on CI (which does not).

Identity is *read* here, never changed — multi-account switching is a separate,
spike-first problem, and there is nothing to test for it in this PR.
"""

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_office_host import make_service
from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.office_host import OfficeAccount, OfficeIdentity
from workbench_server.services.office_host import identity as identity_module
from workbench_server.services.office_host.identity import (
    FAKE_ACCOUNT,
    fake_identity,
    probe_identity,
)

# ---- fake mode: the synthetic signed-in, licensed account --------------------


def test_fake_identity_is_a_signed_in_licensed_account() -> None:
    ident = fake_identity()
    assert ident.signed_in is True
    assert ident.license == "licensed"
    assert ident.active == FAKE_ACCOUNT
    assert ident.accounts == [FAKE_ACCOUNT]
    assert ident.active is not None
    assert ident.active.email == "analyst@example.com"
    assert ident.detail


async def test_the_service_reports_the_fake_account_in_fake_mode(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, fake=True)
    ident = await service.identity()
    assert ident.signed_in is True
    assert ident.license == "licensed"
    assert ident.active == FAKE_ACCOUNT


# ---- the real probe, scripted so it does not depend on the machine -----------


def test_probe_reports_the_sole_account_as_active(monkeypatch: pytest.MonkeyPatch) -> None:
    account = OfficeAccount(display_name="Ada Lovelace", email="ada@example.com")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(identity_module, "_read_accounts", lambda: [account])
    ident = probe_identity()
    assert ident.signed_in is True
    assert ident.active == account
    assert ident.accounts == [account]
    assert ident.license == "unknown"  # the registry does not carry it; honest
    assert "ada@example.com" in ident.detail


def test_probe_cannot_name_the_active_account_when_several_are_signed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [
        OfficeAccount(display_name="Work", email="work@corp.example"),
        OfficeAccount(display_name="Personal", email="me@personal.example"),
    ]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(identity_module, "_read_accounts", lambda: accounts)
    ident = probe_identity()
    assert ident.signed_in is True
    assert ident.accounts == accounts
    assert ident.active is None  # honest "cannot tell", never a guess
    assert ident.license == "unknown"


def test_probe_says_none_explicitly_when_nobody_is_signed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(identity_module, "_read_accounts", lambda: [])
    ident = probe_identity()
    assert ident.signed_in is False
    assert ident.accounts == []
    assert ident.license == "unknown"
    assert "no microsoft account" in ident.detail.lower()


def test_a_registry_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """The requirement in one test: a probe failure yields signed_in=False /
    unknown, and never lets the OSError out of the read."""

    def boom() -> list[OfficeAccount]:
        raise OSError("the hive is locked")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(identity_module, "_read_accounts", boom)
    ident = probe_identity()
    assert ident.signed_in is False
    assert ident.license == "unknown"
    assert ident.active is None
    assert ident.detail


def test_off_windows_the_read_is_simply_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    ident = probe_identity()
    assert ident.signed_in is False
    assert ident.license == "unknown"
    assert "windows" in ident.detail.lower()


async def test_the_service_pushes_the_real_probe_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not fake mode: the service calls the real probe (via ``to_thread``) and
    returns what it produces."""
    account = OfficeAccount(display_name="Grace", email="grace@example.com")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(identity_module, "_read_accounts", lambda: [account])
    service, _ = make_service(tmp_path, fake=False)
    ident = await service.identity()
    assert ident.active == account


# ---- the real registry parse, against a scratch HKCU key (Windows only) ------
#
# The tests above script ``_read_accounts`` itself, which proves ``probe_identity``'s
# branching but never touches the winreg calls that do the actual work. These do:
# they build a key with the same shape Office uses under
# ``…\Common\Identity\Identities`` and run ``_read_accounts`` end-to-end, so the
# ``OpenKey``/``EnumKey``/``QueryValueEx`` reads and the ``REG_SZ`` type check are
# exercised against real registry semantics on the Windows CI runner (which has no
# Office, hence no real Identities key).

_SCRATCH_KEY = r"Software\Workbench\_identity_test_scratch"

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="winreg and HKCU exist only on Windows"
)

#: Every helper below touches `winreg`, whose attributes typeshed defines only
#: under `sys.platform == "win32"` — so on the matrix's linux and macos legs
#: (M7 §C2) the bodies have to sit inside a platform branch, not merely behind
#: the `windows_only` mark. mypy skips a branch a platform check rules out; a
#: pytest mark it does not see at all. The non-Windows half of each branch never
#: runs — the mark is what stops these being called there.
_NOT_WINDOWS = "winreg is Windows-only; this helper is guarded by @windows_only"


def _delete_tree(path: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS)
    else:
        import winreg

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS)
        except FileNotFoundError:
            return
        with key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


def _write_account(
    subkey: str,
    *,
    email: str | None = None,
    friendly: str | None = None,
    email_kind: int | None = None,
) -> None:
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS)
    else:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _SCRATCH_KEY + "\\" + subkey) as key:
            if email is not None:
                winreg.SetValueEx(key, "EmailAddress", 0, email_kind or winreg.REG_SZ, email)
            if friendly is not None:
                winreg.SetValueEx(key, "FriendlyName", 0, winreg.REG_SZ, friendly)


def _reg_expand_sz() -> int:
    """``winreg.REG_EXPAND_SZ``, read behind the branch mypy needs off Windows."""
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS)
    else:
        import winreg

        return winreg.REG_EXPAND_SZ


def _refuse_to_open(monkeypatch: pytest.MonkeyPatch, subkey: str) -> None:
    """Make ``winreg.OpenKey`` raise ``PermissionError`` for one subkey name."""
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS)
    else:
        import winreg

        real_open = winreg.OpenKey

        def open_but_refuse_the_bad_one(key: Any, sub_key: str, *args: Any, **kwargs: Any) -> Any:
            if sub_key == subkey:
                raise PermissionError("access is denied")
            return real_open(key, sub_key, *args, **kwargs)

        monkeypatch.setattr(winreg, "OpenKey", open_but_refuse_the_bad_one)


@pytest.fixture
def scratch_identities(monkeypatch: pytest.MonkeyPatch) -> Any:
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS)
    else:
        import winreg

        _delete_tree(_SCRATCH_KEY)
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, _SCRATCH_KEY).Close()
        monkeypatch.setattr(identity_module, "_IDENTITIES_KEY", _SCRATCH_KEY)
        try:
            yield
        finally:
            _delete_tree(_SCRATCH_KEY)


@windows_only
def test_read_accounts_parses_a_real_registry_key(scratch_identities: None) -> None:
    _write_account("id-ada", email="ada@example.com", friendly="Ada Lovelace")
    _write_account("id-grace", email="grace@example.com", friendly="Grace Hopper")
    accounts = identity_module._read_accounts()
    by_email = {a.email: a for a in accounts}
    assert set(by_email) == {"ada@example.com", "grace@example.com"}
    assert by_email["ada@example.com"].display_name == "Ada Lovelace"
    assert by_email["grace@example.com"].display_name == "Grace Hopper"


@windows_only
def test_read_accounts_keeps_a_name_only_identity_and_drops_an_empty_one(
    scratch_identities: None,
) -> None:
    _write_account("id-friendly-only", friendly="Katherine Johnson")
    _write_account("id-empty")  # neither value: names nobody, not reported
    accounts = identity_module._read_accounts()
    assert [(a.display_name, a.email) for a in accounts] == [("Katherine Johnson", None)]


@windows_only
def test_value_narrows_a_non_reg_sz_type_to_none(scratch_identities: None) -> None:
    """The ``kind != REG_SZ`` check in ``_value`` fires against a real value: an
    ``EmailAddress`` stored as ``REG_EXPAND_SZ`` reads back as None, while a
    sibling ``REG_SZ`` FriendlyName is kept — the account is still reported by
    name rather than the whole read tripping on the unexpected type."""
    _write_account(
        "id-oddtype",
        email="expand@example.com",
        friendly="Odd Type",
        email_kind=_reg_expand_sz(),
    )
    accounts = identity_module._read_accounts()
    assert [(a.display_name, a.email) for a in accounts] == [("Odd Type", None)]


@windows_only
def test_one_unreadable_subkey_does_not_discard_the_others(
    scratch_identities: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permissions refusal on a single identity subkey degrades that one entry,
    not the whole read: the good account still comes through. Regression for the
    conflation of 'partially readable' with 'nothing readable'."""
    _write_account("id-good", email="good@example.com", friendly="Good Account")
    _write_account("id-bad", email="bad@example.com", friendly="Bad Account")
    _refuse_to_open(monkeypatch, "id-bad")
    accounts = identity_module._read_accounts()
    assert [(a.display_name, a.email) for a in accounts] == [("Good Account", "good@example.com")]


# ---- the model round-trips ---------------------------------------------------


def test_the_identity_model_round_trips() -> None:
    ident = OfficeIdentity(
        signed_in=True,
        active=OfficeAccount(display_name="Ada", email="ada@example.com"),
        accounts=[OfficeAccount(display_name="Ada", email="ada@example.com")],
        license="unlicensed",
        detail="signed into Office as ada@example.com",
    )
    assert OfficeIdentity.model_validate(ident.model_dump()) == ident
    assert OfficeIdentity.model_validate_json(ident.model_dump_json()) == ident


# ---- the endpoint, through the real app --------------------------------------


def identity_app(tmp_path: Path, **overrides: Any) -> Any:
    return create_app(
        Settings(
            workspace_root=tmp_path,
            claude_projects_dir=tmp_path / "projects",
            office_native="on",
            office_fake=True,
            **overrides,
        )
    )


def test_the_identity_endpoint_returns_200_and_the_model(tmp_path: Path) -> None:
    with TestClient(identity_app(tmp_path)) as client:
        response = client.get("/api/office/identity")
    assert response.status_code == 200
    ident = OfficeIdentity.model_validate(response.json())
    assert ident.signed_in is True
    assert ident.license == "licensed"
    assert ident.active is not None
    assert ident.active.email == "analyst@example.com"


def test_the_identity_endpoint_on_a_default_machine_does_not_error(tmp_path: Path) -> None:
    """No fake backend, no Office on CI: the endpoint still answers 200 with an
    honest 'not signed in / unknown', never a 500."""
    with TestClient(create_app(Settings(workspace_root=tmp_path))) as client:
        response = client.get("/api/office/identity")
    assert response.status_code == 200
    ident = OfficeIdentity.model_validate(response.json())
    assert ident.license in ("licensed", "unlicensed", "unknown")
    assert ident.detail
