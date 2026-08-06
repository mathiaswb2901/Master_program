"""The fleet feed: what it keeps, what it drops, and what it refuses to say.

Three families of test, and the split is the design:

* **The window** — a rolling cap that is bounded *by construction*, evicts in a
  stated order, counts what it drops, and never resurrects an entry a settle
  arrives too late for. These run with no event loop at all, which is what makes
  the rules assertable one call at a time.
* **The jail** — a fleet-wide feed reaches every window in the workspace, which
  is wider than the per-session socket these frames came from. Paths are
  normalized workspace-relative, a path that escapes is redacted, and a tool
  *result* never appears at all.
* **The pipe** — the fake client puts real tool calls on a real session's
  stream, ``AgentSession`` hands them to the service at the same seam it hands
  provenance its claims, and the frame that comes out of ``/ws/events`` agrees
  with ``GET /api/activity``. Plus the budget the whole coalescer exists for: a
  forty-call burst is a handful of frames, not forty.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.activity import SessionActivityEvent
from workbench_server.services.activity import (
    MAX_ENTRIES_PER_SESSION,
    MAX_SESSIONS,
    OUTSIDE_WORKSPACE,
    SUMMARY_LIMIT,
    ActivityService,
    describe,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.fake_agent import STORM_TOOL_CALLS


class FakeClock:
    """Wall time under the test's control: ordering and ageing are real rules."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        self.now += 1.0  # every stamp is strictly later than the last
        return self.now


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[BaseModel] = []

    def publish(self, event: BaseModel) -> None:
        self.published.append(event)
        super().publish(event)

    def frames(self) -> list[SessionActivityEvent]:
        return [e for e in self.published if isinstance(e, SessionActivityEvent)]


def service(tmp_path: Path, **kwargs: Any) -> tuple[ActivityService, RecordingBus]:
    """A service with no event loop: every change publishes immediately, so a
    test can assert the window one call at a time. The coalescing tests below
    run a real loop precisely because that is the half a clock decides."""
    bus = RecordingBus()
    return ActivityService(tmp_path, bus, clock=FakeClock(), **kwargs), bus


# ---- the window --------------------------------------------------------------


