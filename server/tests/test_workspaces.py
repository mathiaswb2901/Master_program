"""The workspace switcher: the root swap, the recent list, and the jail.

The load-bearing assertion in this file is the last one in
``test_switch_rejails_and_forgets_the_old_workspace``: after a switch, a path
from the *old* workspace is refused by the same rule that has always refused
``../``. Everything else here exists so that assertion cannot be true by
accident — a tree that still lists the old project, a watcher still publishing
its paths, or a layouts file still read from it would each be a way for the app
to keep serving a workspace the user has left.
"""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.files import FileChangedEvent
from workbench_server.models.workspaces import MAX_RECENT_WORKSPACES, RECENTS_VERSION
from workbench_server.services.event_bus import EventBus
from workbench_server.services.layouts import LayoutsService
from workbench_server.services.provenance import ProvenanceService
from workbench_server.services.shortcuts import ShortcutsService
from workbench_server.services.watcher import Watcher
from workbench_server.services.workspace import PathOutsideWorkspaceError, Workspace
from workbench_server.services.workspaces import (
    RECENTS_FILE,
    RecentsStore,
    WorkspaceRefusedError,
    WorkspaceService,
    validate_root,
)


def _two_projects(tmp_path: Path) -> tuple[Path, Path]:
    """Two workspaces whose contents cannot be confused for each other."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    (alpha / ".workbench").mkdir(parents=True)
    (beta / ".workbench").mkdir(parents=True)
    (alpha / "only-in-alpha.txt").write_bytes(b"a\n")
    (beta / "only-in-beta.txt").write_bytes(b"b\n")
    return alpha, beta


def _files_in(response: object) -> list[str]:
    """File names from a `GET /api/files/dir` response — the `.workbench` folder
    both fixtures carry is not what any of these assertions are about."""
    payload = response.json()  # type: ignore[attr-defined]
    return [entry["name"] for entry in payload["entries"] if entry["kind"] == "file"]


def _service(
    workspace: Workspace,
    bus: EventBus,
    *,
    recents_dir: Path,
    sync: list[object] | None = None,
    asyncs: list[object] | None = None,
) -> WorkspaceService:
    return WorkspaceService(
        workspace,
        bus,
        # `list[object]` at the call site, because a test's fakes satisfy the
        # Protocol structurally and mypy is happier being told that once here
        # than at every construction below.
        sync_rootables=sync or [],  # type: ignore[arg-type]
        async_rootables=asyncs or [],  # type: ignore[arg-type]
        explicit=False,
        recents=RecentsStore(recents_dir),
    )


# ---- validation -------------------------------------------------------------


def test_validate_root_accepts_a_readable_directory(tmp_path: Path) -> None:
    assert validate_root(tmp_path) == tmp_path.resolve()


def test_validate_root_refuses_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceRefusedError, match="does not exist"):
        validate_root(tmp_path / "nope")


def test_validate_root_refuses_a_file(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_bytes(b"x")
    with pytest.raises(WorkspaceRefusedError, match="is a file, not a folder"):
        validate_root(target)


def test_validate_root_refuses_a_directory_it_cannot_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable is asserted through the call that actually fails.

    A real ACL denial is not reproducible in a portable test (and `os.access` on
    Windows reports the read-only *attribute*, not the ACL — which is exactly why
    `validate_root` lists the directory instead of asking permission). So the
    listing is made to fail the way the OS would fail it, and the assertion is
    that the refusal carries the OS's own sentence rather than a generic one.
    """

    def deny(_path: Path) -> object:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr("workbench_server.services.workspaces.os.scandir", deny)
    with pytest.raises(WorkspaceRefusedError, match="cannot be read: Access is denied"):
        validate_root(tmp_path)


# ---- recents ----------------------------------------------------------------


def test_recents_are_most_recent_first_deduped_and_capped(tmp_path: Path) -> None:
    store = RecentsStore(tmp_path / "appdata")
    roots = [tmp_path / f"p{n}" for n in range(MAX_RECENT_WORKSPACES + 3)]
    for root in roots:
        root.mkdir()
        store.record(root)
    entries = store.entries()
    assert len(entries) == MAX_RECENT_WORKSPACES
    assert [entry.path for entry in entries] == [str(root) for root in reversed(roots)][
        :MAX_RECENT_WORKSPACES
    ]
    # Re-opening one that is already in the list moves it, never duplicates it.
    store.record(roots[0])
    entries = store.entries()
    assert entries[0].path == str(roots[0])
    assert [entry.path for entry in entries].count(str(roots[0])) == 1


