"""The correlator, up close.

Every test here is about one thing: an attribution is a claim about the user's
own files, so being *wrong* is worse than being silent. The near-miss, the
expired window and the external-write cases are the ones that matter — they are
the cases where a lazier heuristic would name a session it has no evidence for.
"""

import json
import os
from pathlib import Path
from typing import Any, Literal

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.files import FileChangedEvent
from workbench_server.models.provenance import FileProvenanceEvent
from workbench_server.services.event_bus import EventBus
from workbench_server.services.provenance import (
    ATTRIBUTION_WINDOW_S,
    MAX_TRACKED_PATHS,
    ProvenanceService,
    is_write_tool,
    workspace_relative,
)

ROOT = Path("C:/ws") if os.name == "nt" else Path("/ws")

OFFICE_SECRET = "test-secret"
DOCX = b"PK\x03\x04 fake docx bytes"
EDITED_DOCX = b"PK\x03\x04 the user's own keystrokes"


class FakeClock:
    """Monotonic time under the test's control — the window is a real rule."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[BaseModel] = []

    def publish(self, event: BaseModel) -> None:
        self.published.append(event)
        super().publish(event)

    def provenance_events(self) -> list[FileProvenanceEvent]:
        return [e for e in self.published if isinstance(e, FileProvenanceEvent)]


def service(clock: FakeClock, root: Path = ROOT) -> tuple[ProvenanceService, RecordingBus]:
    bus = RecordingBus()
    return ProvenanceService(root, bus, clock=clock), bus


def changed(
    path: str, change: Literal["added", "modified", "deleted"] = "modified"
) -> FileChangedEvent:
    return FileChangedEvent(path=path, change=change, hash="abc")


def write_note(
    svc: ProvenanceService,
    path: str,
    *,
    session_id: str = "s1",
    title: str = "Fix the bidder",
    tool: str = "Write",
    folder: Path | None = None,
    call_id: str | None = None,
) -> None:
    svc.note_tool_use(
        session_id=session_id,
        session_title=title,
        folder=folder if folder is not None else ROOT,
        tool=tool,
        tool_input={"file_path": path},
        call_id=call_id,
    )


# ---- attribution -------------------------------------------------------------


def test_exact_path_inside_the_window_is_attributed() -> None:
    clock = FakeClock()
    svc, bus = service(clock)
    write_note(svc, "src/model.py")
    clock.advance(0.4)  # the watcher's debounce
    svc.note_file_change(changed("src/model.py"))

    entry = svc.snapshot().entries[0]
    assert entry.path == "src/model.py"
    assert entry.agent is not None
    assert entry.agent.session_id == "s1"
    assert entry.agent.session_title == "Fix the bidder"
    assert entry.agent.tool == "Write"
    assert entry.acknowledged is False
    assert bus.provenance_events()[0].entry == entry


def test_a_near_miss_path_is_not_attributed() -> None:
    """Same directory, one character apart. A suffix or prefix match here would
    hand the user a confident, wrong answer."""
    clock = FakeClock()
    svc, bus = service(clock)
    write_note(svc, "src/model.py")
    svc.note_file_change(changed("src/models.py"))
    svc.note_file_change(changed("other/src/model.py"))

    assert svc.snapshot().entries == []
    assert bus.provenance_events() == []


def test_a_change_after_the_window_is_not_attributed() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "src/model.py")
    clock.advance(ATTRIBUTION_WINDOW_S + 0.1)
    svc.note_file_change(changed("src/model.py"))

    assert svc.snapshot().entries == []


def test_an_external_change_is_reported_as_unattributed_not_guessed() -> None:
    """A git checkout, another editor, a build. There *is* a recent session with
    a recent tool call — and it still must not be named."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "src/model.py")
    svc.note_file_change(changed("docs/README.md"))

    assert [e.path for e in svc.snapshot().entries] == []


def test_the_most_recent_exact_match_wins_when_two_sessions_claim_a_path() -> None:
    """Two agents writing the same file inside the window is a genuine
    ambiguity. The documented rule is "most recent exact match"; nothing
    cleverer is available and a coin flip would be dishonest."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "src/model.py", session_id="first", title="First")
    clock.advance(0.5)
    write_note(svc, "src/model.py", session_id="second", title="Second")
    clock.advance(0.2)
    svc.note_file_change(changed("src/model.py"))

    agent = svc.snapshot().entries[0].agent
    assert agent is not None
    assert agent.session_id == "second"


def test_one_claim_survives_the_burst_a_single_write_produces() -> None:
    """A Windows write often surfaces as several watcher events. Consuming the
    claim on the first would report the rest as the user's."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "notes.md")
    svc.note_file_change(changed("notes.md", "added"))
    clock.advance(0.3)
    svc.note_file_change(changed("notes.md", "modified"))

    entry = svc.snapshot().entries[0]
    assert entry.agent is not None and entry.agent.session_id == "s1"


