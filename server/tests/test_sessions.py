"""The named-session store: CRUD, the never-raise read, and that it is global.

Mirrors the recents tests in ``test_workspaces.py`` — the store copies
``RecentsStore``'s discipline, so it inherits the same obligations: a round trip
survives a reload, a bad file costs the list and not the server, the write is
atomic, the caps bind, and the load-bearing one here — a workspace switch does
**not** re-root the store, because a session is data about the user, queryable
from any project.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.sessions import (
    MAX_FILE_BYTES,
    MAX_NAMED_SESSIONS,
    SESSIONS_VERSION,
    CreateNamedSessionRequest,
    UpdateNamedSessionRequest,
)
from workbench_server.services.sessions import (
    SESSIONS_FILE,
    SessionNotFoundError,
    SessionsFullError,
    SessionsStore,
    SessionTooLargeError,
)


def _create(name: str, workspace: str) -> CreateNamedSessionRequest:
    return CreateNamedSessionRequest(name=name, workspace=workspace)


# ---- the store, directly ----------------------------------------------------


def test_create_list_update_delete_round_trip(tmp_path: Path) -> None:
    store = SessionsStore(tmp_path / "appdata")
    ws = str(tmp_path / "project")

    created = store.create(_create("morning bid run", ws))
    assert created.id
    assert created.name == "morning bid run"
    assert created.created_at == created.last_attached_at

    listed = store.entries()
    assert [s.id for s in listed] == [created.id]

    updated = store.update(
        created.id,
        UpdateNamedSessionRequest(name="afternoon bid run", workspace=ws, arrangement={"grid": 1}),
    )
    assert updated.id == created.id  # identity is immutable
    assert updated.created_at == created.created_at  # so is the birth time
    assert updated.name == "afternoon bid run"
    assert updated.arrangement == {"grid": 1}
    assert updated.last_attached_at >= created.last_attached_at  # an update is an attach

    store.delete(created.id)
    assert store.entries() == []


def test_entries_are_most_recently_attached_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A monotonic fake clock, because the real one's resolution on Windows can
    # tie three writes in the same tick — the ordering the store promises is by
    # attach time, so the test must give each write a distinct one.
    ticks = iter(range(100, 200))
    monkeypatch.setattr("workbench_server.services.sessions.time.time", lambda: float(next(ticks)))
    store = SessionsStore(tmp_path / "appdata")
    ws = str(tmp_path / "p")
    first = store.create(_create("first", ws))
    second = store.create(_create("second", ws))
    assert [s.id for s in store.entries()] == [second.id, first.id]
    # Touch `first` so it becomes the most recently attached.
    store.update(first.id, UpdateNamedSessionRequest(name="first", workspace=ws))
    assert [s.id for s in store.entries()] == [first.id, second.id]


def test_update_and_delete_raise_on_an_unknown_id(tmp_path: Path) -> None:
    store = SessionsStore(tmp_path / "appdata")
    with pytest.raises(SessionNotFoundError):
        store.update("nope", UpdateNamedSessionRequest(name="x", workspace="w"))
    with pytest.raises(SessionNotFoundError):
        store.delete("nope")


def test_entries_filter_by_workspace_case_insensitively(tmp_path: Path) -> None:
    store = SessionsStore(tmp_path / "appdata")
    a = str(tmp_path / "Alpha")
    b = str(tmp_path / "Beta")
    store.create(_create("in alpha", a))
    store.create(_create("in beta", b))
    assert [s.name for s in store.entries(a)] == ["in alpha"]
    # A window rooted at a differently-cased spelling of the same path still
    # sees its session — Windows paths are case-insensitive.
    assert {s.name for s in store.entries(a.upper())} == {"in alpha"}
    # No workspace filter returns every session across every project.
    assert len(store.entries()) == 2


def test_a_session_survives_a_reload_and_is_written_to_app_data(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    ws = str(tmp_path / "project")
    created = SessionsStore(appdata).create(_create("kept", ws))

    assert (appdata / SESSIONS_FILE).is_file()
    document = json.loads((appdata / SESSIONS_FILE).read_text(encoding="utf-8"))
    assert document["version"] == SESSIONS_VERSION

    reloaded = SessionsStore(appdata).entries()
    assert [s.id for s in reloaded] == [created.id]


def test_the_write_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    store = SessionsStore(appdata)
    store.create(_create("s", str(tmp_path / "p")))
    # tmp files are named `.sessions.json.*.tmp` — none may survive a completed write.
    leftovers = list(appdata.glob(f".{SESSIONS_FILE}.*"))
    assert leftovers == []


# ---- the never-raise read ---------------------------------------------------


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("not json at all", "not a sessions document"),
        ('{"version": 999, "sessions": []}', "another version"),
        ('{"version": 1, "sessions": [{"id": 5}]}', "not a sessions document"),
        (
            '{"version": 1, "sessions": [{"id": "a", "name": "x"',
            "not a sessions document",
        ),  # truncated
    ],
)
def test_an_unusable_sessions_file_costs_the_list_and_nothing_else(
    tmp_path: Path, body: str, fragment: str
) -> None:
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    (appdata / SESSIONS_FILE).write_text(body, encoding="utf-8")
    store = SessionsStore(appdata)
    assert store.entries() == []
    assert store.problem is not None and fragment in store.problem
    # And it recovers: a create overwrites the unusable document.
    created = store.create(_create("fresh", str(tmp_path / "p")))
    assert [s.id for s in SessionsStore(appdata).entries()] == [created.id]


def test_an_oversized_file_is_ignored_not_read(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    (appdata / SESSIONS_FILE).write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    store = SessionsStore(appdata)
    assert store.entries() == []
    assert store.problem is not None and "larger than" in store.problem


# ---- the caps ---------------------------------------------------------------


def test_the_store_refuses_more_than_the_cap(tmp_path: Path) -> None:
    store = SessionsStore(tmp_path / "appdata")
    ws = str(tmp_path / "p")
    for n in range(MAX_NAMED_SESSIONS):
        store.create(_create(f"s{n}", ws))
    assert len(store.entries()) == MAX_NAMED_SESSIONS
    with pytest.raises(SessionsFullError):
        store.create(_create("one too many", ws))


def test_an_over_large_arrangement_is_refused_before_it_is_written(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    store = SessionsStore(appdata)
    huge = {"blob": "x" * (MAX_FILE_BYTES + 1)}
    with pytest.raises(SessionTooLargeError):
        store.create(
            CreateNamedSessionRequest(name="huge", workspace=str(tmp_path / "p"), arrangement=huge)
        )
    # Nothing was written — the refusal is before disk is touched.
    assert not (appdata / SESSIONS_FILE).exists()


def test_a_name_over_sixty_chars_is_rejected_by_the_model() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CreateNamedSessionRequest(name="x" * 61, workspace="w")


# ---- through the API --------------------------------------------------------


async def test_crud_through_the_api(client: AsyncClient, tmp_path: Path) -> None:
    ws = str(tmp_path / "project")

    created = await client.post("/api/sessions", json={"name": "run", "workspace": ws})
    assert created.status_code == 200
    session_id = created.json()["id"]

    listed = await client.get("/api/sessions", params={"workspace": ws})
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()["sessions"]] == [session_id]
    assert listed.json()["problem"] is None

    updated = await client.put(
        f"/api/sessions/{session_id}",
        json={"name": "renamed", "workspace": ws, "arrangement": {"a": 1}},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "renamed"

    deleted = await client.delete(f"/api/sessions/{session_id}")
    assert deleted.status_code == 204
    assert (await client.get("/api/sessions", params={"workspace": ws})).json()["sessions"] == []


async def test_unknown_id_is_404_and_full_store_is_409(client: AsyncClient, tmp_path: Path) -> None:
    ws = str(tmp_path / "p")
    assert (
        await client.put("/api/sessions/nope", json={"name": "x", "workspace": ws})
    ).status_code == 404
    assert (await client.delete("/api/sessions/nope")).status_code == 404

    for n in range(MAX_NAMED_SESSIONS):
        assert (
            await client.post("/api/sessions", json={"name": f"s{n}", "workspace": ws})
        ).status_code == 200
    overflow = await client.post("/api/sessions", json={"name": "over", "workspace": ws})
    assert overflow.status_code == 409


async def test_the_session_list_is_not_re_rooted_by_a_workspace_switch(tmp_path: Path) -> None:
    """The load-bearing assertion: a session created while rooted at alpha is
    still there — queryable by its own workspace — after the server switches to
    beta. The store is data about the user, not about the current project."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    (alpha / ".workbench").mkdir(parents=True)
    (beta / ".workbench").mkdir(parents=True)

    app = create_app(Settings(workspace_root=alpha, app_data_root=tmp_path / "appdata"))
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"name": "alpha work", "workspace": str(alpha)})
        assert created.status_code == 200
        session_id = created.json()["id"]

        # Switch the whole server to beta.
        assert client.post("/api/workspace/switch", json={"path": str(beta)}).status_code == 200

        # The session still exists, unchanged, queried by its own workspace.
        after = client.get("/api/sessions", params={"workspace": str(alpha)}).json()
        assert [s["id"] for s in after["sessions"]] == [session_id]
        # And it is not filed under beta — the switch did not re-root the store.
        assert client.get("/api/sessions", params={"workspace": str(beta)}).json()["sessions"] == []
        # The unfiltered list sees it from anywhere.
        assert session_id in {s["id"] for s in client.get("/api/sessions").json()["sessions"]}
