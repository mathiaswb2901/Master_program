"""Layout persistence: the file, and the two endpoints over it.

The theme of every test here is that a bad ``.workbench/layouts.json`` costs the
user their arrangement and nothing else. There is no input this service may
answer with a 500, because the one thing worse than forgetting a layout is a
window that will not open.
"""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from workbench_server.config import Settings
from workbench_server.models.layouts import (
    MAX_FILE_BYTES,
    MAX_SAVED_LAYOUTS,
    LayoutsResponse,
    LayoutsState,
    NamedLayout,
)
from workbench_server.services.layouts import LAYOUTS_PATH, LayoutsService, LayoutTooLargeError

GRID = {"grid": {"root": {"type": "branch", "data": []}}, "panels": {}}


@pytest.fixture
def service(tmp_path: Path) -> LayoutsService:
    return LayoutsService(tmp_path)


def _write(tmp_path: Path, text: str) -> None:
    path = tmp_path / ".workbench" / "layouts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---- the file ---------------------------------------------------------------


def test_nothing_saved_is_not_a_problem(service: LayoutsService) -> None:
    loaded = service.load()
    assert loaded.problem is None
    assert loaded.state == LayoutsState()


def test_round_trips_the_document_verbatim(service: LayoutsService, tmp_path: Path) -> None:
    state = LayoutsState(
        current=GRID, current_name="Review", saved=[NamedLayout(name="Review", state=GRID)]
    )
    service.save(state)
    assert (tmp_path / ".workbench" / "layouts.json").is_file()
    assert service.load() == LayoutsResponse(state=state)


def test_a_save_replaces_the_previous_document(service: LayoutsService) -> None:
    service.save(LayoutsState(current=GRID, saved=[NamedLayout(name="Review", state=GRID)]))
    service.save(LayoutsState(current=None, saved=[]))
    assert service.load().state == LayoutsState()


def test_corrupt_json_falls_back_to_the_default_with_a_reason(
    service: LayoutsService, tmp_path: Path
) -> None:
    _write(tmp_path, "{not json at all")
    loaded = service.load()
    assert loaded.state == LayoutsState()
    assert loaded.problem is not None
    assert LAYOUTS_PATH in loaded.problem
    assert "not valid JSON" in loaded.problem


def test_json_of_the_wrong_shape_falls_back_too(service: LayoutsService, tmp_path: Path) -> None:
    """Written by another version, or edited by hand. Still not a crash."""
    _write(tmp_path, json.dumps({"saved": "not a list"}))
    loaded = service.load()
    assert loaded.state == LayoutsState()
    assert loaded.problem is not None
    assert "understands" in loaded.problem


def test_a_byte_order_mark_is_not_corruption(service: LayoutsService, tmp_path: Path) -> None:
    """Windows-first. Notepad and ``Set-Content -Encoding utf8`` both write a BOM,
    and a user *will* open this file — losing an arrangement to three invisible
    bytes is not a fallback anybody can act on."""
    path = tmp_path / ".workbench" / "layouts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"current": GRID, "saved": []}), encoding="utf-8-sig")
    loaded = service.load()
    assert loaded.problem is None
    assert loaded.state.current == GRID


def test_an_oversized_file_is_ignored(service: LayoutsService, tmp_path: Path) -> None:
    _write(tmp_path, " " * (MAX_FILE_BYTES + 1))
    loaded = service.load()
    assert loaded.state == LayoutsState()
    assert loaded.problem is not None
    assert "larger than" in loaded.problem


def test_a_directory_where_the_file_should_be_is_reported(
    service: LayoutsService, tmp_path: Path
) -> None:
    (tmp_path / ".workbench" / "layouts.json").mkdir(parents=True)
    loaded = service.load()
    assert loaded.state == LayoutsState()
    assert loaded.problem is not None


def test_refuses_to_persist_an_oversized_document(service: LayoutsService) -> None:
    huge = LayoutsState(current={"blob": "x" * (MAX_FILE_BYTES + 1)})
    with pytest.raises(LayoutTooLargeError):
        service.save(huge)
    # And it left nothing behind: the previous document (none) is still current.
    assert service.load().state == LayoutsState()


def test_a_failed_save_leaves_no_temp_file(service: LayoutsService, tmp_path: Path) -> None:
    with pytest.raises(LayoutTooLargeError):
        service.save(LayoutsState(current={"blob": "x" * (MAX_FILE_BYTES + 1)}))
    workbench = tmp_path / ".workbench"
    assert not workbench.exists() or list(workbench.iterdir()) == []


# ---- the endpoints ----------------------------------------------------------


EMPTY: dict[str, object] = {"current": None, "current_name": None, "saved": []}


async def test_get_returns_the_empty_document_on_a_fresh_workspace(client: AsyncClient) -> None:
    response = await client.get("/api/layouts")
    assert response.status_code == 200
    assert response.json() == {"state": EMPTY, "problem": None}


async def test_put_then_get_round_trips(client: AsyncClient) -> None:
    body = {
        "current": GRID,
        "current_name": "Review",
        "saved": [{"name": "Review", "state": GRID}],
    }
    assert (await client.put("/api/layouts", json=body)).status_code == 200
    assert (await client.get("/api/layouts")).json()["state"] == body


async def test_get_reports_a_corrupt_file_instead_of_failing(
    client: AsyncClient, settings: Settings
) -> None:
    _write(settings.resolved_workspace(), "]]not json[[")
    response = await client.get("/api/layouts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == EMPTY
    assert payload["problem"] is not None


async def test_too_many_saved_layouts_is_a_422(client: AsyncClient) -> None:
    body = {
        **EMPTY,
        "saved": [{"name": f"L{i}", "state": GRID} for i in range(MAX_SAVED_LAYOUTS + 1)],
    }
    assert (await client.put("/api/layouts", json=body)).status_code == 422


async def test_an_empty_layout_name_is_a_422(client: AsyncClient) -> None:
    body = {**EMPTY, "saved": [{"name": "", "state": GRID}]}
    assert (await client.put("/api/layouts", json=body)).status_code == 422


async def test_an_oversized_document_is_a_413(client: AsyncClient) -> None:
    body = {**EMPTY, "current": {"blob": "x" * (MAX_FILE_BYTES + 1)}}
    assert (await client.put("/api/layouts", json=body)).status_code == 413
