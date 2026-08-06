"""Mission Control's orchestrator: the crew, the ceilings, and the reaping.

Driven against a **real** git repository and the **real** worktree pool, with
the scripted client seam on the far side of it — the same seam the E2E suite
uses. What is faked here is the model, never the mechanism: real sessions, real
``git worktree add``, real leases, real permission futures.

The four constraints the ROADMAP made non-negotiable each get a test named after
them, because each is a promise a later edit could quietly break:

* **never auto-allow shell** — a worker's ``Bash`` blocks on a human, its prompt
  reaches ``/ws/events`` where the board can see it, and nothing in the
  orchestrator can answer it;
* **a budget that binds** — every ceiling refuses *before* a worker exists, and
  every refusal names the setting that raises it;
* **bounded concurrency** — the session cap and the pool cap are honoured, and
  each produces its own honest sentence;
* **a worker's death is not the orchestrator's** — and stopping the orchestrator
  reaps its crew and gives the slots back.
"""

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.agents import SessionKind
from workbench_server.models.orchestrator import (
    OrchestratorBudget,
    SpawnRefusal,
    WorkerInfo,
)
from workbench_server.services.agent_sessions import (
    SdkClient,
    SessionBridge,
    SessionManager,
)
from workbench_server.services.agent_tools import (
    LIST_WORKERS,
    ORCHESTRATOR_TOOLS,
    SPAWN_WORKER,
    allowed_tool_names,
    handle_list_workers,
    handle_read_worker,
    handle_send_to_worker,
    handle_spawn_worker,
    handle_stop_worker,
    tools_for,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.fake_agent import SPAWN_TRIGGER
from workbench_server.services.orchestrator import (
    SETTING_MAX_SESSIONS,
    SETTING_MAX_WORKERS,
    SETTING_POOL_SIZE,
    OrchestratorService,
)
from workbench_server.services.usage import UsageService
from workbench_server.services.worktrees import WorktreeService

pytestmark = pytest.mark.timeout(180)


# ---- a real repository, and a scripted worker -------------------------------


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - arguments are this file's own literals
        ["git", *args],  # noqa: S607 - git is on PATH wherever this suite runs
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr or done.stdout}"
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@workbench.invalid")
    _git(root, "config", "user.name", "Workbench Test")
    (root / "model.py").write_bytes(b"VERSION = 1\n")
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-m", "first")
    return root


class ResultMessage:
    """The SDK's turn terminator, duck-typed like every other fake here."""

    def __init__(self, cost: float | None = 0.0, is_error: bool = False) -> None:
        self.session_id = "sdk-1"
        self.total_cost_usd = cost
        self.is_error = is_error
        self.model_usage: dict[str, Any] = {}


class StreamEvent:
    def __init__(self, text: str) -> None:
        self.event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        }


class ScriptedWorker:
    """A worker that answers, and — by mode — asks, holds, or dies."""

    def __init__(self, bridge: SessionBridge, mode: str, gate: asyncio.Event) -> None:
        self._bridge = bridge
        self._mode = mode
        self._gate = gate
        self.prompts: list[str] = []
        self.interrupted = False
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        yield StreamEvent("working on it")
        if self._mode == "ask":
            allowed = await self._bridge.ask_permission("Bash", {"command": "rm -rf /"})
            yield StreamEvent(f" permission={allowed}")
        if self._mode == "hold":
            # Stays in ``working`` until the test lets go — the only way to
            # observe a *concurrency* cap, which counts working sessions.
            await self._gate.wait()
        if self._mode == "die":
            # What a worker dying mid-turn really looks like: the exception is
            # raised inside ``_run_turn``, which turns it into agent_error +
            # turn_done(is_error) rather than letting it escape the session.
            raise RuntimeError("the worker fell over")
        yield ResultMessage()

    async def interrupt(self) -> None:
        self.interrupted = True
        self._gate.set()

    async def disconnect(self) -> None:
        self.disconnected = True


