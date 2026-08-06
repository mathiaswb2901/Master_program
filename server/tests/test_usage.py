"""Plan usage: what we report, and — mostly — what we refuse to report.

The feature's honesty is the thing under test. Three of the four caveats in
``services/usage.py`` are properties a test can hold:

* the never-emitted account (``buckets == []``, and the degraded view is all
  there is) — the case most likely to be a given user's whole experience, so it
  is first here and it is a first-class E2E journey too;
* a snapshot is stale until a live session talks to an agent, so every figure
  carries when it arrived and the snapshot carries its own age;
* we report the buckets the SDK gives — one window per event, nothing
  synthesized for the windows it never mentioned.

The fourth (no polling) is a property of the *source*, and the test for it is
that nothing in this module can ask for a value.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.usage import UsageBucketKind, UsageEvent, UsageStatus
from workbench_server.services import fake_agent
from workbench_server.services import usage as usage_module
from workbench_server.services.event_bus import EventBus
from workbench_server.services.usage import (
    BUCKET_ORDER,
    MAX_MODELS,
    STATUSES,
    UsageService,
    bucket_kind,
    usage_status,
)


class FakeClock:
    """Wall time under the test's control — staleness is a real rule here."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

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

    def usage_events(self) -> list[UsageEvent]:
        return [e for e in self.published if isinstance(e, UsageEvent)]


class Info:
    """A stand-in for ``claude_agent_sdk.RateLimitInfo``.

    Attribute access only, exactly like the service reads the real one — the
    SDK's dataclass is not importable in this environment and duck-typing is
    what keeps the fake path and the real path the same path.
    """

    def __init__(self, **fields: Any) -> None:
        self.status: Any = fields.get("status", "allowed")
        self.rate_limit_type: Any = fields.get("rate_limit_type")
        self.utilization: Any = fields.get("utilization")
        self.resets_at: Any = fields.get("resets_at")
        self.overage_status: Any = fields.get("overage_status")
        self.overage_resets_at: Any = fields.get("overage_resets_at")
        self.overage_disabled_reason: Any = fields.get("overage_disabled_reason")
        self.raw: Any = fields.get("raw", {})


def service() -> tuple[UsageService, RecordingBus, FakeClock]:
    bus = RecordingBus()
    clock = FakeClock()
    return UsageService(bus, clock=clock), bus, clock


# ---- caveat 3: the account that never emits ---------------------------------


def test_a_fresh_service_reports_nothing_rather_than_zero() -> None:
    """No event has arrived, so there is no plan usage — and "no data" must not
    look like "0% used"."""
    usage, bus, _ = service()
    snapshot = usage.snapshot()
    assert snapshot.buckets == []
    assert snapshot.observed_at is None
    assert snapshot.age_s is None
    assert snapshot.overage is None
    assert snapshot.session_cost.turns == 0
    assert snapshot.session_cost.observed_at is None
    assert bus.usage_events() == []  # nothing to announce, so nothing is


def test_turn_cost_is_the_whole_degraded_view() -> None:
    """The fallback: money this process watched go by, per model. Still not plan
    usage — ``buckets`` stays empty, which is what the UI labels it on."""
    usage, bus, clock = service()
    usage.note_turn(
        session_id="s1",
        cost_usd=0.0125,
        model_usage={
            "claude-opus-5": {"costUSD": 0.01, "inputTokens": 1200, "outputTokens": 300},
            "claude-haiku-5": {"costUSD": 0.0025, "inputTokens": 400, "outputTokens": 90},
        },
    )
    clock.advance(30)
    usage.note_turn(
        session_id="s1", cost_usd=0.005, model_usage={"claude-opus-5": {"costUSD": 0.005}}
    )

    snapshot = usage.snapshot()
    assert snapshot.buckets == []
    cost = snapshot.session_cost
    assert cost.turns == 2
    assert cost.total_cost_usd == 0.0175
    assert cost.last_turn_cost_usd == 0.005
    assert cost.observed_at == clock.now
    # Sorted by cost, so the model doing the work leads.
    assert [entry.model for entry in cost.models] == ["claude-opus-5", "claude-haiku-5"]
    assert cost.models[0].cost_usd == 0.015
    assert cost.models[0].input_tokens == 1200  # the second turn carried no tokens
    assert len(bus.usage_events()) == 2


