"""Shared fixtures. Tests build Settings explicitly — never from ambient env."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services import shortcuts as shortcuts_service
from workbench_server.services import workspaces as workspaces_service


@pytest.fixture(autouse=True)
def no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every ``WORKBENCH_*`` variable for the duration of a test.

    ``Settings()`` reads the environment, so without this the suite asserts
    against whatever the developer running it exported — and CLAUDE.md tells
    this project's developers to export ``WORKBENCH_ONLYOFFICE_URL`` and
    ``WORKBENCH_ONLYOFFICE_JWT_SECRET`` for Office editing, which turns Office
    on and fails the tests that assert it is off by default. Same reason
    `playwright.config.ts` strips the prefix before starting the E2E backend: a
    run on a working machine must be the run a bare CI runner gets. A test that
    wants a setting passes it to ``Settings(...)``.
    """
    for key in [k for k in os.environ if k.startswith("WORKBENCH_")]:
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


@pytest.fixture(autouse=True)
def app_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point machine-local state (the recent-workspaces list) at a throwaway dir.

    Autouse for the same reason ``global_shortcuts_file`` is: the recents store
    falls back to the real ``%LOCALAPPDATA%\\Workbench`` when nothing names a
    directory, so without this a test run would read — and *rewrite* — the
    developer's own list of projects. Outside the workspace root, like the real
    one, because that is the property the feature depends on.
    """
    path = tmp_path.parent / f"{tmp_path.name}-appdata"
    monkeypatch.setattr(workspaces_service, "app_data_dir", lambda: path)
    return path


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