def scripted_factory(mode: str = "plain") -> Any:
    created: list[ScriptedWorker] = []
    gate = asyncio.Event()

    def factory(
        folder: Path,
        resume: str | None,
        bridge: SessionBridge,
        kind: SessionKind = "chat",
    ) -> SdkClient:
        # The orchestrator itself always answers plainly: what these modes
        # script is the *worker's* behaviour.
        client = ScriptedWorker(bridge, "plain" if kind == "orchestrator" else mode, gate)
        created.append(client)
        return client

    factory.created = created  # type: ignore[attr-defined]
    factory.gate = gate  # type: ignore[attr-defined]
    return factory


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)
        super().publish(event)

    def of_type(self, name: str) -> list[Any]:
        return [e for e in self.events if getattr(e, "type", "") == name]


def budget(**overrides: Any) -> OrchestratorBudget:
    base = {
        "max_workers": 4,
        "max_worker_turns": 40,
        "max_worker_cost_usd": 5.0,
        "max_fleet_turns": 200,
        "max_fleet_cost_usd": 20.0,
    }
    base.update(overrides)
    return OrchestratorBudget(**base)  # type: ignore[arg-type]


class Fleet:
    """Everything one test needs, wired the way ``main.py`` wires it."""

    def __init__(
        self,
        service: OrchestratorService,
        sessions: SessionManager,
        pool: WorktreeService,
        usage: UsageService,
        bus: RecordingBus,
        factory: Any,
    ) -> None:
        self.service = service
        self.sessions = sessions
        self.pool = pool
        self.usage = usage
        self.bus = bus
        self.factory = factory

    def orchestrator(self) -> str:
        session = self.sessions.create("", kind="orchestrator")
        return session.local_id


@pytest.fixture
async def fleet(repo: Path, tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Fleet]:
    """The service under test, with a real pool over a real repository."""
    marker = request.node.get_closest_marker("fleet")
    options: dict[str, Any] = marker.kwargs if marker else {}
    bus = RecordingBus()
    pool = WorktreeService(
        repo,
        bus,
        pool_root=tmp_path / "pool",
        capacity=options.get("pool_size", 4),
        lease_seconds=600.0,
    )
    await pool.start()
    usage = UsageService(bus)
    service = OrchestratorService(bus, usage, options.get("budget", budget()), pool)
    factory = scripted_factory(options.get("mode", "plain"))
    sessions = SessionManager(
        repo,
        factory,
        options.get("max_sessions", 8),
        event_publisher=bus,
        usage_observer=usage,
    )
    service.bind(sessions)
    yield Fleet(service, sessions, pool, usage, bus, factory)
    await service.stop_all()
    await sessions.close_all()
    await pool.stop()


async def settle() -> None:
    """Let the worker's turn task and the orchestrator's pump run."""
    for _ in range(12):
        await asyncio.sleep(0.02)


# ---- spawn, list, read, send, stop ------------------------------------------


async def test_spawn_binds_a_worker_to_its_own_worktree_slot(fleet: Fleet) -> None:
    """The whole point of the pool: two workers, two checkouts, no collision."""
    boss = fleet.orchestrator()
    first = await fleet.service.spawn(boss, "task one")
    second = await fleet.service.spawn(boss, "task two")
    assert isinstance(first, WorkerInfo)
    assert isinstance(second, WorkerInfo)
    assert first.slot != second.slot
    assert Path(first.path).is_dir()
    assert Path(second.path).is_dir()
    # And each really is a *session*, in that directory — the board's card, the
    # activity row and the agent pane all key off this same id.
    assert fleet.sessions.get(first.worker_id) is not None
    assert fleet.sessions.get(first.worker_id).folder == Path(first.path)  # type: ignore[union-attr]
    # The task became the worker's first message rather than a label on a row.
    await settle()
    assert fleet.factory.created[-1].prompts == ["task two"]


async def test_a_worker_is_never_created_through_the_http_path(repo: Path, tmp_path: Path) -> None:
    """``kind="worker"`` is refused at the schema, so the jail cannot be dodged.

    A worker's cwd is a pooled slot outside the workspace. If a client could ask
    for one, ``POST /api/agents/sessions`` would be a way to run an agent
    anywhere on disk — so the narrowing on ``CreateSessionRequest`` is the
    security boundary and this is the test of it.
    """
    settings = Settings(workspace_root=repo, fake_agent=True, worktree_root=tmp_path / "pool")
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.post("/api/agents/sessions", json={"folder": "", "kind": "worker"})
        assert refused.status_code == 422
        allowed = await client.post(
            "/api/agents/sessions", json={"folder": "", "kind": "orchestrator"}
        )
        assert allowed.status_code == 200
        assert allowed.json()["kind"] == "orchestrator"