def test_deletions_are_ignored() -> None:
    """Order inside a watchfiles batch is not guaranteed, so a delete must not
    be allowed to erase the attribution for the write that produced it."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "notes.md")
    svc.note_file_change(changed("notes.md", "added"))
    svc.note_file_change(changed("notes.md", "deleted"))

    assert svc.snapshot().entries[0].agent is not None


def test_only_file_writing_tools_claim_anything() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    svc.note_tool_use(
        session_id="s1",
        session_title="Reading around",
        folder=ROOT,
        tool="Read",
        tool_input={"file_path": "src/model.py"},
    )
    svc.note_file_change(changed("src/model.py"))
    assert svc.snapshot().entries == []


def test_write_tool_names_are_matched_case_and_namespace_insensitively() -> None:
    assert is_write_tool("Write") and is_write_tool("edit")
    assert is_write_tool("MultiEdit") and is_write_tool("NotebookEdit")
    assert is_write_tool("mcp__filesystem__write")
    assert not is_write_tool("Read")
    assert not is_write_tool("Bash")


# ---- claims that turned out not to have written anything ----------------------


def test_a_failed_tool_withdraws_its_claim() -> None:
    """An ``Edit`` that came back "String to replace not found" wrote nothing.
    The user then makes the fix by hand, inside the window — and it is *theirs*.
    Without the withdrawal, the tool the user watched fail gets the credit."""
    clock = FakeClock()
    svc, bus = service(clock)
    write_note(svc, "src/bidder.py", tool="Edit", call_id="call-1")
    svc.note_tool_result(call_id="call-1", ok=False)
    clock.advance(2.0)
    svc.note_file_change(changed("src/bidder.py"))

    assert svc.snapshot().entries == []
    assert bus.provenance_events() == []


def test_a_successful_tool_keeps_its_claim_after_the_result() -> None:
    """The result routinely lands before the last watcher event of the write it
    describes — settling a *successful* call must not drop its claim."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "notes.md", call_id="call-1")
    svc.note_tool_result(call_id="call-1", ok=True)
    clock.advance(0.3)
    svc.note_file_change(changed("notes.md"))

    entry = svc.snapshot().entries[0]
    assert entry.agent is not None and entry.agent.session_id == "s1"


def test_a_failed_tool_withdraws_only_its_own_claim() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "kept.md", call_id="call-1")
    write_note(svc, "failed.md", call_id="call-2")
    svc.note_tool_result(call_id="call-2", ok=False)
    svc.note_file_change(changed("kept.md"))
    svc.note_file_change(changed("failed.md"))

    assert [e.path for e in svc.snapshot().entries] == ["kept.md"]


def test_a_denied_permission_withdraws_the_claim() -> None:
    """The user clicked Deny on the card, then made the change themselves —
    reverting the file with `git checkout`, or editing it in another window."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "src/bidder.py", call_id="call-1")
    svc.note_tool_denied(
        session_id="s1", folder=ROOT, tool="Write", tool_input={"file_path": "src/bidder.py"}
    )
    clock.advance(1.0)
    svc.note_file_change(changed("src/bidder.py"))

    assert svc.snapshot().entries == []


def test_a_denial_withdraws_one_claim_not_the_history_of_that_path() -> None:
    """A session that wrote the file earlier in the turn, and had a *second*
    write refused, is still the author of the first one."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "notes.md", call_id="call-1")
    clock.advance(0.5)
    write_note(svc, "notes.md", call_id="call-2")
    svc.note_tool_denied(
        session_id="s1", folder=ROOT, tool="Write", tool_input={"file_path": "notes.md"}
    )
    svc.note_file_change(changed("notes.md"))

    entry = svc.snapshot().entries[0]
    assert entry.agent is not None and entry.agent.session_id == "s1"


def test_a_denial_never_withdraws_another_sessions_claim() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "notes.md", session_id="other", title="Other", call_id="call-1")
    svc.note_tool_denied(
        session_id="s1", folder=ROOT, tool="Write", tool_input={"file_path": "notes.md"}
    )
    svc.note_file_change(changed("notes.md"))

    entry = svc.snapshot().entries[0]
    assert entry.agent is not None and entry.agent.session_id == "other"


# ---- clearing and acknowledgment ---------------------------------------------


