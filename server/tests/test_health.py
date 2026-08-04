from pathlib import Path

from httpx import AsyncClient

from workbench_server import __version__
from workbench_server.config import Settings


async def test_health_reports_version_and_workspace(
    client: AsyncClient, settings: Settings
) -> None:
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert Path(body["workspace"]) == settings.resolved_workspace()