async def test_list_read_send_and_stop_walk_one_worker_through_its_life(
    fleet: Fleet,
) -> None:
    boss = fleet.orchestrator()
    listing = handle_list_workers(fleet.service, boss, lambda _id: 0.0)
    # "None" said out loud, with the next step — CLAUDE.md's three shapes.
    assert "no workers" in listing["content"][0]["text"]

    spawned = await handle_spawn_worker(fleet.service, boss, {"task": "count to three"})
    assert spawned.get("is_error") is not True
    worker_id = fleet.service.workers_of(boss)[0].worker_id
    await settle()

    listing = handle_list_workers(fleet.service, boss, lambda _id: 0.25)
    text = listing["content"][0]["text"]
    assert worker_id in text
    assert "count to three" in text
    assert "$0.25" in text
    assert f"1/{fleet.service.budget.max_workers} running" in text

    read = handle_read_worker(fleet.service, boss, {"worker_id": worker_id})
    assert "working on it" in read["content"][0]["text"]

    sent = handle_send_to_worker(fleet.service, boss, {"worker_id": worker_id, "text": "again"})
    assert sent.get("is_error") is not True
    await settle()
    assert "again" in fleet.factory.created[-1].prompts

    stopped = await handle_stop_worker(fleet.service, boss, {"worker_id": worker_id})
    assert worker_id in stopped["content"][0]["text"]
    assert fleet.service.workers_of(boss)[0].outcome == "stopped"


async def test_read_says_what_it_withheld_and_how_to_widen(fleet: Fleet) -> None:
    """Truncation with a stated size and an escape hatch (CLAUDE.md)."""
    boss = fleet.orchestrator()
    worker = await fleet.service.spawn(boss, "long one")
    assert isinstance(worker, WorkerInfo)
    await settle()
    narrow = fleet.service.read(boss, worker.worker_id, 5)
    assert "withheld" in narrow
    assert "raise `chars`" in narrow


async def test_read_says_none_rather_than_returning_blankness(fleet: Fleet) -> None:
    boss = fleet.orchestrator()
    worker = await fleet.service.spawn(boss, "quiet one")
    assert isinstance(worker, WorkerInfo)
    # Deliberately before ``settle`` — nothing has streamed yet.
    assert "no output yet" in fleet.service.read(boss, worker.worker_id, 2_000)


async def test_one_orchestrator_cannot_touch_anothers_worker(fleet: Fleet) -> None:
    """Scoping, not decoration: ``_resolve`` is keyed by the parent."""
    first = fleet.orchestrator()
    second = fleet.orchestrator()
    worker = await fleet.service.spawn(first, "mine")
    assert isinstance(worker, WorkerInfo)
    assert "no worker" in fleet.service.read(second, worker.worker_id, 100)
    assert "no worker" in str(fleet.service.send(second, worker.worker_id, "hi"))
    assert "no worker" in await fleet.service.stop_worker(second, worker.worker_id)
    assert fleet.service.workers_of(first)[0].outcome is None


# ---- never auto-allow shell --------------------------------------------------


def test_a_worker_gets_no_shell_and_no_orchestrator_toolset() -> None:
    """The permission story, asserted on the allow-list the SDK is handed.

    ``Bash`` is absent for every kind, so a worker's shell request falls through
    to ``can_use_tool`` and becomes a human decision. And a worker carries the
    *base* toolset only — a worker that could spawn workers is a fork bomb with
    a budget attached.
    """
    for kind in ("chat", "orchestrator", "worker"):
        names = allowed_tool_names(kind)  # type: ignore[arg-type]
        assert not any("Bash" in name for name in names)
    names_of = lambda kind: [spec.name for spec in tools_for(kind)]  # noqa: E731
    assert names_of("worker") == names_of("chat")
    assert set(names_of("orchestrator")) > set(names_of("chat"))
    # And the five extra tools are exactly the orchestrator's.
    assert {spec.name for spec in ORCHESTRATOR_TOOLS} == {
        "spawn_worker",
        "list_workers",
        "read_worker",
        "send_to_worker",
        "stop_worker",
    }