def test_recents_dedupe_ignores_windows_path_case(tmp_path: Path) -> None:
    """`C:\\Work` and `c:\\work` are one folder, and must be one row."""
    store = RecentsStore(tmp_path / "appdata")
    root = tmp_path / "Project"
    root.mkdir()
    store.record(root)
    store.record(Path(str(root).upper()))
    # Case-insensitive filesystems (Windows) collapse the two; case-sensitive
    # ones are genuinely two directories, so the assertion is on the platform's
    # own answer rather than on a hard-coded count.
    import os

    expected = 1 if os.path.normcase(str(root)) == os.path.normcase(str(root).upper()) else 2
    assert len(store.entries()) == expected


def test_recents_report_a_folder_that_is_gone_without_dropping_it(tmp_path: Path) -> None:
    store = RecentsStore(tmp_path / "appdata")
    root = tmp_path / "removed"
    root.mkdir()
    store.record(root)
    root.rmdir()
    entry = store.entries()[0]
    assert entry.path == str(root)
    assert entry.exists is False


def test_recents_survive_a_reload_and_are_written_to_app_data(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    root = tmp_path / "kept"
    root.mkdir()
    RecentsStore(appdata).record(root)
    assert (appdata / RECENTS_FILE).is_file()
    document = json.loads((appdata / RECENTS_FILE).read_text(encoding="utf-8"))
    assert document["version"] == RECENTS_VERSION
    assert RecentsStore(appdata).entries()[0].path == str(root)


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("not json at all", "not a recents document"),
        ('{"version": 999, "recents": []}', "another version"),
        ('{"version": 1, "recents": [{"path": 5}]}', "not a recents document"),
    ],
)
def test_an_unusable_recents_file_costs_the_history_and_nothing_else(
    tmp_path: Path, body: str, fragment: str
) -> None:
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    (appdata / RECENTS_FILE).write_text(body, encoding="utf-8")
    store = RecentsStore(appdata)
    assert store.entries() == []
    assert store.problem is not None and fragment in store.problem
    # And it recovers: recording overwrites the unusable document.
    root = tmp_path / "fresh"
    root.mkdir()
    store.record(root)
    assert RecentsStore(appdata).entries()[0].path == str(root)


# ---- the root swap ----------------------------------------------------------


async def test_switch_re_roots_every_service_that_copied_the_root(tmp_path: Path) -> None:
    alpha, beta = _two_projects(tmp_path)
    bus = EventBus()
    workspace = Workspace(alpha)
    layouts = LayoutsService(alpha)
    shortcuts = ShortcutsService(alpha, bus, global_path=tmp_path / "no-global.md")
    provenance = ProvenanceService(alpha, bus)
    watcher = Watcher(alpha, bus)
    service = _service(
        workspace,
        bus,
        recents_dir=tmp_path / "appdata",
        sync=[shortcuts, layouts, provenance],
        asyncs=[watcher],
    )

    (alpha / ".workbench" / "shortcuts.md").write_text(
        "## Alpha build\n\n```\nnpm run alpha\n```\n", encoding="utf-8"
    )
    (beta / ".workbench" / "shortcuts.md").write_text(
        "## Beta build\n\n```\nnpm run beta\n```\n", encoding="utf-8"
    )
    shortcuts.reload()
    assert [entry.name for entry in shortcuts.state().entries] == ["Alpha build"]

    state = await service.switch(str(beta))

    assert state.root == str(beta.resolve())
    assert state.name == "beta"
    assert state.explicit is True
    assert workspace.root == beta.resolve()
    assert layouts.path == beta.resolve() / ".workbench" / "layouts.json"
    assert [entry.name for entry in shortcuts.state().entries] == ["Beta build"]
    # The watcher is watching the new root, and its previous watch is finished.
    assert watcher._root == beta.resolve()
    await watcher.stop()


async def test_switch_clears_provenance_so_a_same_named_file_is_not_attributed(
    tmp_path: Path,
) -> None:
    """The failure this prevents: `src/main.py` in the project you just opened
    inheriting the claim an agent made on `src/main.py` in the one you left."""
    alpha, beta = _two_projects(tmp_path)
    bus = EventBus()
    provenance = ProvenanceService(alpha, bus)
    provenance.note_tool_use(
        session_id="s1",
        session_title="alpha work",
        folder=alpha,
        tool="Write",
        tool_input={"file_path": str(alpha / "shared.py")},
    )
    provenance.note_file_change(
        FileChangedEvent(path="shared.py", change="modified", hash=None, is_dir=False)
    )
    assert provenance.snapshot().entries != []

    service = _service(Workspace(alpha), bus, recents_dir=tmp_path / "appdata", sync=[provenance])
    await service.switch(str(beta))
    assert provenance.snapshot().entries == []


