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

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.activity import SessionActivityEvent
from workbench_server.services import worktrees as worktrees_service
from workbench_server.services.activity import (
    ACTIVITY_WINDOW_S,
    MAX_ENTRIES_PER_SESSION,
    MAX_SESSIONS,
    OUTSIDE_WORKSPACE,
    SUMMARY_LIMIT,
    ActivityService,
    describe,
)
from workbench_server.services.agent_sessions import SessionManager
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
    # A plain chat by default — the kind is only news for an orchestrator.
    assert row.kind == "chat"


def test_a_sessions_kind_rides_the_row_from_creation(tmp_path: Path) -> None:
    """Mission Control tells an orchestrator from a chat off the activity feed,
    before either has run a tool or raised a prompt — so the kind has to be on
    the row the moment the session is created, not derived from later activity.
    """
    svc, _ = service(tmp_path)
    svc.note_session(session_id="boss", title="run the crew", folder="", kind="orchestrator")
    assert svc.snapshot().sessions[0].kind == "orchestrator"


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
    the workspace receives it.

    The path must be absolute on the platform running the test — "C:/Users/…"
    is an ordinary *relative* path on linux and macOS, which would land inside
    the workspace and prove nothing about the jail."""
    secret = (
        "C:/Users/someone/.ssh/config" if sys.platform == "win32" else "/home/someone/.ssh/config"
    )
    summary, target = describe(tmp_path, tmp_path, "Read", {"file_path": secret})
    assert target is None
    assert summary == f"Read: {OUTSIDE_WORKSPACE}"
    assert "someone" not in summary


def test_a_named_root_outside_the_workspace_is_named_but_never_clickable(tmp_path: Path) -> None:
    """A Mission Control worker's cwd is a pooled slot, outside the workspace by
    design, so without ``extra_roots`` every row of a working worker would read
    ``(outside the workspace)``. Naming and opening are separate answers: the
    slot is spelled out, and ``target`` — what the panel makes clickable — stays
    ``None`` because the jail is not widened by it.
    """
    workspace = tmp_path / "ws"
    pool = tmp_path / "pool"
    summary, target = describe(
        workspace,
        workspace,
        "Read",
        {"file_path": str(pool / "slot-01" / "server" / "main.py")},
        [("", pool)],
    )
    assert summary == "Read: slot-01/server/main.py"
    assert target is None
    # A path in neither the workspace nor a named root is still redacted.
    outside, _ = describe(
        workspace,
        workspace,
        "Read",
        {"file_path": str(tmp_path / "elsewhere" / "x.py")},
        [("", pool)],
    )
    assert outside == f"Read: {OUTSIDE_WORKSPACE}"


def test_a_named_root_can_carry_a_label(tmp_path: Path) -> None:
    """The label prefixes what the row shows, so two named roots stay tellable
    apart. Production passes ``""`` — the pool root's own slot names already
    read as slots."""
    workspace = tmp_path / "ws"
    pool = tmp_path / "pool"
    summary, _ = describe(
        workspace,
        workspace,
        "Read",
        {"file_path": str(pool / "slot-01" / "a.py")},
        [("pool", pool)],
    )
    assert summary == "Read: pool/slot-01/a.py"


def test_the_feed_asks_for_its_named_roots_rather_than_remembering_them(tmp_path: Path) -> None:
    """The pool root is itself workspace-derived and moves on every switch, so a
    copy taken at construction is the old one for the rest of the process — the
    same staleness as the jail, one indirection out. Asked for per call, it
    cannot go stale and no re-rooting order has to be got right.
    """
    # Outside the workspace, like the real pool root: a slot *inside* it would
    # be named by the jail and prove nothing about the named roots.
    pool = tmp_path / "pool-alpha"
    named = [("", pool)]
    svc, _ = service(tmp_path / "ws", extra_roots=lambda: named)

    def summary_for(call_id: str, path: Path) -> str:
        svc.note_tool_started(
            session_id="w1",
            session_title="a worker",
            folder=path.parent,
            folder_relative=path.parent.as_posix(),
            call_id=call_id,
            tool="Read",
            tool_input={"file_path": str(path)},
        )
        return svc.snapshot().sessions[0].entries[0].summary

    assert summary_for("c1", pool / "slot-01" / "a.py") == "Read: slot-01/a.py"
    moved = tmp_path / "pool-beta"
    named[:] = [("", moved)]
    assert summary_for("c2", moved / "slot-01" / "a.py") == "Read: slot-01/a.py"
    # And the root it left is no more nameable than any other stranger's.
    assert summary_for("c3", pool / "slot-01" / "a.py") == f"Read: {OUTSIDE_WORKSPACE}"


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


def test_a_broken_activity_observer_cannot_wedge_the_shutdown(tmp_path: Path) -> None:
    """Shutting the server down with a fleet running, while this feed is broken.

    The reproduction, at the level a user meets it: a real app, two real
    sessions that have each run a turn, and the observer raising when the
    lifespan tears them down. ``close`` telling the fleet view a session is gone
    is a *notification*; everything after it in that method **releases a
    resource**, and ``close_all`` is awaited by the lifespan before the watcher,
    the PTYs and the Office host are stopped.

    Before the guard, the exception escaped ``close`` -> ``close_all`` -> the
    lifespan: the first session's turn task and SDK client were left running,
    the second session was never closed at all, and the whole rest of shutdown
    was skipped. Here that failure surfaces as ``TestClient.__exit__`` raising.
    """
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    app = _app(tmp_path)
    with TestClient(app) as client:
        manager: SessionManager = app.state.session_manager
        local_ids = [
            client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
            for _ in range(2)
        ]
        # A turn each, so both sessions actually hold a connected SDK client —
        # a session that never ran has nothing for a broken close to strand.
        for local_id in local_ids:
            with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
                agent.send_text(json.dumps({"type": "user_message", "text": "use tool"}))
                while json.loads(agent.receive_text())["type"] != "turn_done":
                    pass
        sessions = [manager.get(local_id) for local_id in local_ids]
        assert all(session is not None and session._client is not None for session in sessions)

        def boom(**_: Any) -> None:
            raise RuntimeError("the activity service is broken")

        app.state.activity.note_session_gone = boom
    # Leaving the block ran the lifespan's shutdown. Reaching this line at all is
    # the regression assertion: it did not raise.
    for session in sessions:
        assert session is not None
        assert session._client is None, "an SDK client outlived the server"
    assert manager.sessions == {}, "close_all gave up partway through the fleet"


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


# ---- the switch --------------------------------------------------------------
#
# The workspace is not fixed at launch (M5 item 5): the status-bar chip and the
# QuickBar's *Switch workspace…* re-root the running server. This service copies
# `workspace.root` into a field of its own, so it owes a `set_workspace_root` and
# a line in `create_app`'s rootables — and until it had them, a switch left the
# feed jailed against the folder the user walked away from.


def _two_projects(tmp_path: Path) -> tuple[Path, Path]:
    """Two workspaces whose files cannot be confused for each other, and which
    are not inside one another — so a path from one is genuinely outside the
    other's jail rather than merely relative to a different prefix."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha / "only-in-alpha.md").write_text("a\n", encoding="utf-8")
    (beta / "only-in-beta.md").write_text("b\n", encoding="utf-8")
    return alpha, beta