def test_the_window_is_capped_and_says_how_much_it_dropped(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    for index in range(MAX_ENTRIES_PER_SESSION + 5):
        svc.note_tool_started(
            session_id="s1",
            session_title="a session",
            folder=tmp_path,
            folder_relative="",
            call_id=f"c{index}",
            tool="Grep",
            tool_input={"pattern": f"needle-{index}"},
        )
        svc.note_tool_settled(session_id="s1", call_id=f"c{index}", ok=True)
    row = svc.snapshot().sessions[0]
    assert len(row.entries) == MAX_ENTRIES_PER_SESSION
    # Nothing is lost silently: the five that fell out are on the record.
    assert row.dropped == 5
    # Newest first, and the newest is the last call that started.
    assert row.entries[0].entry_id == f"c{MAX_ENTRIES_PER_SESSION + 4}"


def test_the_running_call_is_the_last_thing_a_full_window_gives_up(tmp_path: Path) -> None:
    """A two-minute Bash must not be pushed out by the eight quick calls that
    followed it — the panel's headline is *what this agent is doing now*."""
    svc, _ = service(tmp_path)
    svc.note_tool_started(
        session_id="s1",
        session_title="a session",
        folder=tmp_path,
        folder_relative="",
        call_id="slow",
        tool="Bash",
        tool_input={"command": "uv run pytest"},
    )
    for index in range(MAX_ENTRIES_PER_SESSION + 3):
        svc.note_tool_started(
            session_id="s1",
            session_title="a session",
            folder=tmp_path,
            folder_relative="",
            call_id=f"q{index}",
            tool="Grep",
            tool_input={"pattern": "x"},
        )
        svc.note_tool_settled(session_id="s1", call_id=f"q{index}", ok=True)
    row = svc.snapshot().sessions[0]
    running = [entry for entry in row.entries if entry.settled_at is None]
    assert [entry.entry_id for entry in running] == ["slow"]


def test_a_settle_patches_its_entry_where_it_stands(tmp_path: Path) -> None:
    """Entries are ordered by when calls *started*. A settle that moved one
    would reshuffle rows under someone reading them."""
    svc, _ = service(tmp_path)
    for call_id in ("a", "b", "c"):
        svc.note_tool_started(
            session_id="s1",
            session_title="a session",
            folder=tmp_path,
            folder_relative="",
            call_id=call_id,
            tool="Read",
            tool_input={"file_path": f"{call_id}.py"},
        )
    svc.note_tool_settled(session_id="s1", call_id="a", ok=False)
    row = svc.snapshot().sessions[0]
    assert [entry.entry_id for entry in row.entries] == ["c", "b", "a"]
    settled = row.entries[2]
    assert settled.ok is False
    assert settled.settled_at is not None


def test_a_settle_for_an_evicted_entry_changes_nothing(tmp_path: Path) -> None:
    """The late half of a call whose start the window has already forgotten. It
    must not be resurrected — that would break both the cap and the ordering —
    and it must not cost a frame, because nothing a client renders changed."""
    svc, bus = service(tmp_path, max_entries=2)
    for call_id in ("old", "mid", "new"):
        svc.note_tool_started(
            session_id="s1",
            session_title="a session",
            folder=tmp_path,
            folder_relative="",
            call_id=call_id,
            tool="Grep",
            tool_input={"pattern": call_id},
        )
        svc.note_tool_settled(session_id="s1", call_id=call_id, ok=True)
    before = len(bus.frames())
    ids_before = [entry.entry_id for entry in svc.snapshot().sessions[0].entries]

    svc.note_tool_settled(session_id="s1", call_id="old", ok=False)

    assert [entry.entry_id for entry in svc.snapshot().sessions[0].entries] == ids_before
    assert len(bus.frames()) == before


def test_a_settle_for_a_session_that_is_gone_is_ignored(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    svc.note_tool_settled(session_id="never-existed", call_id="c1", ok=True)
    assert svc.snapshot().sessions == []


def test_the_fleet_is_capped_and_evictions_are_announced(tmp_path: Path) -> None:
    """Same discipline as the provenance map's LRU: a client holds its own copy,
    so a row we have forgotten can never be corrected later."""
    svc, bus = service(tmp_path, max_sessions=3)
    for index in range(5):
        svc.note_session(session_id=f"s{index}", title=f"session {index}", folder="")
    snapshot = svc.snapshot()
    assert [row.session_id for row in snapshot.sessions] == ["s4", "s3", "s2"]
    assert snapshot.dropped_sessions == 2
    removed = {name for frame in bus.frames() for name in frame.removed}
    assert removed == {"s0", "s1"}


def test_a_closed_session_leaves_the_fleet_and_the_frame_says_so(tmp_path: Path) -> None:
    svc, bus = service(tmp_path)
    svc.note_session(session_id="s1", title="a session", folder="")
    svc.note_session_gone(session_id="s1")
    assert svc.snapshot().sessions == []
    assert bus.frames()[-1].removed == ["s1"]
    # A second close is a no-op, not a second frame about nothing.
    before = len(bus.frames())
    svc.note_session_gone(session_id="s1")
    assert len(bus.frames()) == before


def test_a_session_that_has_run_nothing_is_still_a_row(tmp_path: Path) -> None:
    """An idle fleet is the common case: "three sessions open, none touching
    anything" is a reading, an empty panel is not."""
    svc, _ = service(tmp_path)
    svc.note_session(session_id="s1", title="new session", folder="src")
    row = svc.snapshot().sessions[0]
    assert row.entries == []
    assert row.title == "new session"
    assert row.folder == "src"


def test_naming_a_session_twice_with_the_same_title_costs_no_frame(tmp_path: Path) -> None:
    """``note_session`` fires on create *and* when the first message derives a
    title; the second call is only news when the title actually changed."""
    svc, bus = service(tmp_path)
    svc.note_session(session_id="s1", title="new session", folder="")
    before = len(bus.frames())
    svc.note_session(session_id="s1", title="new session", folder="")
    assert len(bus.frames()) == before
    svc.note_session(session_id="s1", title="fix the DST bug", folder="")
    assert len(bus.frames()) == before + 1
    assert svc.snapshot().sessions[0].title == "fix the DST bug"


def test_the_fleet_is_ordered_by_most_recently_active(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    for name in ("s1", "s2", "s3"):
        svc.note_session(session_id=name, title=name, folder="")
    svc.note_tool_started(
        session_id="s1",
        session_title="s1",
        folder=tmp_path,
        folder_relative="",
        call_id="c1",
        tool="Read",
        tool_input={"file_path": "a.py"},
    )
    assert [row.session_id for row in svc.snapshot().sessions] == ["s1", "s3", "s2"]


# ---- the jail ----------------------------------------------------------------


def test_a_workspace_path_becomes_a_target_the_window_can_open(tmp_path: Path) -> None:
    summary, target = describe(tmp_path, tmp_path / "src", "Edit", {"file_path": "model.py"})
    assert target == "src/model.py"
    assert summary == "Edit: src/model.py"


def test_a_path_outside_the_workspace_is_redacted_not_printed(tmp_path: Path) -> None:
    """The disclosure this jail exists for. On the per-session socket this row
    is only seen by a window that opened that conversation; here every window in
    the workspace receives it."""
    summary, target = describe(
        tmp_path, tmp_path, "Read", {"file_path": "C:/Users/someone/.ssh/config"}
    )
    assert target is None
    assert summary == f"Read: {OUTSIDE_WORKSPACE}"
    assert "someone" not in summary


def test_a_call_that_names_no_path_still_reads_as_something(tmp_path: Path) -> None:
    summary, target = describe(tmp_path, tmp_path, "Bash", {"command": "uv run pytest"})
    assert (summary, target) == ("Bash: uv run pytest", None)
    assert describe(tmp_path, tmp_path, "TodoWrite", {}) == ("TodoWrite", None)


def test_a_summary_is_capped(tmp_path: Path) -> None:
    summary, _ = describe(tmp_path, tmp_path, "Bash", {"command": "x" * 500})
    assert len(summary) == SUMMARY_LIMIT


def test_a_tool_result_never_reaches_the_fleet_feed(tmp_path: Path) -> None:
    """``ToolSettled`` carries an excerpt to the conversation that produced it.
    Only ``ok`` crosses to the shared bus — asserted on the serialized frame,
    because "the model has no field for it" is a claim a later edit can undo."""
    svc, bus = service(tmp_path)
    svc.note_tool_started(
        session_id="s1",
        session_title="a session",
        folder=tmp_path,
        folder_relative="",
        call_id="c1",
        tool="Read",
        tool_input={"file_path": "a.py"},
    )
    svc.note_tool_settled(session_id="s1", call_id="c1", ok=True)
    wire = "".join(frame.model_dump_json() for frame in bus.frames())
    assert "excerpt" not in wire
    assert svc.snapshot().sessions[0].entries[0].ok is True


# ---- the pipe: SDK seam -> bus -> /ws/events -> GET /api/activity -------------


def _app(tmp_path: Path) -> Any:
    return create_app(
        Settings(
            workspace_root=tmp_path,
            claude_projects_dir=tmp_path / "projects",
            fake_agent=True,
        )
    )


def _drain(
    events: Any,
    until: Callable[[list[dict[str, Any]]], bool],
    limit: int = 400,
) -> list[dict[str, Any]]:
    """Read `/ws/events` frames until `until` says we have what we came for.

    A bounded read rather than a sleep: the fan-out carries watcher frames too,
    and the number of them depends on what the filesystem did.
    """
    seen: list[dict[str, Any]] = []
    for _ in range(limit):
        frame = json.loads(events.receive_text())
        if frame["type"] == "session_activity":
            seen.append(frame)
            if until(seen):
                return seen
    raise AssertionError(f"never saw enough session_activity frames (got {len(seen)})")


def test_a_tool_call_flows_from_the_sdk_seam_to_both_readers(tmp_path: Path) -> None:
    """The whole pipe, through the production wiring — and the point of the
    feature: the window listening here never opened this session's socket."""
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/api/activity").json()["sessions"] == []

        with client.websocket_connect("/ws/events") as events:
            local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
            # Creating a session is already fleet news: the panel lists an idle
            # session rather than showing nothing until it runs a tool.
            frames = _drain(events, lambda seen: any(f["sessions"] for f in seen))
            assert frames[-1]["sessions"][0]["session_id"] == local_id

            with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
                agent.send_text(json.dumps({"type": "user_message", "text": "use tool"}))
                while json.loads(agent.receive_text())["type"] != "turn_done":
                    pass

            def settled(seen: list[dict[str, Any]]) -> bool:
                return any(
                    entry["settled_at"] is not None
                    for frame in seen
                    for row in frame["sessions"]
                    for entry in row["entries"]
                )

            _drain(events, settled)

        # The load path a window that connected afterwards takes.
        snapshot = client.get("/api/activity").json()
        assert snapshot["max_entries_per_session"] == MAX_ENTRIES_PER_SESSION
        assert snapshot["max_sessions"] == MAX_SESSIONS
        row = snapshot["sessions"][0]
        assert row["session_id"] == local_id
        entry = row["entries"][0]
        assert entry["tool"] == "Read"
        # The fake reads the alphabetically first file in the session folder,
        # which is the one seeded above — and it is a *target*, so the panel can
        # open it.
        assert entry["target"] == "notes.md"
        assert entry["summary"] == "Read: notes.md"
        assert entry["ok"] is True
        assert entry["settled_at"] is not None


def test_a_burst_is_coalesced_into_a_handful_of_frames(tmp_path: Path) -> None:
    """The budget the coalescer exists for, and the ROADMAP's exit criterion:
    a Grep-heavy turn must cost the shared socket almost nothing.

    Work-shaped, so it cannot flake: ``tool storm`` fires exactly
    ``STORM_TOOL_CALLS`` calls, which is 2 x that many changes (an announce and
    a settle each). The ceiling is generous — what it excludes is the
    uncoalesced design, which would produce 80.
    """
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    with TestClient(_app(tmp_path)) as client:
        with client.websocket_connect("/ws/events") as events:
            local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
            with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
                agent.send_text(json.dumps({"type": "user_message", "text": "tool storm"}))
                while json.loads(agent.receive_text())["type"] != "turn_done":
                    pass
            frames = _drain(
                events,
                lambda seen: any(
                    entry["summary"].endswith(f"needle-{STORM_TOOL_CALLS - 1}")
                    and entry["settled_at"] is not None
                    for frame in seen
                    for row in frame["sessions"]
                    for entry in row["entries"]
                ),
            )

        changes = 2 * STORM_TOOL_CALLS
        uncoalesced = f"{len(frames)} frames for {changes} changes — no coalescing"
        assert len(frames) < changes // 4, uncoalesced
        # And each frame stays small: one row, holding a window that is capped.
        widest = max(len(frame_json) for frame_json in (json.dumps(f) for f in frames))
        assert widest < 4000, f"widest frame {widest} bytes"

        row = client.get("/api/activity").json()["sessions"][0]
        assert len(row["entries"]) == MAX_ENTRIES_PER_SESSION
        assert row["dropped"] == STORM_TOOL_CALLS - MAX_ENTRIES_PER_SESSION


async def test_the_first_change_after_a_quiet_fleet_goes_out_immediately() -> None:
    """The interactive half of the same policy (``terminal_stream.py`` proves
    it for the PTY): one tool call on an idle workbench must not wait on a
    timer. Asserted with a real loop, because the immediacy *is* the clock."""
    bus = RecordingBus()
    svc = ActivityService(Path.cwd(), bus)
    svc.start()
    try:
        svc.note_session(session_id="s1", title="a session", folder="")
        assert len(bus.frames()) == 1, "an idle fleet's first change waited on a timer"
        # The next one inside the window rides a single scheduled frame instead.
        svc.note_session(session_id="s2", title="another", folder="")
        svc.note_session(session_id="s3", title="a third", folder="")
        assert len(bus.frames()) == 1
    finally:
        await svc.stop()


@pytest.mark.parametrize("path_key", ["file_path", "notebook_path", "path"])
def test_every_path_key_provenance_knows_is_jailed_here_too(tmp_path: Path, path_key: str) -> None:
    """The two views must not disagree about which file an agent is on."""
    _, target = describe(tmp_path, tmp_path, "Edit", {path_key: "src/model.py"})
    assert target == "src/model.py"