def test_the_orchestrator_service_has_no_way_to_answer_a_permission() -> None:
    """Enforced by absence, so this is the test that the absence is deliberate.

    An orchestrator that could resolve its own workers' prompts would be able to
    grant them shell access without a human — a different product with a
    different threat model. If a future edit adds such a call, this fails.
    """
    source = Path(OrchestratorService.__module__.replace(".", "/") + ".py")
    text = (Path("server/src") / source).read_text(encoding="utf-8")
    assert "resolve_permission" not in text
    assert "PermissionDecision" not in text


@pytest.mark.fleet(mode="ask")
async def test_a_blocked_worker_escalates_to_the_board(fleet: Fleet) -> None:
    """The gap Mission Control closes, end to end over the shared bus.

    Nobody has opened this worker's chat — nobody ever will — so the only way a
    human learns it is blocked is this frame. It carries the description as-is:
    an approval the user cannot read is one they cannot give informedly.
    """
    boss = fleet.orchestrator()
    worker = await fleet.service.spawn(boss, "please ask")
    assert isinstance(worker, WorkerInfo)
    await settle()

    frames = fleet.bus.of_type("session_permission")
    blocked = [f for f in frames if f.session.session_id == worker.worker_id and f.session.pending]
    assert blocked, "a blocked worker must reach the board"
    pending = blocked[-1].session.pending[0]
    assert pending.tool == "Bash"
    assert "rm -rf /" in pending.description
    assert blocked[-1].session.kind == "worker"

    # And the fleet-wide snapshot agrees, which is the reconnect path.
    assert [row.session_id for row in fleet.sessions.pending_permissions()] == [worker.worker_id]

    # Answered from the board — the second door onto the same future.
    session = fleet.sessions.get(worker.worker_id)
    assert session is not None
    assert session.resolve_permission(pending.request_id, False) is True
    await settle()
    # Retracted everywhere, and a second click is refused rather than reported
    # as a decision that changed something.
    assert fleet.sessions.pending_permissions() == []
    assert session.resolve_permission(pending.request_id, True) is False


# ---- a budget that binds -----------------------------------------------------


@pytest.mark.fleet(budget=budget(max_workers=1))
async def test_the_worker_cap_refuses_before_anything_is_created(fleet: Fleet) -> None:
    boss = fleet.orchestrator()
    assert isinstance(await fleet.service.spawn(boss, "one"), WorkerInfo)
    refusal = await fleet.service.spawn(boss, "two")
    assert isinstance(refusal, SpawnRefusal)
    assert refusal.reason == "worker_limit"
    assert refusal.setting == SETTING_MAX_WORKERS
    assert refusal.limit == 1
    assert refusal.observed == 1
    # Nothing was created and no slot was taken — the check is *before*.
    assert len(fleet.service.workers_of(boss)) == 1
    assert sum(1 for s in fleet.pool.snapshot().slots if s.state == "leased") == 1
    # And the pane can render it: the cap, and the way out.
    assert SETTING_MAX_WORKERS in refusal.detail


@pytest.mark.fleet(budget=budget(max_fleet_cost_usd=0.10))
async def test_the_fleet_cost_ceiling_counts_a_stopped_workers_spend(fleet: Fleet) -> None:
    """A budget that forgot a dead worker's cost could be spent many times."""
    boss = fleet.orchestrator()
    worker = await fleet.service.spawn(boss, "expensive")
    assert isinstance(worker, WorkerInfo)
    fleet.usage.note_turn(session_id=worker.worker_id, cost_usd=0.50)
    await fleet.service.stop_worker(boss, worker.worker_id)

    refusal = await fleet.service.spawn(boss, "another")
    assert isinstance(refusal, SpawnRefusal)
    assert refusal.reason == "fleet_cost"
    assert refusal.observed == pytest.approx(0.50)
    assert "WORKBENCH_ORCHESTRATOR_FLEET_COST_USD" in refusal.detail