def test_a_turn_that_reports_nothing_is_not_counted() -> None:
    """A turn with neither a cost nor model usage tells us nothing, so counting
    it would inflate ``turns`` — the one number the degraded card leans on."""
    usage, bus, _ = service()
    usage.note_turn(session_id="s1", cost_usd=None)
    assert usage.snapshot().session_cost.turns == 0
    assert bus.usage_events() == []


def test_per_model_totals_are_bounded() -> None:
    usage, _, _ = service()
    for index in range(MAX_MODELS + 5):
        usage.note_turn(
            session_id="s1", cost_usd=0.001, model_usage={f"model-{index}": {"costUSD": 0.001}}
        )
    assert len(usage.snapshot().session_cost.models) == MAX_MODELS


# ---- caveat 4: the buckets the SDK gives, and no others ---------------------


def test_one_event_describes_one_window() -> None:
    """The shape the SDK actually has: ``RateLimitInfo`` carries a single
    ``rate_limit_type``. A five-hour event says nothing about the week, and the
    week must not be invented from it."""
    usage, bus, clock = service()
    usage.note_rate_limit(
        Info(
            status="allowed", rate_limit_type="five_hour", utilization=0.42, resets_at=1_700_003_600
        )
    )

    snapshot = usage.snapshot()
    assert [bucket.kind for bucket in snapshot.buckets] == ["five_hour"]
    bucket = snapshot.buckets[0]
    assert bucket.status == "allowed"
    assert bucket.utilization == 0.42
    assert bucket.resets_at == 1_700_003_600
    assert bucket.observed_at == clock.now
    assert snapshot.observed_at == clock.now
    assert snapshot.age_s == 0.0
    assert len(bus.usage_events()) == 1


def test_windows_accumulate_and_keep_a_stable_order() -> None:
    """Several events build the picture, and the newest for a window replaces
    the older one. Order is presentation order, never arrival order — meters
    that reshuffle on a transition cannot be read at a glance."""
    usage, _, clock = service()
    usage.note_rate_limit(Info(status="allowed", rate_limit_type="seven_day", utilization=0.5))
    clock.advance(60)
    usage.note_rate_limit(Info(status="allowed", rate_limit_type="five_hour", utilization=0.1))
    clock.advance(60)
    usage.note_rate_limit(
        Info(status="allowed_warning", rate_limit_type="seven_day", utilization=0.81)
    )

    snapshot = usage.snapshot()
    assert [bucket.kind for bucket in snapshot.buckets] == ["five_hour", "seven_day"]
    weekly = snapshot.buckets[1]
    assert weekly.utilization == 0.81  # replaced, not appended
    assert weekly.status == "allowed_warning"
    assert weekly.observed_at == clock.now
    # Per-bucket stamps: the five-hour figure is a minute older than the weekly.
    assert snapshot.buckets[0].observed_at == clock.now - 60


def test_per_model_weeklies_are_reported_only_when_they_arrive() -> None:
    usage, _, _ = service()
    usage.note_rate_limit(
        Info(status="rejected", rate_limit_type="seven_day_opus", utilization=1.0)
    )
    kinds = [bucket.kind for bucket in usage.snapshot().buckets]
    assert kinds == ["seven_day_opus"]  # no sonnet row conjured next to it


def test_a_window_with_no_utilization_reports_status_only() -> None:
    """The SDK types ``utilization`` optional. None must survive to the wire —
    rendering a missing figure as an empty bar would read as "0% used"."""
    usage, _, _ = service()
    usage.note_rate_limit(Info(status="allowed_warning", rate_limit_type="five_hour"))
    bucket = usage.snapshot().buckets[0]
    assert bucket.utilization is None
    assert bucket.resets_at is None
    assert bucket.status == "allowed_warning"


def test_an_event_that_names_no_window_is_reported_as_unspecified() -> None:
    """``rate_limit_type=None`` is allowed by the SDK's own type. Dropping it
    would hide a limit signal; guessing which window it was would be synthesis."""
    usage, _, _ = service()
    usage.note_rate_limit(Info(status="rejected", rate_limit_type=None, utilization=1.0))
    assert [bucket.kind for bucket in usage.snapshot().buckets] == ["unspecified"]