def _switching_app(root: Path, tmp_path: Path) -> Any:
    return create_app(
        Settings(
            workspace_root=root,
            claude_projects_dir=tmp_path / "projects",
            fake_agent=True,
        )
    )


def _new_session(client: TestClient) -> str:
    session_id: str = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
    return session_id


def _run_a_tool(client: TestClient, session_id: str) -> None:
    """One real turn on a real session socket: the fake reads the alphabetically
    first file in the session's folder, which is a file this workspace owns."""
    with client.websocket_connect(f"/ws/agent/{session_id}") as agent:
        agent.send_text(json.dumps({"type": "user_message", "text": "use tool"}))
        while json.loads(agent.receive_text())["type"] != "turn_done":
            pass


def _rows(client: TestClient) -> dict[str, dict[str, Any]]:
    payload = client.get("/api/activity").json()
    return {row["session_id"]: row for row in payload["sessions"]}


def test_a_window_nobody_re_announces_is_reported_gone(tmp_path: Path) -> None:
    """The service contract under that wiring: a row dropped by a switch is
    *announced*, because every client holds its own copy of this map."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    svc, bus = service(tmp_path)
    svc.note_session(session_id="s1", title="a session", folder="")
    svc.note_session(session_id="s2", title="another", folder="src")
    svc.set_workspace_root(elsewhere)
    assert svc.snapshot().sessions == []
    assert bus.frames()[-1].removed == ["s1", "s2"]


def test_a_switch_resets_the_count_of_what_the_fleet_view_dropped(tmp_path: Path) -> None:
    """``dropped_sessions`` is what this view has had to leave out. The view is
    the next workspace's now, and carrying the number over would report the
    project the user left in the one they opened."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    svc, _ = service(tmp_path, max_sessions=1)
    svc.note_session(session_id="s1", title="a session", folder="")
    svc.note_session(session_id="s2", title="another", folder="")
    assert svc.snapshot().dropped_sessions == 1
    svc.set_workspace_root(elsewhere)
    assert svc.snapshot().dropped_sessions == 0