@pytest.mark.fleet(budget=budget(max_worker_turns=1))
async def test_the_per_worker_turn_ceiling_refuses_more_work(fleet: Fleet) -> None:
    """Checked before *more* work, not before creation: a worker's first turn
    has by definition cost nothing."""
    boss = fleet.orchestrator()
    worker = await fleet.service.spawn(boss, "first")
    assert isinstance(worker, WorkerInfo)
    fleet.usage.note_turn(session_id=worker.worker_id, cost_usd=0.01)

    refusal = fleet.service.send(boss, worker.worker_id, "again")
    assert isinstance(refusal, SpawnRefusal)
    assert refusal.reason == "worker_turns"
    assert refusal.setting == "WORKBENCH_ORCHESTRATOR_WORKER_TURNS"
    # The board sees the cap without opening the orchestrator's chat.
    snapshot = fleet.service.snapshot()
    assert snapshot.orchestrators[0].last_refusal is not None
    assert snapshot.orchestrators[0].last_refusal.reason == "worker_turns"


async def test_the_budget_reads_the_same_numbers_the_board_renders(fleet: Fleet) -> None:
    """One authority for cost: the usage service, not a counter of our own."""
    boss = fleet.orchestrator()
    worker = await fleet.service.spawn(boss, "work")
    assert isinstance(worker, WorkerInfo)
    fleet.usage.note_turn(session_id=worker.worker_id, cost_usd=1.25)
    entry = fleet.usage.snapshot().sessions[0]
    assert entry.session_id == worker.worker_id
    assert entry.cost_usd == 1.25
    assert fleet.service.workers_of(boss)[0].turns == entry.turns == 1


# ---- bounded concurrency -----------------------------------------------------


@pytest.mark.fleet(max_sessions=1, mode="hold")
async def test_the_session_cap_is_honoured_and_named(fleet: Fleet) -> None:
    """A cap this service does not own, asked rather than duplicated.

    The first worker holds its turn open, so it is genuinely *working* — which
    is the only state the concurrency cap counts, and the reason the refusal
    says so out loud instead of "1 of 1 sessions".
    """
    boss = fleet.orchestrator()
    assert isinstance(await fleet.service.spawn(boss, "hold on"), WorkerInfo)
    await settle()
    assert fleet.sessions.active_count() == 1

    refusal = await fleet.service.spawn(boss, "no room")
    assert isinstance(refusal, SpawnRefusal)
    assert refusal.reason == "session_limit"
    assert refusal.setting == SETTING_MAX_SESSIONS
    assert "working, not sessions open" in refusal.detail
    # And no slot was borrowed for the worker that was never created.
    assert sum(1 for s in fleet.pool.snapshot().slots if s.state == "leased") == 1


@pytest.mark.fleet(pool_size=1)
async def test_an_exhausted_pool_is_a_refusal_not_a_shared_checkout(fleet: Fleet) -> None:
    """Never a quiet fallback to the workspace: two workers in one checkout is
    exactly the failure the pool exists to prevent."""
    boss = fleet.orchestrator()
    assert isinstance(await fleet.service.spawn(boss, "one"), WorkerInfo)
    refusal = await fleet.service.spawn(boss, "two")
    assert isinstance(refusal, SpawnRefusal)
    assert refusal.reason == "no_worktree"
    assert refusal.setting == SETTING_POOL_SIZE
    assert len(fleet.service.workers_of(boss)) == 1


async def test_no_pool_means_no_workers(tmp_path: Path) -> None:
    """A server with no worktree service refuses honestly rather than sharing."""
    bus = RecordingBus()
    usage = UsageService(bus)
    service = OrchestratorService(bus, usage, budget(), None)
    service.bind(SessionManager(tmp_path, scripted_factory(), 4, event_publisher=bus))
    refusal = await service.spawn("boss", "anything")
    assert isinstance(refusal, SpawnRefusal)
    assert refusal.reason == "unavailable"
    assert "nowhere safe to work" in refusal.detail