def test_a_later_unattributed_change_clears_the_agent_claim() -> None:
    clock = FakeClock()
    svc, bus = service(clock)
    write_note(svc, "src/model.py")
    svc.note_file_change(changed("src/model.py"))
    clock.advance(ATTRIBUTION_WINDOW_S + 1)
    svc.note_file_change(changed("src/model.py"))

    assert svc.snapshot().entries == []
    cleared = bus.provenance_events()[-1].entry
    assert cleared.path == "src/model.py"
    assert cleared.agent is None  # the wire's "drop this path"


def test_the_users_own_save_beats_a_still_open_agent_claim() -> None:
    """The editor's PUT says so explicitly, so a save seconds after an agent
    wrote the same file is the user's change — not the agent's."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "src/model.py")
    clock.advance(1.0)
    svc.note_user_write("src/model.py")
    svc.note_file_change(changed("src/model.py"))

    assert svc.snapshot().entries == []


def test_acknowledge_keeps_the_attribution_and_clears_the_marker() -> None:
    clock = FakeClock()
    svc, bus = service(clock)
    write_note(svc, "notes.md")
    svc.note_file_change(changed("notes.md"))

    acknowledged = svc.acknowledge("notes.md")
    assert acknowledged is not None
    assert acknowledged.acknowledged is True
    assert acknowledged.agent is not None  # who changed it is still true
    assert bus.provenance_events()[-1].entry.acknowledged is True
    # Idempotent, and an unknown path is a no-op rather than an error.
    assert svc.acknowledge("notes.md") == acknowledged
    assert svc.acknowledge("never/seen.md") is None
    assert len(bus.provenance_events()) == 2


def test_a_new_agent_change_reopens_an_acknowledged_path() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "notes.md")
    svc.note_file_change(changed("notes.md"))
    svc.acknowledge("notes.md")

    clock.advance(60)
    write_note(svc, "notes.md", session_id="s2", title="Second pass")
    svc.note_file_change(changed("notes.md"))

    entry = svc.snapshot().entries[0]
    assert entry.acknowledged is False
    assert entry.agent is not None and entry.agent.session_id == "s2"


# ---- bounds ------------------------------------------------------------------


def test_the_map_is_bounded_and_evicts_the_least_recently_changed() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    for i in range(MAX_TRACKED_PATHS + 10):
        write_note(svc, f"f{i}.txt")
        svc.note_file_change(changed(f"f{i}.txt"))

    paths = [e.path for e in svc.snapshot().entries]
    assert len(paths) == MAX_TRACKED_PATHS
    assert "f0.txt" not in paths  # evicted
    assert f"f{MAX_TRACKED_PATHS + 9}.txt" in paths


def test_an_eviction_is_announced_as_a_clear() -> None:
    """Clients keep their own map, and an entry the server has forgotten can
    never be corrected later — the clear branch only fires for paths it still
    holds. A silent eviction would strand the attribution on screen."""
    clock = FakeClock()
    svc, bus = service(clock)
    for i in range(MAX_TRACKED_PATHS + 1):
        write_note(svc, f"f{i}.txt")
        svc.note_file_change(changed(f"f{i}.txt"))

    clears = [e.entry for e in bus.provenance_events() if e.entry.agent is None]
    assert [c.path for c in clears] == ["f0.txt"]


def test_touching_a_tracked_path_again_keeps_it_from_being_evicted() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "keep.txt")
    svc.note_file_change(changed("keep.txt"))
    for i in range(MAX_TRACKED_PATHS - 1):
        write_note(svc, f"f{i}.txt")
        svc.note_file_change(changed(f"f{i}.txt"))
    write_note(svc, "keep.txt")
    svc.note_file_change(changed("keep.txt"))  # refreshes its LRU position
    for i in range(10):
        write_note(svc, f"later{i}.txt")
        svc.note_file_change(changed(f"later{i}.txt"))

    assert "keep.txt" in [e.path for e in svc.snapshot().entries]


# ---- path normalization ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "folder", "expected"),
    [
        ("src/model.py", ROOT, "src/model.py"),
        ("src\\model.py", ROOT, "src/model.py"),  # Windows separators
        ("model.py", ROOT / "src", "src/model.py"),  # relative to the session folder
        (".\\model.py", ROOT / "src", "src/model.py"),
        ("../notes.md", ROOT / "src", "notes.md"),
        (str(ROOT / "src" / "model.py"), ROOT, "src/model.py"),  # absolute
        ('"src/model.py"', ROOT, "src/model.py"),  # quoted
        ("  src/model.py  ", ROOT, "src/model.py"),
        ("", ROOT, None),
        ("   ", ROOT, None),
        ("../../outside.py", ROOT, None),  # escapes the workspace
    ],
)
def test_path_normalization(raw: str, folder: Path, expected: str | None) -> None:
    assert workspace_relative(ROOT, folder, raw) == expected


def test_a_relative_path_resolves_against_the_session_folder_not_the_root() -> None:
    """An agent's cwd is its session folder, so a bare filename means a file
    *there* — attributing it to the root would name the wrong file."""
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "model.py", folder=ROOT / "src")
    svc.note_file_change(changed("model.py"))
    assert svc.snapshot().entries == []

    svc.note_file_change(changed("src/model.py"))
    assert [e.path for e in svc.snapshot().entries] == ["src/model.py"]


def test_a_write_outside_the_workspace_never_becomes_a_claim() -> None:
    clock = FakeClock()
    svc, _ = service(clock)
    write_note(svc, "../elsewhere/model.py")
    svc.note_file_change(changed("elsewhere/model.py"))
    assert svc.snapshot().entries == []


# ---- through the whole app ---------------------------------------------------


@pytest.mark.timeout(90)
def test_a_fake_session_write_reaches_ws_events(tmp_path: Path) -> None:
    """The integration the feature exists for: a session writes a file with its
    own tool, the real watcher notices, and the UI learns whose change it was —
    over the real bus, from the real endpoint."""
    settings = Settings(
        workspace_root=tmp_path,
        claude_projects_dir=tmp_path / "projects",
        fake_agent=True,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        created = client.post("/api/agents/sessions", json={"folder": ""}).json()
        local_id = created["session_id"]
        with client.websocket_connect("/ws/events") as events:
            with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
                agent.send_text(json.dumps({"type": "user_message", "text": "write file please"}))
                frame = json.loads(agent.receive_text())
                while frame["type"] != "turn_done":
                    frame = json.loads(agent.receive_text())

            provenance = None
            while provenance is None:
                event = json.loads(events.receive_text())
                if event["type"] == "file_provenance":
                    provenance = event

    entry = provenance["entry"]
    assert entry["path"] == "written-by-agent.md"
    assert entry["agent"]["session_id"] == local_id
    assert entry["agent"]["tool"] == "Write"
    assert entry["agent"]["session_title"] == "write file please"
    assert entry["acknowledged"] is False


@pytest.mark.timeout(90)
def test_rest_map_and_acknowledge_agree_with_the_socket(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        claude_projects_dir=tmp_path / "projects",
        fake_agent=True,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect("/ws/events") as events:
            with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
                agent.send_text(json.dumps({"type": "user_message", "text": "write file"}))
                frame = json.loads(agent.receive_text())
                while frame["type"] != "turn_done":
                    frame = json.loads(agent.receive_text())
            while True:
                if json.loads(events.receive_text())["type"] == "file_provenance":
                    break

        listed = client.get("/api/provenance").json()["entries"]
        assert [e["path"] for e in listed] == ["written-by-agent.md"]
        assert listed[0]["agent"]["session_id"] == local_id
        assert listed[0]["acknowledged"] is False

        acked = client.post("/api/provenance/acknowledge", json={"path": "written-by-agent.md"})
        assert acked.status_code == 200
        after = client.get("/api/provenance").json()["entries"]
        assert after[0]["acknowledged"] is True
        # An unknown path is a no-op, not an error: the UI acknowledges on open.
        assert (
            client.post("/api/provenance/acknowledge", json={"path": "nope.md"}).status_code == 200
        )


@pytest.mark.timeout(90)
def test_a_refused_write_leaves_the_next_change_to_that_path_unattributed(tmp_path: Path) -> None:
    """The whole failure mode, end to end: the agent announces a ``Write``, the
    call comes back an error (a declined permission card, an ``Edit`` whose old
    string was not found), and the user then makes that change themselves from
    outside the app. The claim was recorded when the call was *announced*, so
    without withdrawing it the user's own edit wears the agent's name."""
    settings = Settings(
        workspace_root=tmp_path,
        claude_projects_dir=tmp_path / "projects",
        fake_agent=True,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect("/ws/events") as events:
            with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
                agent.send_text(json.dumps({"type": "user_message", "text": "refuse write please"}))
                frame = json.loads(agent.receive_text())
                while frame["type"] != "turn_done":
                    frame = json.loads(agent.receive_text())
            assert not (tmp_path / "refused-by-agent.md").exists()

            # The user makes the change by hand, well inside the window.
            (tmp_path / "refused-by-agent.md").write_text("mine, not yours\n", encoding="utf-8")
            _await_file_changed(events, "refused-by-agent.md")

            # Barrier: a claim that *is* good, on a path written from outside the
            # app. Its frame cannot be published before the earlier change has
            # been processed, so "no frame for refused-by-agent.md" is a fact
            # rather than a race.
            _claim(app, session_id=local_id, path="barrier.md")
            (tmp_path / "barrier.md").write_text("agent wrote this\n", encoding="utf-8")
            attributed = _await_file_provenance(events)

        assert attributed["entry"]["path"] == "barrier.md"
        listed = client.get("/api/provenance").json()["entries"]
        assert [e["path"] for e in listed] == ["barrier.md"]


@pytest.mark.timeout(90)
def test_an_office_save_inside_an_agent_claim_stays_the_users(tmp_path: Path) -> None:
    """A `.docx` is the one file the user cannot diff for themselves, so an
    OnlyOffice save landing on an open agent claim is the worst place to get
    this wrong. The Document Server's callback is the user's own keystrokes."""
    (tmp_path / "report.docx").write_bytes(DOCX)
    settings = Settings(
        workspace_root=tmp_path,
        claude_projects_dir=tmp_path / "projects",
        onlyoffice_url="http://localhost:8880",
        onlyoffice_jwt_secret=OFFICE_SECRET,
        public_base_url="http://127.0.0.1:8787",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=EDITED_DOCX))
        )
        cfg = client.get("/api/office/config", params={"path": "report.docx"}).json()
        callback_token = cfg["editorConfig"]["callbackUrl"].rsplit("/", 1)[-1]

        with client.websocket_connect("/ws/events") as events:
            _claim(app, session_id="s1", path="report.docx")
            body: dict[str, Any] = {
                "status": 2,
                "url": "http://stub-ds/download/x",
                "key": cfg["document"]["key"],
            }
            body["token"] = pyjwt.encode(body, OFFICE_SECRET, algorithm="HS256")
            assert client.post(f"/api/office/callback/{callback_token}", json=body).json() == {
                "error": 0
            }
            assert (tmp_path / "report.docx").read_bytes() == EDITED_DOCX
            _await_file_changed(events, "report.docx")

            _claim(app, session_id="s1", path="barrier.md")
            (tmp_path / "barrier.md").write_text("agent wrote this\n", encoding="utf-8")
            attributed = _await_file_provenance(events)

        assert attributed["entry"]["path"] == "barrier.md"
        listed = client.get("/api/provenance").json()["entries"]
        assert [e["path"] for e in listed] == ["barrier.md"]


def test_create_and_rename_announce_the_users_write(tmp_path: Path) -> None:
    """`PUT /api/files/content` is not the only way Workbench writes for the
    user. Create and rename produce watcher events of exactly the same shape, so
    they have to say so too or an open agent claim collects the credit."""
    app = create_app(Settings(workspace_root=tmp_path, claude_projects_dir=tmp_path / "projects"))
    with TestClient(app) as client:
        recorder = _RecordingWrites()
        app.state.provenance = recorder  # the routers resolve it per request
        client.post("/api/files/create", json={"path": "src", "kind": "dir"})
        client.post("/api/files/create", json={"path": "src/new.py", "kind": "file"})
        client.post("/api/files/rename", json={"path": "src/new.py", "new_path": "src/keep.py"})
        client.put("/api/files/content", json={"path": "src/keep.py", "content": "x = 1\n"})

    assert recorder.paths == ["src", "src/new.py", "src/keep.py", "src/keep.py"]


class _RecordingWrites:
    """Stands in for the service on `app.state`: only the one call is of interest."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def note_user_write(self, relative_path: str) -> None:
        self.paths.append(relative_path)


def _claim(app: Any, *, session_id: str, path: str) -> None:
    """The claim the session would have recorded from an SDK ``ToolUseBlock``.

    Made directly because the fake agent writes one fixed path, and these
    journeys need a claim on a document (and on a file the test writes itself).
    """
    app.state.provenance.note_tool_use(
        session_id=session_id,
        session_title="Draft the report",
        folder=app.state.workspace.root,
        tool="Write",
        tool_input={"file_path": path},
        call_id=f"call-{path}",
    )


def _await_file_changed(events: Any, path: str) -> None:
    """Block until the watcher reports ``path``. Used to keep the next change in
    a *later* watcher batch: a batch is a set, so order within one is not
    guaranteed and a barrier inside it would not be a barrier."""
    while True:
        event = json.loads(events.receive_text())
        if event["type"] == "file_changed" and event["path"] == path:
            return


def _await_file_provenance(events: Any) -> dict[str, Any]:
    while True:
        event: dict[str, Any] = json.loads(events.receive_text())
        if event["type"] == "file_provenance":
            return event