def test_an_unknown_window_type_lands_on_unspecified_rather_than_being_dropped() -> None:
    assert bucket_kind("seven_day_sonnet") == "seven_day_sonnet"
    assert bucket_kind("some_future_window") == "unspecified"
    assert bucket_kind(None) == "unspecified"
    assert bucket_kind(7) == "unspecified"
    # Ours, never something an event may resolve to.
    assert bucket_kind("unspecified") == "unspecified"


def test_an_unusable_status_drops_the_event() -> None:
    """``status`` is the SDK's one required field. A value we do not recognize
    means we are not looking at what we think we are, so nothing is recorded."""
    usage, bus, _ = service()
    usage.note_rate_limit(Info(status="probably_fine", rate_limit_type="five_hour"))
    usage.note_rate_limit(Info(status=None))
    assert usage.snapshot().buckets == []
    assert bus.usage_events() == []
    assert usage_status("allowed") == "allowed"
    assert usage_status("nonsense") is None


def test_non_finite_figures_never_reach_the_wire() -> None:
    """NaN and infinity are not JSON. A CLI that sent one would otherwise fail
    the whole ``/ws/events`` fan-out, not just this frame."""
    usage, _, _ = service()
    usage.note_rate_limit(
        Info(
            status="allowed",
            rate_limit_type="five_hour",
            utilization=float("nan"),
            resets_at=float("inf"),
        )
    )
    bucket = usage.snapshot().buckets[0]
    assert bucket.utilization is None
    assert bucket.resets_at is None
    json.dumps(usage.snapshot().model_dump())  # would raise on a non-finite float


def test_overage_state_rides_another_window_and_is_reported_separately() -> None:
    """The SDK hangs ``overageStatus`` on whichever window's event it likes, so
    it is a field of the snapshot rather than of the bucket it arrived on."""
    usage, _, clock = service()
    usage.note_rate_limit(
        Info(
            status="allowed_warning",
            rate_limit_type="seven_day",
            utilization=0.9,
            overage_status="rejected",
            overage_resets_at=1_700_100_000,
            overage_disabled_reason="not enabled for this account",
        )
    )
    overage = usage.snapshot().overage
    assert overage is not None
    assert overage.status == "rejected"
    assert overage.resets_at == 1_700_100_000
    assert overage.disabled_reason == "not enabled for this account"
    assert overage.observed_at == clock.now


# ---- caveats 1 and 2: stale until you talk to an agent, and unpollable ------


def test_a_snapshot_ages_and_says_so() -> None:
    usage, _, clock = service()
    usage.note_rate_limit(Info(status="allowed", rate_limit_type="five_hour", utilization=0.3))
    observed = clock.now
    clock.advance(3 * 3600)

    snapshot = usage.snapshot()
    assert snapshot.age_s == 3 * 3600
    assert snapshot.observed_at == observed
    # The figure itself is untouched: stale means old, not wrong.
    assert snapshot.buckets[0].utilization == 0.3
    assert snapshot.buckets[0].observed_at == observed


def test_a_clock_that_goes_backwards_never_reports_a_negative_age() -> None:
    usage, _, clock = service()
    usage.note_rate_limit(Info(status="allowed", rate_limit_type="five_hour", utilization=0.3))
    clock.advance(-500)
    assert usage.snapshot().age_s == 0.0


def test_the_service_offers_no_way_to_ask_for_a_value() -> None:
    """Caveat 2, as an API property: there is nothing to poll. The only inputs
    are the two the message stream pushes."""
    inputs = {name for name in vars(UsageService) if not name.startswith("_")}
    # ``session_entry`` is a *read*, not a poll: it answers from what the stream
    # already pushed. It exists so Mission Control's per-worker budget is
    # enforced against the same figure this panel renders rather than a second
    # accumulator that could disagree with it.
    assert inputs == {"note_rate_limit", "note_turn", "session_entry", "snapshot"}


# ---- the tables the service and the schema share ---------------------------


def test_bucket_order_covers_every_kind_exactly_once() -> None:
    """``BUCKET_ORDER`` is spelled out for typing, so it can drift from the
    Literal it mirrors. It may not."""
    from typing import get_args

    assert set(BUCKET_ORDER) == set(get_args(UsageBucketKind))
    assert len(BUCKET_ORDER) == len(set(BUCKET_ORDER))
    assert set(STATUSES) == set(get_args(UsageStatus))
    # The window that bites first leads the meters.
    assert BUCKET_ORDER[0] == "five_hour"