def test_a_session_relabelled_by_a_switch_is_relabelled_in_the_feed(tmp_path: Path) -> None:
    """``note_session`` fires again after a switch carrying the label the session
    manager has just derived. A row that kept the old one would file a
    still-running agent under a folder of the project it has nothing to do
    with — the reason ``SessionInfo.folder`` is relabelled in the first place."""
    svc, bus = service(tmp_path)
    svc.note_session(session_id="s1", title="a session", folder="")
    before = len(bus.frames())
    svc.note_session(session_id="s1", title="a session", folder="/other/project")
    assert svc.snapshot().sessions[0].folder == "/other/project"
    assert len(bus.frames()) == before + 1, "a relabelled session was not news"


async def test_a_session_re_announced_after_a_switch_is_not_also_reported_gone(
    tmp_path: Path,
) -> None:
    """A row and its own id in ``removed``, in one frame, is a contradiction the
    client resolves by dropping the row (``ui/src/activity.ts`` applies removals
    last) — and it would eat the re-announcement every switch ends with.

    Asserted with a real loop because the collision only exists inside the
    coalescing window: without one, every change is its own frame.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bus = RecordingBus()
    svc = ActivityService(tmp_path, bus)
    svc.start()
    try:
        svc.note_session(session_id="s1", title="a session", folder="")
        assert len(bus.frames()) == 1  # a quiet fleet's first change goes out at once
        # Both of these land inside the window that frame opened, so they arrive
        # as one — which is exactly the arrangement a real switch produces.
        svc.set_workspace_root(elsewhere)
        svc.note_session(session_id="s1", title="a session", folder="src")
        await asyncio.sleep(ACTIVITY_WINDOW_S * 4)
        frame = bus.frames()[-1]
        assert frame.removed == [], "the row the switch re-announced was also reported gone"
        assert [row.session_id for row in frame.sessions] == ["s1"]
    finally:
        await svc.stop()


def test_a_switch_does_not_reshuffle_the_fleet_that_survives_it(tmp_path: Path) -> None:
    """A survivor keeps the age it had, because that is what orders the panel.

    Every survivor is re-announced inside one tick, so rebuilding the rows with
    "now" stamps them all with the same instant and "most recently active first"
    degrades to whatever order the announcements happened to arrive in — which
    is iteration order over the session manager's map, not a reading of the
    fleet. Here they come back oldest-first, which is exactly what that map can
    give you.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    svc, _ = service(tmp_path)
    svc.note_session(session_id="idle", title="idle for hours", folder="")
    svc.note_session(session_id="busy", title="just worked", folder="")
    assert [row.session_id for row in svc.snapshot().sessions] == ["busy", "idle"]

    svc.set_workspace_root(elsewhere)
    # Re-announced in the *opposite* order, which is the whole point: the
    # session manager iterates its own map, and nothing makes that order agree
    # with how recently each session did something.
    svc.note_session(session_id="busy", title="just worked", folder="")
    svc.note_session(session_id="idle", title="idle for hours", folder="")
    order = [row.session_id for row in svc.snapshot().sessions]
    assert order == ["busy", "idle"], "re-ordered by when the announcements arrived"


