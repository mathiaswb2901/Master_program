"""The settings document on disk, and the precedence around it.

One small JSON file under the machine's app-data dir
(``services/app_data.py``), read and written whole. It follows the discipline
``services/layouts.py`` and ``services/sessions.py`` already established, for
the same reasons:

* **Version-stamped**, so a document this code does not understand is discarded
  rather than guessed at.
* **Never raises on read.** A corrupt, oversized or wrong-version file costs the
  user their preferences and nothing else — never a server that will not start.
  Every failure resolves to "defaults, plus a sentence saying why".
* **Atomic writes, retried past a transient Windows lock** (``tmp`` +
  ``os.replace``): a replace onto a path Defender or the indexer momentarily
  holds open fails outright on Windows instead of waiting.
* **utf-8-sig on read**, so a file someone opened in Notepad still parses.

**Where it lives, and where it deliberately does not.** App data, not the
workspace: a theme is about the person at the keyboard, not about the project
(the reasoning ``RecentsStore`` spells out). So this service copies no workspace
root, owes no ``set_workspace_root``, and is deliberately absent from
``create_app``'s ``WorkspaceService`` rootables — switching projects must not
change the palette. And **never** ``~/.claude``: the global Claude scope carries
hooks and permission rules that the server keeps out of sessions by default
(``CLAUDE.md``), and a settings panel able to write there would be a way around
that posture.

**Precedence: outside-the-app beats stored.** ``WORKBENCH_OFFICE_NATIVE=off``
(or the same key in ``workbench.toml``) is an operator's decision about this
launch, so it wins — and it is *reported*, never written into the file, so
un-setting the variable restores the user's own choice. Anything a process was
explicitly configured with arrives here as :class:`ProcessConfig`;
``model_fields_set`` on the app's ``Settings`` names exactly those fields, which
is how ``create_app`` can tell "the user exported it" from "nobody said".
"""

import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

import structlog
from pydantic import BaseModel, ValidationError

from workbench_server.models.office_host import OfficeNativeMode
from workbench_server.models.settings import (
    MAX_FILE_BYTES,
    SETTINGS_VERSION,
    SettingKey,
    SettingOverride,
    SettingsFile,
    SettingsState,
    TelemetryStance,
    WorkbenchSettings,
)
from workbench_server.services.app_data import app_data_dir

log = structlog.get_logger()

SETTINGS_FILE = "settings.json"

# Same transient-lock retry as `services/layouts.py`; see the note there.
REPLACE_ATTEMPTS = 10
REPLACE_BACKOFF_S = 0.02

#: The settings this process only reads at launch. A change to one of them is
#: stored immediately and reported as `pending_restart` until the next start —
#: the host backend is built once, in `create_app`, and rebuilding a live one
#: would mean tearing down whatever window it is currently hosting.
RESTART_KEYS: tuple[SettingKey, ...] = ("office_native",)

#: The environment variable behind each overridable setting. Only knobs that
#: really exist are listed: the theme and voice have no variable today, so a
#: stored choice is the whole answer for them.
ENV_VARS: Mapping[SettingKey, str] = {"office_native": "WORKBENCH_OFFICE_NATIVE"}

#: The zero-telemetry position, as the server states it. A sentence rather than
#: a switch — see `models/settings.py`.
TELEMETRY_DETAIL = (
    "Off, and there is nothing to turn on. Workbench sends no usage data, "
    "no crash reports and no file contents anywhere; your work and your "
    "conversations stay on this machine."
)


class ProcessConfig(BaseModel):
    """What this process was configured with outside the app.

    ``None`` means "nobody said", and the stored choice decides. One field per
    setting that has an environment variable; adding a second overridable knob
    is a field here, a line in :meth:`SettingsService.effective` and a row in
    :meth:`SettingsService._overrides`.
    """

    office_native: OfficeNativeMode | None = None