# ---- the seam: SDK stream -> bus -> /ws/events -> GET /api/usage ------------


def _app(tmp_path: Path) -> Any:
    # ``log_level`` is deliberately left out: ``create_app`` configures logging
    # from these settings, and the logging test below has to run against whatever
    # level the app really ships with rather than one restated here.
    return create_app(
        Settings(
            workspace_root=tmp_path,
            claude_projects_dir=tmp_path / "projects",
            fake_agent=True,
        )
    )


def test_a_rate_limit_event_flows_from_the_sdk_seam_to_both_readers(tmp_path: Path) -> None:
    """The whole pipe, through the production wiring: the fake client puts
    ``RateLimitEvent`` messages on a real session's stream, ``AgentSession``
    translates them at the same seam it translates everything else, the service
    publishes on the shared bus, ``/ws/events`` carries it to every window, and
    ``GET /api/usage`` answers the same picture for a window that just loaded.
    """
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/api/usage").json()["buckets"] == []  # nothing yet

        with client.websocket_connect("/ws/events") as events:
            local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
            with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
                agent.send_text(json.dumps({"type": "user_message", "text": "usage please"}))
                while json.loads(agent.receive_text())["type"] != "turn_done":
                    pass

            pushed: list[dict[str, Any]] = []
            while len(pushed) < len(fake_agent.FAKE_RATE_LIMITS):
                frame = json.loads(events.receive_text())
                if frame["type"] == "usage":
                    pushed.append(frame)

        # One frame per transition, each carrying the whole snapshot so far.
        assert [len(frame["snapshot"]["buckets"]) for frame in pushed] == [1, 2, 3]

        snapshot = client.get("/api/usage").json()
        assert [bucket["kind"] for bucket in snapshot["buckets"]] == [
            kind for kind, _, _, _ in fake_agent.FAKE_RATE_LIMITS
        ]
        five_hour = snapshot["buckets"][0]
        assert five_hour["status"] == "allowed"
        assert five_hour["utilization"] == 0.42
        assert five_hour["resets_at"] > five_hour["observed_at"]
        assert snapshot["buckets"][2]["status"] == "rejected"
        assert snapshot["overage"]["status"] == fake_agent.FAKE_OVERAGE_STATUS
        assert snapshot["overage"]["disabled_reason"] == fake_agent.FAKE_OVERAGE_REASON
        assert snapshot["observed_at"] is not None
        assert 0 <= snapshot["age_s"] < 60
        # The degraded figures are still gathered while plan usage is known.
        assert snapshot["session_cost"]["turns"] == 1
        assert [entry["model"] for entry in snapshot["session_cost"]["models"]] == [
            fake_agent.FAKE_MODEL
        ]


def test_a_workspace_that_never_talks_to_an_agent_serves_the_empty_snapshot(
    tmp_path: Path,
) -> None:
    """The degraded path through the real endpoint — and the reason the UI has
    to render it: this is what a browser gets on load, every time, until a
    session emits."""
    with TestClient(_app(tmp_path)) as client:
        snapshot = client.get("/api/usage").json()
    assert snapshot == {
        "buckets": [],
        "overage": None,
        "observed_at": None,
        "age_s": None,
        "session_cost": {
            "turns": 0,
            "total_cost_usd": 0.0,
            "last_turn_cost_usd": None,
            "models": [],
            "observed_at": None,
        },
        # No session has finished a turn, so there is nothing per-session either:
        # absent, never a row of measured zeroes.
        "sessions": [],
    }


def test_nothing_is_written_to_disk(tmp_path: Path) -> None:
    """Live state about an account, not workspace data. A restart forgets it."""
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    with TestClient(_app(tmp_path)) as client:
        local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
        with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
            agent.send_text(json.dumps({"type": "user_message", "text": "usage please"}))
            while json.loads(agent.receive_text())["type"] != "turn_done":
                pass
        assert client.get("/api/usage").json()["buckets"] != []

    workbench_dir = tmp_path / ".workbench"
    written = sorted(p.name for p in workbench_dir.iterdir()) if workbench_dir.is_dir() else []
    assert "usage.json" not in written
    with TestClient(_app(tmp_path)) as fresh:
        assert fresh.get("/api/usage").json()["buckets"] == []