async def test_switch_to_the_current_root_is_a_no_op_that_still_succeeds(
    tmp_path: Path,
) -> None:
    alpha, _ = _two_projects(tmp_path)
    bus = EventBus()
    watcher = Watcher(alpha, bus)
    service = _service(Workspace(alpha), bus, recents_dir=tmp_path / "appdata", asyncs=[watcher])
    watcher.start()
    task = watcher._task
    state = await service.switch(str(alpha))
    assert state.root == str(alpha.resolve())
    # The watch was never torn down: arriving where we already are must not cost
    # a restart, which the user would see as the tree blinking.
    assert watcher._task is task
    await watcher.stop()


def _live_watches() -> list[asyncio.Task[None]]:
    """Every watch task still running on this loop, however it was started.

    Named rather than tracked through ``watcher._task``, because the failure this
    exists to catch is precisely a watch that **nothing holds a reference to**:
    an overwritten ``_task`` is a watch that ``stop`` can no longer reach and
    that keeps publishing the old project's paths on the shared bus forever.
    """
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "workspace-watcher" and not task.done()
    ]


async def test_two_windows_switching_at_once_cannot_half_switch_the_server(
    tmp_path: Path,
) -> None:
    """Two windows, one server, two switches in the same tick.

    A supported arrangement, not a hypothetical: the ``workspace_changed`` frame
    the switcher publishes exists *because* a second window can learn about a
    switch it did not ask for, and either window may be the one that asks.

    Re-rooting is a sequence — the jail synchronously, then an awaited restart of
    the watcher — so without a lock across the whole of it the two halves of two
    switches interleave: both windows' jail writes land, then both watcher
    restarts do, and they need not land in the same order. What that produces is
    a server whose path jail (every file, office and content endpoint) and whose
    watch (the tree the user is looking at) point at *different projects*, plus a
    watch on the workspace nobody is in that no ``stop`` can ever reach — a
    server that looks correct from either window and reads the wrong folder.
    """
    alpha, beta = _two_projects(tmp_path)
    gamma = tmp_path / "gamma"
    (gamma / ".workbench").mkdir(parents=True)
    (gamma / "only-in-gamma.txt").write_bytes(b"g\n")

    bus = EventBus()
    workspace = Workspace(alpha)
    watcher = Watcher(alpha, bus)
    service = _service(workspace, bus, recents_dir=tmp_path / "appdata", asyncs=[watcher])
    watcher.start()

    try:
        await asyncio.gather(service.switch(str(beta)), service.switch(str(gamma)))

        # Whichever window won is not the assertion — "last one wins" is a fine
        # answer and the loser's window is told by the bus. What must be true is
        # that the server agrees with *itself*.
        assert watcher._root == workspace.root
        assert workspace.root in {beta.resolve(), gamma.resolve()}
        assert service.root == workspace.root
        # One watch, and it is the one the watcher can still stop.
        live = _live_watches()
        assert live == [watcher._task]
    finally:
        await watcher.stop()
        for stray in _live_watches():
            stray.cancel()


async def test_a_refused_root_changes_nothing(tmp_path: Path) -> None:
    alpha, _ = _two_projects(tmp_path)
    bus = EventBus()
    workspace = Workspace(alpha)
    layouts = LayoutsService(alpha)
    service = _service(workspace, bus, recents_dir=tmp_path / "appdata", sync=[layouts])
    with pytest.raises(WorkspaceRefusedError):
        await service.switch(str(tmp_path / "does-not-exist"))
    assert workspace.root == alpha.resolve()
    assert layouts.path == alpha.resolve() / ".workbench" / "layouts.json"


async def test_switch_publishes_workspace_changed_on_the_bus(tmp_path: Path) -> None:
    alpha, beta = _two_projects(tmp_path)
    bus = EventBus()
    queue = bus.subscribe()
    service = _service(Workspace(alpha), bus, recents_dir=tmp_path / "appdata")
    await service.switch(str(beta))
    event = await asyncio.wait_for(queue.get(), timeout=2)
    assert event.model_dump() == {
        "type": "workspace_changed",
        "root": str(beta.resolve()),
        "name": "beta",
    }