# ---- death, and reaping ------------------------------------------------------


@pytest.mark.fleet(mode="die")
async def test_a_worker_that_dies_mid_turn_does_not_take_the_fleet_with_it(
    fleet: Fleet,
) -> None:
    """The orchestrator keeps running, the failure is recorded, and the next
    spawn still works."""
    boss = fleet.orchestrator()
    worker = await fleet.service.spawn(boss, "fall over")
    assert isinstance(worker, WorkerInfo)
    await settle()

    recorded = fleet.service.workers_of(boss)[0]
    assert recorded.outcome == "failed"
    assert recorded.detail is not None
    assert "fell over" in recorded.detail
    # The orchestrator session itself is untouched, and can spawn again.
    assert fleet.sessions.get(boss) is not None
    again = await fleet.service.spawn(boss, "try again")
    assert isinstance(again, WorkerInfo)
    # A failed worker keeps its slot: its checkout is usually the interesting
    # thing on the machine, and the pool parks a dirty slot rather than resetting.
    assert again.slot != recorded.slot


async def test_stopping_the_orchestrator_reaps_its_crew_and_frees_the_slots(
    fleet: Fleet,
) -> None:
    """The anti-orphaning rule. A worker outliving its orchestrator is an agent
    nobody is watching, holding a slot nobody will release."""
    boss = fleet.orchestrator()
    first = await fleet.service.spawn(boss, "one")
    second = await fleet.service.spawn(boss, "two")
    assert isinstance(first, WorkerInfo)
    assert isinstance(second, WorkerInfo)
    await settle()
    assert sum(1 for s in fleet.pool.snapshot().slots if s.state == "leased") == 2

    assert await fleet.service.stop(boss) == 2
    await settle()

    assert all(w.outcome == "stopped" for w in fleet.service.workers_of(boss))
    assert fleet.sessions.get(first.worker_id) is None
    assert fleet.sessions.get(second.worker_id) is None
    assert sum(1 for s in fleet.pool.snapshot().slots if s.state == "leased") == 0
    # Idempotent: a second stop is not an error and releases nothing twice.
    assert await fleet.service.stop(boss) == 0


async def test_stop_all_reaps_every_orchestrator(fleet: Fleet) -> None:
    """The shutdown hook, which is why it runs *before* ``close_all``."""
    first = fleet.orchestrator()
    second = fleet.orchestrator()
    assert isinstance(await fleet.service.spawn(first, "a"), WorkerInfo)
    assert isinstance(await fleet.service.spawn(second, "b"), WorkerInfo)
    await fleet.service.stop_all()
    assert sum(1 for s in fleet.pool.snapshot().slots if s.state == "leased") == 0


# ---- the board's own inputs --------------------------------------------------


async def test_the_snapshot_carries_the_ceilings_the_pane_has_to_render(
    fleet: Fleet,
) -> None:
    boss = fleet.orchestrator()
    assert isinstance(await fleet.service.spawn(boss, "work"), WorkerInfo)
    snapshot = fleet.service.snapshot()
    assert snapshot.budget.max_workers == 4
    assert snapshot.max_concurrent_sessions == 8
    assert snapshot.worktree_capacity == 4
    assert [o.orchestrator_id for o in snapshot.orchestrators] == [boss]
    # And it went out on the bus, so a board that never polls still updates.
    assert fleet.bus.of_type("orchestrator")