# ---- the other half of "nothing is persisted" -------------------------------


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """``create_app`` configures structlog from its ``Settings``, and nothing
    else in this suite does. Put the defaults back afterwards rather than leaking
    a level and a renderer into every test that happens to run later."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _drive_usage_turn(client: TestClient) -> None:
    """One real turn through the fake client: three ``RateLimitEvent`` messages
    arrive at the SDK seam exactly as they do in production."""
    local_id = client.post("/api/agents/sessions", json={"folder": ""}).json()["session_id"]
    with client.websocket_connect(f"/ws/agent/{local_id}") as agent:
        agent.send_text(json.dumps({"type": "user_message", "text": fake_agent.USAGE_TRIGGER}))
        while json.loads(agent.receive_text())["type"] != "turn_done":
            pass


def test_no_usage_figure_is_logged_at_the_default_level(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], restore_logging: None
) -> None:
    """The disk ``test_nothing_is_written_to_disk`` does not look at — and the
    only one an end user actually has.

    We write no file of our own, but we do not have to in order to persist this.
    The packaged desktop shell runs this server as a child process and copies
    every line of its **stdout** into ``shell.log``, appended across restarts
    (``desktop/src-tauri/src/backend.rs``: ``pump()`` and ``open_log()``).
    structlog's default logger factory prints to stdout and ``configure_logging``
    never replaces it, so anything logged at or above ``Settings.log_level``
    lands in that file. That level defaults to ``info`` and the packaged shell is
    the mode a real user runs — so an account's true utilization and reset times
    logged at ``info`` are exactly the persisted plan-usage telemetry this
    module's docstring, ``models/usage.py``, ARCHITECTURE.md and ROADMAP item 11
    all promise does not exist.

    ``capfd`` rather than ``capsys``: fd 1 is precisely what ``pump()`` reads.
    """
    assert Settings().log_level == "info", "the shipped default this test is about"

    with TestClient(_app(tmp_path)) as client:
        _drive_usage_turn(client)
        buckets = client.get("/api/usage").json()["buckets"]

    # The figures did reach the snapshot — in memory, which is where they belong.
    # Without this the silence below would also be satisfied by a run in which
    # nothing was ever observed.
    assert [bucket["utilization"] for bucket in buckets] == [
        utilization for _, _, utilization, _ in fake_agent.FAKE_RATE_LIMITS
    ]

    out = capfd.readouterr().out

    # Capture really is watching the stream structlog prints to: the app's own
    # info-level shutdown line is in here. Silence only means something if noise
    # would have shown up, and this is what stops a capture that quietly stopped
    # working from reading as a pass.
    assert "workbench.stopped" in out

    assert "usage.rate_limit" not in out
    for field in ("utilization", "resets_at"):
        assert field not in out, f"{field} was logged at the default level"
    for _kind, _status, utilization, _resets in fake_agent.FAKE_RATE_LIMITS:
        assert str(utilization) not in out, "a usage figure was logged under another name"


def test_the_rate_limit_diagnostic_is_debug_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where the figures *are* allowed to go — and the reason the line survives.

    The test above proves nothing reaches stdout at the shipped level. This one
    pins why that is stable: the line is a ``debug`` diagnostic, the same gate
    ``_log_unmodeled`` already sits behind, so a wrong meter stays diagnosable by
    whoever explicitly asks — and the way back to ``info`` is a deliberate edit
    that fails here.

    Asserted against the module's logger rather than by reconfiguring structlog
    mid-process, because that does not work: ``configure_logging`` sets
    ``cache_logger_on_first_use``, so a logger already bound at ``info`` by an
    earlier test ignores a later ``configure()`` and the check would pass or fail
    on test ordering.
    """
    logged: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        """Records the level a call was made at, not just its fields."""

        def __getattr__(self, level: str) -> Any:
            def record(event: str, **fields: Any) -> None:
                logged.append((f"{level} {event}", fields))

            return record

    monkeypatch.setattr(usage_module, "log", Recorder())
    usage, _, _ = service()
    usage.note_rate_limit(
        Info(
            status="allowed_warning",
            rate_limit_type="seven_day",
            utilization=0.86,
            resets_at=1_700_007_200,
        )
    )

    # Exactly one line, at debug — nothing about this event is logged higher.
    assert [entry for entry, _ in logged] == ["debug usage.rate_limit"]
    assert logged[0][1]["utilization"] == 0.86
