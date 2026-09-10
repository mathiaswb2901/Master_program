"""The settings document, its precedence, and the endpoints over it.

Four properties are what this file exists to hold, because each is a promise
made somewhere else in the repo:

* the document lives under the machine's **app data dir** and nowhere near
  ``~/.claude`` (the security posture in ``CLAUDE.md``) or the workspace;
* a **bad file never costs more than the preferences** — defaults plus a
  sentence saying why, never an exception and never a server that will not start;
* an **environment override wins and is reported**, and the stored choice
  survives it, so unsetting the variable puts the user's own answer back;
* **telemetry is not a setting** — the model has no shape in which it is on, so
  no request can turn it on.
"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.office_host import OfficeNativeMode
from workbench_server.models.settings import (
    MAX_FILE_BYTES,
    SETTINGS_VERSION,
    SettingsFile,
    WorkbenchSettings,
)
from workbench_server.services.office_host import OfficeHostService
from workbench_server.services.settings import (
    SETTINGS_FILE,
    ProcessConfig,
    SettingsService,
)


def _service(
    directory: Path,
    *,
    config: ProcessConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> SettingsService:
    return SettingsService(directory, config=config, environ=environ)


def _write(directory: Path, text: str) -> Path:
    path = directory / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@asynccontextmanager
async def _launched(settings: Settings) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """A server started the way a restart starts one, and a client on it.

    The `client` fixture builds its app from the `settings` fixture, so a test
    that needs a *particular* launch — a stored document already on disk, an
    environment variable exported — has to build its own. This is the whole
    difference between reading `_detail` directly and reading it through the
    endpoints a user's window and the Setup panel actually call.
    """
    app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Workbench-Token": app.state.auth_token},
    ) as client:
        yield app, client


# ---- defaults and the round trip -------------------------------------------


def test_missing_file_is_the_defaults_and_not_a_problem(tmp_path: Path) -> None:
    state = _service(tmp_path).state()
    assert state.stored == WorkbenchSettings()
    assert state.stored.theme == "system"
    assert state.stored.office_native == "auto"
    assert state.stored.voice_input is False
    # Nothing chosen yet is not a fault: `problem` is for a file we could not use.
    assert state.problem is None


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save(WorkbenchSettings(theme="light", office_native="off", voice_input=True))
    assert saved.stored.theme == "light"
    # A second service reading the same directory sees the same thing — the file
    # is the authority, not an in-memory cache.
    assert _service(tmp_path).stored() == WorkbenchSettings(
        theme="light", office_native="off", voice_input=True
    )


def test_saved_document_is_version_stamped(tmp_path: Path) -> None:
    _service(tmp_path).save(WorkbenchSettings(theme="dark"))
    written = json.loads((tmp_path / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert written["version"] == SETTINGS_VERSION
    assert written["settings"]["theme"] == "dark"


def test_document_lives_under_app_data_and_never_in_dot_claude(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save(WorkbenchSettings(theme="light"))
    assert service.path == tmp_path / SETTINGS_FILE
    assert service.path.is_file()
    # The one place this must never write. Asserted rather than described: the
    # global Claude scope carries hooks and permission rules the server keeps
    # out of sessions by default.
    assert ".claude" not in service.path.parts


# ---- a bad file costs the preferences and nothing else ---------------------


def test_unparseable_file_falls_back_to_defaults_with_a_reason(tmp_path: Path) -> None:
    _write(tmp_path, "{{{ not json")
    state = _service(tmp_path).state()
    assert state.stored == WorkbenchSettings()
    assert state.problem is not None
    assert "not valid JSON" in state.problem


def test_wrong_shape_falls_back_to_defaults_with_a_reason(tmp_path: Path) -> None:
    _write(tmp_path, json.dumps({"version": SETTINGS_VERSION, "settings": {"theme": "chartreuse"}}))
    state = _service(tmp_path).state()
    assert state.stored == WorkbenchSettings()
    assert state.problem is not None and "understands" in state.problem


def test_document_from_another_version_is_discarded_not_guessed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        json.dumps({"version": SETTINGS_VERSION + 7, "settings": {"theme": "light"}}),
    )
    state = _service(tmp_path).state()
    assert state.stored.theme == "system"
    assert state.problem is not None and "settings version" in state.problem


def test_oversized_file_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path, " " * (MAX_FILE_BYTES + 1))
    state = _service(tmp_path).state()
    assert state.stored == WorkbenchSettings()
    assert state.problem is not None and "larger than" in state.problem


def test_a_file_with_a_bom_still_parses(tmp_path: Path) -> None:
    # What Notepad (and `Set-Content -Encoding utf8`) leaves behind on Windows.
    document = SettingsFile(settings=WorkbenchSettings(theme="dark")).model_dump_json()
    (tmp_path / SETTINGS_FILE).write_text(document, encoding="utf-8-sig")
    assert _service(tmp_path).stored().theme == "dark"


def test_a_missing_field_arrives_at_its_default(tmp_path: Path) -> None:
    # A document written before a field existed must still load — that is what
    # keeps a settings file forward-compatible instead of disposable.
    _write(tmp_path, json.dumps({"version": SETTINGS_VERSION, "settings": {"theme": "light"}}))
    stored = _service(tmp_path).stored()
    assert stored.theme == "light"
    assert stored.voice_input is False


# ---- precedence: what the process was configured with wins -----------------


def test_env_configured_office_native_overrides_the_stored_choice(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        config=ProcessConfig(office_native="off"),
        environ={"WORKBENCH_OFFICE_NATIVE": "off"},
    )
    service.save(WorkbenchSettings(office_native="on"))
    state = service.state()
    # The stored choice is kept exactly as the user left it …
    assert state.stored.office_native == "on"
    # … and what is in force is the operator's.
    assert state.effective.office_native == "off"
    assert [o.key for o in state.overrides] == ["office_native"]
    assert state.overrides[0].value == "off"
    assert "WORKBENCH_OFFICE_NATIVE" in state.overrides[0].detail


def test_override_reached_the_process_another_way_names_no_variable(tmp_path: Path) -> None:
    # The same field can be configured without the environment variable being
    # present; telling the user to unset one they never exported would be a lie.
    state = _service(tmp_path, config=ProcessConfig(office_native="on"), environ={}).state()
    assert state.overrides[0].detail == "Set outside the app for this launch."


def test_no_override_when_nothing_was_configured(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save(WorkbenchSettings(office_native="on"))
    state = service.state()
    assert state.overrides == []
    assert state.effective.office_native == "on"


def test_a_stored_change_to_a_launch_only_setting_is_pending_restart(tmp_path: Path) -> None:
    service = _service(tmp_path)  # launched with the default "auto"
    state = service.save(WorkbenchSettings(office_native="on"))
    assert state.pending_restart == ["office_native"]
    # And a setting the running app applies itself is never pending.
    back = service.save(WorkbenchSettings(office_native="auto", theme="light"))
    assert back.pending_restart == []


def test_pending_restart_is_empty_at_launch(tmp_path: Path) -> None:
    _write(tmp_path, SettingsFile(settings=WorkbenchSettings(office_native="on")).model_dump_json())
    # A service that starts up on a document already saying "on" is running with
    # "on" — nothing is pending.
    assert _service(tmp_path).state().pending_restart == []


# ---- telemetry is a statement, not a switch --------------------------------


def test_telemetry_is_reported_off_with_a_sentence(tmp_path: Path) -> None:
    state = _service(tmp_path).state()
    assert state.telemetry.enabled is False
    assert state.telemetry.detail.strip() != ""


def test_the_put_body_has_no_telemetry_field_to_set(tmp_path: Path) -> None:
    # The stance is not part of what a client can write, so "turn it on" is not
    # a request the API can express.
    assert "telemetry" not in WorkbenchSettings.model_fields


# ---- the endpoints, end to end ---------------------------------------------


async def test_get_settings_answers_the_defaults(client: AsyncClient) -> None:
    response = await client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] == {
        "theme": "system",
        "office_native": "auto",
        "voice_input": False,
        # One defaulted field is all a new knob costs a stored document — no
        # SETTINGS_VERSION bump, so nobody's theme is discarded to add one.
        "validation_retention_days": 90,
    }
    assert body["effective"] == body["stored"]
    assert body["overrides"] == []
    assert body["telemetry"]["enabled"] is False
    assert body["problem"] is None


async def test_put_settings_persists_and_answers_the_new_state(client: AsyncClient) -> None:
    response = await client.put(
        "/api/settings",
        json={"theme": "light", "office_native": "on", "voice_input": True},
    )
    assert response.status_code == 200
    assert response.json()["stored"]["theme"] == "light"
    # Read back through a second request: the document, not a response cache.
    again = await client.get("/api/settings")
    assert again.json()["stored"] == {
        "theme": "light",
        "office_native": "on",
        "voice_input": True,
        # Untouched by a PUT that did not name it: the client sends the whole
        # document, and a field it omits arrives at its default rather than
        # failing to parse (models/settings.py).
        "validation_retention_days": 90,
    }
    # Launch-only: the running server is still on what it started with, and says so.
    assert again.json()["pending_restart"] == ["office_native"]


async def test_put_rejects_a_value_outside_the_schema(client: AsyncClient) -> None:
    response = await client.put("/api/settings", json={"theme": "chartreuse"})
    assert response.status_code == 422


async def test_put_ignores_an_unknown_field_rather_than_storing_it(client: AsyncClient) -> None:
    # Notably `telemetry`: pydantic drops it, so a client that tried could not
    # smuggle a stance into the document.
    response = await client.put("/api/settings", json={"theme": "dark", "telemetry": True})
    assert response.status_code == 200
    assert "telemetry" not in response.json()["stored"]


@pytest.mark.parametrize("configured", ["on", "off"])
def test_create_app_builds_the_office_host_from_the_configured_setting(
    tmp_path: Path, app_data_root: Path, configured: OfficeNativeMode
) -> None:
    # The wiring this whole feature turns on. An explicitly configured value is
    # what the host is built with, whatever the stored document says — and the
    # capabilities endpoint (the office authority) is where that shows up.
    SettingsService(app_data_root).save(WorkbenchSettings(office_native="auto"))
    app = create_app(Settings(workspace_root=tmp_path, office_native=configured))
    host: OfficeHostService = app.state.office_host
    assert host.capabilities(False).office_native == configured


def test_create_app_builds_the_office_host_from_the_stored_setting(
    tmp_path: Path, app_data_root: Path
) -> None:
    # Nothing configured: the user's own stored choice is what the server runs.
    SettingsService(app_data_root).save(WorkbenchSettings(office_native="off"))
    app = create_app(Settings(workspace_root=tmp_path))
    host: OfficeHostService = app.state.office_host
    assert host.capabilities(False).office_native == "off"
    service: SettingsService = app.state.settings_service
    assert service.state().overrides == []


# ---- "off" names the mechanism that really turned it off -------------------
#
# Making the setting real (above) also made `off` reachable from the panel, and
# a reason sentence that hard-codes the environment variable then sends a user
# hunting something they never exported. These read the two surfaces that echo
# it — `GET /api/office/capabilities`, which the Office panel degrades from, and
# the Setup panel's Office row, which is `caps.detail` verbatim
# (`services/setup.py`) — through a real launch rather than calling `_detail`.


async def test_off_from_the_panel_blames_settings_and_not_an_unset_variable(
    tmp_path: Path, app_data_root: Path
) -> None:
    SettingsService(app_data_root).save(WorkbenchSettings(office_native="off"))
    async with _launched(Settings(workspace_root=tmp_path)) as (_, client):
        caps = (await client.get("/api/office/capabilities")).json()
        status = (await client.get("/api/setup/status")).json()
    assert caps["office_native"] == "off"
    assert caps["native_hosting"] is False
    # The variable is not set in this process (conftest scrubs WORKBENCH_*), so
    # naming it would be false — and it is the string a user is told to go fix.
    assert "WORKBENCH_OFFICE_NATIVE" not in caps["detail"]
    assert "Settings" in caps["detail"]
    office = next(check for check in status["checks"] if check["id"] == "office")
    assert office["detail"] == caps["detail"]


async def test_off_from_the_environment_still_names_the_variable(
    tmp_path: Path, app_data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator's launch decision, unchanged: the message that sends them to
    # the variable is right precisely when the variable is really there.
    monkeypatch.setenv("WORKBENCH_OFFICE_NATIVE", "off")
    SettingsService(app_data_root).save(WorkbenchSettings(office_native="on"))
    async with _launched(Settings(workspace_root=tmp_path, office_native="off")) as (_, client):
        caps = (await client.get("/api/office/capabilities")).json()
    assert caps["detail"] == "native hosting is off (WORKBENCH_OFFICE_NATIVE=off)"


async def test_off_configured_without_the_variable_names_no_variable(
    tmp_path: Path, app_data_root: Path
) -> None:
    # Configured outside the app by some other route (workbench.toml, an
    # embedding host). Same vaguer, true wording `SettingOverride.detail` uses:
    # telling someone to unset a variable they never exported is the same lie.
    SettingsService(app_data_root).save(WorkbenchSettings(office_native="on"))
    async with _launched(Settings(workspace_root=tmp_path, office_native="off")) as (_, client):
        caps = (await client.get("/api/office/capabilities")).json()
    assert "WORKBENCH_OFFICE_NATIVE" not in caps["detail"]
    assert "outside the app" in caps["detail"]


def test_the_source_of_a_setting_is_only_ever_stored_when_nothing_configured(
    tmp_path: Path,
) -> None:
    # The unit behind those three, for the keys that have no variable at all.
    plain = _service(tmp_path)
    assert plain.source_of("office_native") == "stored"
    assert plain.source_of("theme") == "stored"
    assert plain.source_of("voice_input") == "stored"
    configured = _service(
        tmp_path,
        config=ProcessConfig(office_native="off"),
        environ={"WORKBENCH_OFFICE_NATIVE": "off"},
    )
    assert configured.source_of("office_native") == "environment"
    # Only the key that was configured — a variable for one knob says nothing
    # about the others.
    assert configured.source_of("theme") == "stored"
    assert (
        _service(tmp_path, config=ProcessConfig(office_native="off"), environ={}).source_of(
            "office_native"
        )
        == "external"
    )