def test_a_workers_activity_and_prompt_reach_the_board_over_the_shared_bus(
    repo: Path, tmp_path: Path
) -> None:
    """Integration: a worker's tool call *and* its permission prompt arrive on
    ``/ws/events`` — the two things the board is made of.

    A worker's chat frames go down ``/ws/agent/{id}``, a socket nobody ever
    opens for a worker. This is the path that makes "what is it doing right
    now" and "it is blocked on you" true, and it is asserted through **real**
    WebSockets against the real app, because the coalescer, the bus and the
    router are each a place a frame could be lost.

    Sync (``TestClient``) on purpose: the whole point is to drive the app the
    way a browser does, from outside its own event loop.
    """
    settings = Settings(
        workspace_root=repo,
        fake_agent=True,
        worktree_root=tmp_path / "pool",
        max_concurrent_sessions=8,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/agents/sessions", json={"folder": "", "kind": "orchestrator"})
        boss = created.json()["session_id"]
        with (
            client.websocket_connect("/ws/events") as events,
            client.websocket_connect(f"/ws/agent/{boss}") as chat,
        ):
            # The fake orchestrator really calls the real service, which really
            # borrows slots and really starts sessions.
            chat.send_json({"type": "user_message", "text": SPAWN_TRIGGER})
            activity: dict[str, list[str]] = {}
            prompts: dict[str, str] = {}
            for _ in range(200):
                frame = events.receive_json()
                if frame.get("type") == "session_activity":
                    for row in frame["sessions"]:
                        if row["session_id"] == boss:
                            continue
                        activity.setdefault(row["session_id"], []).extend(
                            entry["summary"] for entry in row["entries"]
                        )
                elif frame.get("type") == "session_permission":
                    row = frame["session"]
                    if row["pending"] and row["kind"] == "worker":
                        prompts[row["session_id"]] = row["pending"][0]["description"]
                if len(activity) >= 2 and len(prompts) >= 2:
                    break

            assert len(activity) >= 2, f"two workers should appear on the bus, saw {activity}"
            # Named by slot rather than redacted: the pool root is a server-owned
            # root the feed may *name* (services/activity.py). Without that, every
            # worker row would read "(outside the workspace)".
            summaries = [s for rows in activity.values() for s in rows]
            assert any(s.startswith("Read: slot-") for s in summaries), summaries
            assert len(prompts) >= 2, "each blocked worker must escalate to the board"
            assert all("scripted-permission" in text for text in prompts.values())

        # And the load path agrees with the live one, which is what a board
        # opened *after* the fleet blocked depends on.
        snapshot = client.get("/api/agents/permissions").json()
        assert {row["session_id"] for row in snapshot["sessions"]} == set(prompts)

        # Answered from the board, for a session this client has no chat socket
        # for — the whole point of the second door.
        target = snapshot["sessions"][0]
        answered = client.post(
            f"/api/agents/sessions/{target['session_id']}/permission",
            json={"request_id": target["pending"][0]["request_id"], "allow": False},
        )
        assert answered.status_code == 200
        # A stale click is refused rather than reported as a decision.
        again = client.post(
            f"/api/agents/sessions/{target['session_id']}/permission",
            json={"request_id": target["pending"][0]["request_id"], "allow": True},
        )
        assert again.status_code == 404

        # Stopping the orchestrator reaps the crew and gives the slots back.
        after = client.post(f"/api/orchestrator/sessions/{boss}/stop").json()
        assert after["orchestrators"][0]["orchestrator_id"] == boss
        assert all(w["outcome"] == "stopped" for w in after["orchestrators"][0]["workers"])
        pool = client.get("/api/worktrees").json()
        assert [s for s in pool["slots"] if s["state"] == "leased"] == []


def test_every_setting_name_a_refusal_quotes_is_real() -> None:
    """A cap with a way out is only a way out if the variable exists."""
    settings_fields = {f"WORKBENCH_{name.upper()}" for name in Settings.model_fields}
    for name in (
        SETTING_MAX_WORKERS,
        SETTING_MAX_SESSIONS,
        SETTING_POOL_SIZE,
        "WORKBENCH_ORCHESTRATOR_WORKER_TURNS",
        "WORKBENCH_ORCHESTRATOR_WORKER_COST_USD",
        "WORKBENCH_ORCHESTRATOR_FLEET_TURNS",
        "WORKBENCH_ORCHESTRATOR_FLEET_COST_USD",
    ):
        assert name in settings_fields, f"{name} is not a real setting"


def test_the_spawn_tool_warns_the_model_about_the_permission_policy() -> None:
    """Token-cheap, but it saves a round trip: an orchestrator that does not
    know its workers block on a human writes plans that assume they do not."""
    assert "approving" in SPAWN_WORKER.description
    assert "board" in SPAWN_WORKER.description
    assert "ceiling" in SPAWN_WORKER.description
    assert LIST_WORKERS.input_schema == {}
