"""Shared fixtures. Tests build Settings explicitly — never from ambient env."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services import shortcuts as shortcuts_service

# Every environment variable Settings reads, derived from its own fields so a new
# setting is covered the day it is added. Settings sets extra="ignore", so any other
# WORKBENCH_* variable provably cannot reach it — this set is exactly complete.
_SETTINGS_ENV_KEYS = frozenset(
    f"{Settings.model_config['env_prefix']}{field}".upper() for field in Settings.model_fields
)


@pytest.fixture(autouse=True)
def hermetic_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's own WORKBENCH_* settings from every test.

    Autouse on purpose: CLAUDE.md tells developers to export
    WORKBENCH_ONLYOFFICE_URL and WORKBENCH_ONLYOFFICE_JWT_SECRET to run the app,
    so a bare ``Settings()`` in a test picked up whatever that machine had — the
    suite passed in CI and failed for the developer who followed the setup
    instructions. Tests that want a variable still set it themselves with
    ``monkeypatch.setenv``; this only strips what leaked in from the shell.

    Deliberately does not touch harness gates like WORKBENCH_LIVE_AGENT and
    WORKBENCH_PACKAGING_TEST, which are opt-ins the developer means to be ambient.
    """
    for key in list(os.environ):
        if key.upper() in _SETTINGS_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(workspace_root=tmp_path)


@pytest.fixture(autouse=True)
def global_shortcuts_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user-global shortcuts file at a throwaway home.

    Autouse on purpose: without it every test would read whatever the developer
    running them keeps in ``~/.workbench/shortcuts.md``. Lives outside the
    workspace root, like the real one.
    """
    path = tmp_path.parent / f"{tmp_path.name}-home" / ".workbench" / "shortcuts.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(shortcuts_service, "global_shortcuts_path", lambda: path)
    return path


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
