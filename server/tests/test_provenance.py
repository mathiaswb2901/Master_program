"""The correlator, up close.

Every test here is about one thing: an attribution is a claim about the user's
own files, so being *wrong* is worse than being silent. The near-miss, the
expired window and the external-write cases are the ones that matter — they are
the cases where a lazier heuristic would name a session it has no evidence for.
"""

import json
import os
from pathlib import Path
from typing import Literal

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
) -> None:
    svc.note_tool_use(
        session_id=session_id,
        session_title=title,
        folder=folder if folder is not None else ROOT,
        tool=tool,
        tool_input={"file_path": path},
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