async def test_switch_records_both_workspaces_most_recent_first(tmp_path: Path) -> None:
    alpha, beta = _two_projects(tmp_path)
    bus = EventBus()
    service = _service(Workspace(alpha), bus, recents_dir=tmp_path / "appdata")
    service.start()
    state = await service.switch(str(beta))
    assert [entry.path for entry in state.recents] == [str(beta.resolve()), str(alpha.resolve())]
    assert state.recents[0].opened_at >= state.recents[1].opened_at
    assert state.recents[0].opened_at <= time.time() + 1


# ---- through the API --------------------------------------------------------


async def test_switch_rejails_and_forgets_the_old_workspace(
    client: AsyncClient, tmp_path: Path
) -> None:
    """The integration assertion: what the tree returns follows the switch, and
    a path from the workspace we left is refused by the jail."""
    alpha, beta = _two_projects(tmp_path)
    # `settings` roots the app at tmp_path; move it to alpha first.
    moved = await client.post("/api/workspace/switch", json={"path": str(alpha)})
    assert moved.status_code == 200

    assert _files_in(await client.get("/api/files/dir", params={"path": ""})) == [
        "only-in-alpha.txt"
    ]
    read = await client.get("/api/files/content", params={"path": "only-in-alpha.txt"})
    assert read.status_code == 200

    switched = await client.post("/api/workspace/switch", json={"path": str(beta)})
    assert switched.status_code == 200
    assert switched.json()["root"] == str(beta.resolve())

    assert _files_in(await client.get("/api/files/dir", params={"path": ""})) == [
        "only-in-beta.txt"
    ]
    # The old workspace's file is not merely absent from the listing — the file
    # is still on disk, and reaching it by its old *absolute* path is refused by
    # the jail, exactly as `../` always was.
    gone = await client.get("/api/files/content", params={"path": "only-in-alpha.txt"})
    assert gone.status_code == 404
    escaped = await client.get(
        "/api/files/content", params={"path": str(alpha / "only-in-alpha.txt")}
    )
    assert escaped.status_code == 400
    assert "escapes workspace" in escaped.json()["detail"]


def test_workspace_endpoint_reports_an_unchosen_root_as_unchosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First run: nobody set a root, so the app must be able to *say* that."""
    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(app_data_root=tmp_path / "appdata"))
    with TestClient(app) as client:
        state = client.get("/api/workspace").json()
    assert state["explicit"] is False
    assert state["root"] == str(tmp_path.resolve())
    assert state["name"] == tmp_path.name
    # The launch workspace is on the recent list from the start, so the first
    # switch has somewhere to come back to.
    assert [entry["path"] for entry in state["recents"]] == [str(tmp_path.resolve())]


async def test_switch_refusals_are_400_with_the_reason(client: AsyncClient, tmp_path: Path) -> None:
    missing = await client.post("/api/workspace/switch", json={"path": str(tmp_path / "nope")})
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["detail"]

    target = tmp_path / "a-file.txt"
    target.write_bytes(b"x")
    as_file = await client.post("/api/workspace/switch", json={"path": str(target)})
    assert as_file.status_code == 400
    assert "is a file, not a folder" in as_file.json()["detail"]


async def test_a_live_session_outside_the_new_root_is_relabelled_not_killed(
    settings: Settings, tmp_path: Path
) -> None:
    """A running agent survives the switch, and cannot be filed under a
    same-named folder of the project it has nothing to do with."""
    alpha, beta = _two_projects(tmp_path)
    (alpha / "src").mkdir()
    (beta / "src").mkdir()
    app = create_app(Settings(workspace_root=alpha, app_data_root=tmp_path / "appdata"))
    with TestClient(app) as client:
        created = client.post("/api/agents/sessions", json={"folder": "src"})
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert created.json()["folder"] == "src"

        assert client.post("/api/workspace/switch", json={"path": str(beta)}).status_code == 200

        manager = app.state.session_manager
        assert manager.get(session_id) is not None  # still running
        groups = client.get("/api/agents/sessions").json()
        rows = {
            session["session_id"]: group["folder"]
            for group in groups
            for session in group["sessions"]
        }
        assert rows[session_id] == (alpha / "src").as_posix()


def test_the_jail_still_refuses_a_traversal_after_a_switch(tmp_path: Path) -> None:
    """The rule itself did not move — only the root it is asked about."""
    alpha, beta = _two_projects(tmp_path)
    workspace = Workspace(alpha)
    workspace.set_root(beta)
    with pytest.raises(PathOutsideWorkspaceError):
        workspace.safe_path("../alpha/only-in-alpha.txt")
    assert workspace.safe_path("only-in-beta.txt") == (beta / "only-in-beta.txt").resolve()