def test_a_worker_row_still_names_its_slot_after_a_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The naming half of the re-rooting rule, one indirection out.

    A Mission Control worker's cwd is a pooled slot outside the workspace, so
    the feed is handed the worktree pool root as a root it may *name*. That root
    is itself keyed on the workspace (``default_pool_root``) and ``WorktreeService``
    moves it on every switch — so a feed holding a copy of it answers
    ``(outside the workspace)`` for every worker row afterwards, which is this
    PR's own bug wearing a different hat.

    Driven through ``create_app`` rather than a hand-wired service because the
    thing that can regress is the *wiring*: a snapshot in ``main.py`` reads
    identically to a provider until a switch happens. And it is the wiring that
    makes re-deriving in ``set_workspace_root`` insufficient — ``WorktreeService``
    is an async rootable, so at that moment the pool has not moved yet.
    """
    monkeypatch.setattr(worktrees_service, "app_data_dir", lambda: tmp_path / "appdata")
    alpha, beta = _two_projects(tmp_path)
    app = _switching_app(alpha, tmp_path)
    with TestClient(app) as client:
        portal = client.portal
        assert portal is not None

        def worker_reads(session_id: str, call_id: str) -> str:
            """One worker's announced tool call, made *on the loop* — which is
            where a real session makes it, and what keeps the assertion below
            about the fix rather than about thread scheduling."""
            slot = app.state.worktrees.root / "slot-01"

            def note() -> None:
                app.state.activity.note_tool_started(
                    session_id=session_id,
                    session_title="a worker",
                    folder=slot,
                    folder_relative=slot.as_posix(),
                    call_id=call_id,
                    tool="Read",
                    tool_input={"file_path": str(slot / "server" / "main.py")},
                )

            portal.call(note)
            return str(_rows(client)[session_id]["entries"][0]["summary"])

        pool_before = app.state.worktrees.root
        assert worker_reads("w1", "c1") == "Read: slot-01/server/main.py"

        assert client.post("/api/workspace/switch", json={"path": str(beta)}).status_code == 200
        # Without this the test would pass on the bug: an unmoved pool root is
        # nameable by a stale copy of itself.
        assert app.state.worktrees.root != pool_before

        named = worker_reads("w2", "c2")
        assert named == "Read: slot-01/server/main.py", "naming the pool the user left"


def test_a_switch_re_jails_the_feed_against_the_new_workspace(tmp_path: Path) -> None:
    """The bug, at the level a user meets it: open a folder, work in it, and the
    fleet feed must name the files of the folder you are *in*.

    Before the fix the service kept the root it was constructed with, so every
    call made after a switch was normalized against a workspace the session's
    folder is not inside — and the panel's whole content became
    ``Read: (outside the workspace)`` with nothing left to click.
    """
    alpha, beta = _two_projects(tmp_path)
    with TestClient(_switching_app(alpha, tmp_path)) as client:
        before = _new_session(client)
        _run_a_tool(client, before)
        assert _rows(client)[before]["entries"][0]["target"] == "only-in-alpha.md"

        assert client.post("/api/workspace/switch", json={"path": str(beta)}).status_code == 200

        after = _new_session(client)
        _run_a_tool(client, after)
        entry = _rows(client)[after]["entries"][0]
        assert entry["summary"] == "Read: only-in-beta.md"
        assert entry["target"] == "only-in-beta.md", "the feed is still jailed against alpha"


def test_a_switch_does_not_leave_the_old_workspaces_activity_in_the_new_feed(
    tmp_path: Path,
) -> None:
    """The other half: what a surviving session's row says afterwards.

    A running agent is not killed by the user opening another folder
    (``SessionManager.set_workspace_root``), so its row stays — but every string
    in it was derived from the workspace being left. The entries go, because a
    ``target`` is what the panel *opens* and ``only-in-alpha.md`` in beta is a
    different file (or no file); the label follows the session manager's
    relabelling, which files a session outside the root under its own absolute
    path instead of under a same-named folder of the new project.
    """
    alpha, beta = _two_projects(tmp_path)
    with TestClient(_switching_app(alpha, tmp_path)) as client:
        survivor = _new_session(client)
        _run_a_tool(client, survivor)
        assert _rows(client)[survivor]["entries"] != []

        assert client.post("/api/workspace/switch", json={"path": str(beta)}).status_code == 200

        row = _rows(client)[survivor]
        assert row["entries"] == [], "alpha's tool calls are still in beta's feed"
        assert row["dropped"] == 0
        # Relabelled, not vanished: still listed, under the folder it is really
        # working in. This is also the ordering assertion — the service is
        # re-rooted *before* the session manager re-announces the live fleet, so
        # a wrong order in `create_app` loses the row entirely.
        assert row["folder"] == alpha.as_posix()
        assert client.get("/api/activity").json()["dropped_sessions"] == 0


def test_a_broken_activity_observer_cannot_abandon_a_workspace_switch(tmp_path: Path) -> None:
    """The other end of ``test_a_broken_activity_observer_cannot_wedge_the_shutdown``.

    Re-announcing the live fleet happens inside ``WorkspaceService.switch``,
    after the jail has moved and before the watcher has been restarted. An
    exception escaping it would leave the switch half-applied — a server whose
    jail and whose watch point at different projects — over a panel that failed
    to update. Here that failure would surface as a 500 from the switch.
    """
    alpha, beta = _two_projects(tmp_path)
    app = _switching_app(alpha, tmp_path)
    with TestClient(app) as client:
        session_id = _new_session(client)

        def boom(**_: Any) -> None:
            raise RuntimeError("the activity service is broken")

        app.state.activity.note_session = boom
        assert client.post("/api/workspace/switch", json={"path": str(beta)}).status_code == 200
        # And the rest of the switch still happened: the jail moved, so beta's
        # file reads and alpha's — the one the server was serving a moment ago —
        # does not.
        beta_file = client.get("/api/files/content", params={"path": "only-in-beta.md"})
        alpha_file = client.get("/api/files/content", params={"path": "only-in-alpha.md"})
        assert beta_file.status_code == 200
        assert alpha_file.status_code != 200
        assert app.state.session_manager.get(session_id) is not None


@pytest.mark.timeout(60)
def test_a_switch_is_published_to_windows_that_only_listen(tmp_path: Path) -> None:
    """The reset has to go out on the bus, which is where this differs from
    provenance and validation: ``ui/src/activity.ts`` adopts a switch through the
    frames on ``/ws/events`` and re-reads nothing on a workspace change, so a
    picture nobody corrected stays on screen in every open window.
    """
    alpha, beta = _two_projects(tmp_path)
    with (
        TestClient(_switching_app(alpha, tmp_path)) as client,
        client.websocket_connect("/ws/events") as events,
    ):
        live = _new_session(client)
        _run_a_tool(client, live)
        assert client.post("/api/workspace/switch", json={"path": str(beta)}).status_code == 200
        # Only the post-switch frame can match: before it, this session's row is
        # labelled "" (the old root) and holds the call it just ran.
        _drain(
            events,
            lambda seen: any(
                row["session_id"] == live
                and row["folder"] == alpha.as_posix()
                and row["entries"] == []
                for frame in seen
                for row in frame["sessions"]
            ),
        )
