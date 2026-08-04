"""Shared fixtures. Tests build Settings explicitly — never from ambient env."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services import shortcuts as shortcuts_service


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