class SettingsService:
    """Reads and writes ``<app data>/settings.json``, and resolves precedence.

    Reads never raise (see the module note); a write raises ``OSError`` on a
    real failure so the router can turn it into a status the client can act on.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        config: ProcessConfig | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._path = (directory or app_data_dir()) / SETTINGS_FILE
        self._config = config or ProcessConfig()
        self._environ = os.environ if environ is None else environ
        # What this process is actually running with, captured once. Compared
        # against the live effective values to answer "does this need a
        # restart?" honestly, rather than by hard-coding "office always does".
        self._launched = self.effective()

    @property
    def path(self) -> Path:
        return self._path

    # ---- reading -----------------------------------------------------------

    def load(self) -> tuple[WorkbenchSettings, str | None]:
        """The stored choices, and the reason they are the defaults if they are."""
        try:
            if self._path.stat().st_size > MAX_FILE_BYTES:
                return self._defaults(f"larger than {MAX_FILE_BYTES // 1024} KB — ignored")
            raw = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return WorkbenchSettings(), None  # nothing chosen yet: the defaults
        except OSError as err:
            return self._defaults(f"unreadable: {err.strerror or err}")
        try:
            parsed = json.loads(raw)
        except ValueError as err:
            return self._defaults(f"not valid JSON ({err.args[0] if err.args else err})")
        try:
            document = SettingsFile.model_validate(parsed)
        except ValidationError:
            return self._defaults("not a settings document this version understands")
        if document.version != SETTINGS_VERSION:
            return self._defaults(f"written by settings version {document.version} — ignored")
        return document.settings, None

    def stored(self) -> WorkbenchSettings:
        """Just the choices; the reason is for the endpoint, not for callers."""
        return self.load()[0]

    def effective(self) -> WorkbenchSettings:
        """What is in force: the stored choices, with process config on top."""
        settings = self.stored()
        if self._config.office_native is not None:
            settings = settings.model_copy(update={"office_native": self._config.office_native})
        return settings

    def state(self) -> SettingsState:
        """The whole picture the Settings panel renders."""
        stored, problem = self.load()
        effective = self.effective()
        return SettingsState(
            stored=stored,
            effective=effective,
            overrides=self._overrides(),
            pending_restart=[
                key
                for key in RESTART_KEYS
                if getattr(effective, key) != getattr(self._launched, key)
            ],
            path=str(self._path),
            telemetry=TelemetryStance(detail=TELEMETRY_DETAIL),
            problem=problem,
        )

    def _overrides(self) -> list[SettingOverride]:
        if self._config.office_native is None:
            return []
        variable = ENV_VARS["office_native"]
        # Named honestly: the variable is only *named* when it is really there.
        # The same field can reach the process another way (a config file, an
        # embedding host), and telling a user to unset a variable they never
        # exported is worse than saying where it came from in the vaguer, true way.
        detail = (
            f"Set by {variable} for this launch — the stored choice is kept and used again "
            "once that is unset."
            if variable in self._environ
            else "Set outside the app for this launch."
        )
        return [
            SettingOverride(
                key="office_native",
                value=self._config.office_native,
                detail=detail,
            )
        ]

    # ---- writing -----------------------------------------------------------

    def save(self, settings: WorkbenchSettings) -> SettingsState:
        """Persist the choices whole and answer with the new state.

        The client holds the whole document and PUTs it entire — the layouts
        precedent — so this is idempotent and there is no partial-update path to
        get wrong. Answering with :meth:`state` rather than an ack is what lets
        the panel show an override or a pending restart without a second call.
        """
        data = SettingsFile(settings=settings).model_dump_json().encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            self._replace(tmp_name)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        log.info("settings.saved", path=str(self._path))
        return self.state()

    def _replace(self, tmp_name: str) -> None:
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_name, self._path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                log.debug("settings.replace_retry", path=str(self._path), attempt=attempt + 1)
                time.sleep(REPLACE_BACKOFF_S)

    def _defaults(self, reason: str) -> tuple[WorkbenchSettings, str]:
        log.warning("settings.unusable", path=str(self._path), reason=reason)
        return WorkbenchSettings(), f"{SETTINGS_FILE}: {reason}"
