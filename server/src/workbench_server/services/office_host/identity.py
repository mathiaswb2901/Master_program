"""Reading which Microsoft account this machine's Office is signed in as.

A docked native Office is the user's own local install, so it runs as their
signed-in Microsoft account — identity, license, templates and OneDrive are
inherited by construction. This module *reads* that identity so the UI can show
"signed in as X" and degrade an unsigned or unlicensed instance to a "sign into
Word to edit" card. It never *changes* it: multi-account switching is a separate,
spike-first problem (there is no clean COM property to pin an automation instance
to a chosen account), and nothing here attempts it.

Two seams, the same split the host backend already uses:

* :func:`fake_identity` — a deterministic synthetic account, so CI is green with
  no Office installed (``WORKBENCH_OFFICE_FAKE``).
* :func:`probe_identity` — the best-effort real read. Office caches its signed-in
  accounts in the registry under
  ``HKCU\\Software\\Microsoft\\Office\\16.0\\Common\\Identity\\Identities`` (one
  subkey per account, with ``EmailAddress`` and ``FriendlyName`` values). The
  read is cheap and non-blocking, and — crucially — **honest**: where it cannot
  determine something (which account is active among several, the license state,
  off a Windows host) it reports ``None`` / ``unknown`` rather than fabricating,
  and any registry hiccup degrades to "not signed in" instead of raising out of
  the endpoint.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import structlog

from workbench_server.models.office_host import OfficeAccount, OfficeIdentity

if TYPE_CHECKING:
    # Windows-only, and imported here purely so the ``_value`` annotation
    # resolves; the runtime imports live inside the functions that the
    # ``sys.platform`` guard only reaches on Windows.
    import winreg

log = structlog.get_logger()

#: The synthetic account the fake backend reports. A licensed, signed-in analyst,
#: so the whole "signed in as X, licensed to edit" path is exercised in CI with
#: no Microsoft Office anywhere.
FAKE_ACCOUNT = OfficeAccount(display_name="Analyst", email="analyst@example.com")

#: Where Office caches signed-in accounts. ``16.0`` is the shipping Office major
#: version shared by 2016/2019/2021/365 — the registry hive does not track the
#: marketing name. Each subkey is one account; its ``EmailAddress`` and
#: ``FriendlyName`` values name it.
_IDENTITIES_KEY = r"Software\Microsoft\Office\16.0\Common\Identity\Identities"


def fake_identity() -> OfficeIdentity:
    """The deterministic synthetic identity for ``WORKBENCH_OFFICE_FAKE``."""
    return OfficeIdentity(
        signed_in=True,
        active=FAKE_ACCOUNT,
        accounts=[FAKE_ACCOUNT],
        license="licensed",
        detail="fake host backend: synthetic account Analyst <analyst@example.com>, licensed",
    )


def probe_identity() -> OfficeIdentity:
    """Best-effort real read of the signed-in Office account and license state.

    Never raises: this is called from a request handler, and a registry that is
    missing, locked or malformed must cost a degraded report, not a 500. Runs
    only a cheap registry read; kept synchronous so the service can push it off
    the event loop with :func:`asyncio.to_thread`, the same way the host backend
    runs its COM work off-loop.
    """
    if sys.platform != "win32":
        return OfficeIdentity(
            signed_in=False,
            license="unknown",
            detail="reading the Office account is only available on Windows",
        )
    try:
        accounts = _read_accounts()
    except OSError as error:
        # A locked or malformed hive, or a permissions refusal. Degrade rather
        # than let it out of the endpoint — an unknown identity is a safe answer.
        log.warning("office_identity.read_failed", detail=f"{type(error).__name__}: {error}")
        return OfficeIdentity(
            signed_in=False,
            license="unknown",
            detail="could not read the Office identity from the registry",
        )
    if not accounts:
        # Said explicitly: "none signed in" is a different answer from "could not
        # read", and the UI (and a reading agent) must not have to guess which.
        return OfficeIdentity(
            signed_in=False,
            license="unknown",
            detail="no Microsoft account is signed into Office on this machine",
        )
    # Which of several accounts a launched instance runs as is not something the
    # Identities hive alone reveals — that determination is part of the
    # switching spike, not this read. So: the sole account when there is one,
    # and an honest "cannot tell" when there are several.
    active = accounts[0] if len(accounts) == 1 else None
    return OfficeIdentity(
        signed_in=True,
        active=active,
        accounts=accounts,
        # The registry names the accounts but not, reliably, the license state —
        # that needs a COM read of the live application, which lands with the
        # rest of the COM bridge. Honest "unknown" until then, never a guessed
        # "licensed" that would silently strand the user.
        license="unknown",
        detail=_detail(accounts, active),
    )


def _detail(accounts: list[OfficeAccount], active: OfficeAccount | None) -> str:
    if active is not None:
        return f"signed into Office as {_label(active)}"
    return (
        f"{len(accounts)} Microsoft accounts are signed into Office; "
        "which one a launched instance runs as cannot be read from the registry"
    )


def _label(account: OfficeAccount) -> str:
    return account.email or account.display_name or "an unnamed account"


def _read_accounts() -> list[OfficeAccount]:
    """Every Microsoft account cached under the Office Identities key.

    Only ever called on Windows (the caller guards ``sys.platform``), so the
    ``winreg`` import is safe here and absent everywhere else — the module
    imports cleanly on any platform for collection. A missing Identities key
    means Office is not installed, or nobody has signed in: an empty list, not
    an error.
    """
    import winreg

    try:
        identities = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _IDENTITIES_KEY)
    except FileNotFoundError:
        return []
    accounts: list[OfficeAccount] = []
    with identities:
        subkey_count = winreg.QueryInfoKey(identities)[0]
        for index in range(subkey_count):
            name = winreg.EnumKey(identities, index)
            try:
                with winreg.OpenKey(identities, name) as account:
                    email = _value(account, "EmailAddress")
                    friendly = _value(account, "FriendlyName")
            except OSError as error:
                # One restricted or corrupted identity subkey must not discard
                # the accounts already read. Skip the bad one and keep going —
                # a machine with two signed-in accounts where only the second
                # trips a PermissionError still reports the first, instead of
                # the whole read degrading to "could not read".
                log.warning(
                    "office_identity.subkey_read_failed",
                    subkey=name,
                    detail=f"{type(error).__name__}: {error}",
                )
                continue
            if email or friendly:
                accounts.append(OfficeAccount(display_name=friendly, email=email))
    return accounts


def _value(key: winreg.HKEYType, name: str) -> str | None:
    """One string value under ``key``, or None when it is absent or not a string.

    A registry hiccup on a single value is not worth failing the whole read for —
    an account with a readable name and an unreadable email is still worth
    reporting — so a missing value degrades that field to None.
    """
    import winreg

    try:
        value, kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    if kind != winreg.REG_SZ or not isinstance(value, str) or not value:
        return None
    return value
