"""Shared fixtures. Tests build Settings explicitly — never from ambient env."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(workspace_root=tmp_path)


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
