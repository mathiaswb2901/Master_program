"""The validation frame: the risk table, the check registry, the LRU, the bus.

This PR ships no real check (the reconciliation gate is a later PR), so the
service is driven end-to-end by a fake check that returns canned evidence — the
same fake-first posture the Office host proves its lifecycle with.

What is under test is the frame's honesty:

* risk is **derived** from the evidence, and no evidence is **blocked** (never a
  silent green);
* a result is stored, retrievable, and published on the shared bus for a
  reconnecting client;
* the LRU is bounded and evicts the oldest;
* an approval is recorded with a server-minted timestamp, and a stale id is 404.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.validation import (
    CheckOutcome,
    EvidenceItem,
    RiskLevel,
    ValidationEvent,
    ValidationResult,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.validation import (
    MAX_PAYLOADS_PER_KIND,
    PayloadStore,
    ValidationContext,
    ValidationService,
    derive_risk,
)


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[BaseModel] = []

    def publish(self, event: BaseModel) -> None:
        self.published.append(event)
        super().publish(event)

    def validation_events(self) -> list[ValidationEvent]:
        return [e for e in self.published if isinstance(e, ValidationEvent)]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class CountingIds:
    """Deterministic, ordered ids so an LRU test can name what it evicts."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"val_{self.n:04d}"


class FakeCheck:
    """A check that returns exactly the evidence it was constructed with."""

    def __init__(self, check_id: str, evidence: list[EvidenceItem]) -> None:
        self.id = check_id
        self._evidence = evidence
        self.ran = 0

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        self.ran += 1
        return list(self._evidence)


class ExplodingCheck:
    id = "boom"

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        raise RuntimeError("kaboom")


def _subject() -> ValidationSubject:
    return ValidationSubject(kind="file", ref="book.xlsx", label="book.xlsx")


def _service(root: Path) -> tuple[ValidationService, RecordingBus, FakeClock, CountingIds]:
    bus = RecordingBus()
    clock = FakeClock()
    ids = CountingIds()
    return ValidationService(root, bus, clock=clock, id_factory=ids), bus, clock, ids


def _ev(outcome: CheckOutcome) -> EvidenceItem:
    return EvidenceItem(kind="gate", label="x", outcome=outcome, detail="d")


# ---- the risk-derivation table ----------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "risk"),
    [("pass", "pass"), ("skipped", "low"), ("warn", "medium"), ("fail", "high")],
)
def test_each_outcome_derives_its_risk(outcome: CheckOutcome, risk: RiskLevel) -> None:
    assert derive_risk([_ev(outcome)]) == risk


def test_no_evidence_is_blocked_not_a_silent_green() -> None:
    """The whole point: a validation that judged nothing is blocked, never pass."""
    assert derive_risk([]) == "blocked"


def test_risk_is_the_max_severity_over_the_evidence() -> None:
    assert derive_risk([_ev("pass"), _ev("warn"), _ev("fail"), _ev("pass")]) == "high"
    assert derive_risk([_ev("pass"), _ev("skipped"), _ev("pass")]) == "low"


# ---- a fake check drives run(), and the result is stored + retrievable -------


@pytest.mark.asyncio
async def test_a_registered_check_drives_run_and_the_result_is_stored(tmp_path: Path) -> None:
    service, bus, clock, _ = _service(tmp_path)
    check = FakeCheck("recon", [_ev("pass"), _ev("warn")])
    service.register(check)

    result = await service.run(ValidationSpec(subject=_subject()))

    assert check.ran == 1
    assert result.risk == "medium"  # the warn is the most severe
    assert len(result.evidence) == 2
    assert result.summary.startswith("medium:")
    assert result.created_at == clock.now
    assert result.completed_at == clock.now
    assert result.approval is None
    # Stored and retrievable by its own handle.
    assert service.get(result.validation_id) == result
    assert service.snapshot().results == [result]
    # Published once, carrying the whole result.
    assert [e.result.validation_id for e in bus.validation_events()] == [result.validation_id]


@pytest.mark.asyncio
async def test_a_run_with_no_registered_checks_is_blocked_with_a_reason(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path)
    result = await service.run(ValidationSpec(subject=_subject()))
    assert result.risk == "blocked"
    assert result.evidence == []
    assert "no checks" in result.summary.lower()


@pytest.mark.asyncio
async def test_an_unregistered_named_check_is_a_gate_fail_not_a_silent_skip(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _service(tmp_path)
    result = await service.run(ValidationSpec(subject=_subject(), checks=["ghost"]))
    assert result.risk == "high"
    assert [e.outcome for e in result.evidence] == ["fail"]
    assert "not registered" in result.evidence[0].detail


@pytest.mark.asyncio
async def test_a_check_that_raises_becomes_a_gate_fail(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path)
    service.register(ExplodingCheck())
    result = await service.run(ValidationSpec(subject=_subject(), checks=["boom"]))
    assert result.risk == "high"
    assert result.evidence[0].kind == "gate"
    assert "could not run" in result.evidence[0].detail


@pytest.mark.asyncio
async def test_only_the_named_checks_run(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path)
    a = FakeCheck("a", [_ev("pass")])
    b = FakeCheck("b", [_ev("fail")])
    service.register(a)
    service.register(b)
    result = await service.run(ValidationSpec(subject=_subject(), checks=["a"]))
    assert a.ran == 1
    assert b.ran == 0
    assert result.risk == "pass"


# ---- the LRU bound ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_result_lru_evicts_the_oldest(tmp_path: Path) -> None:
    bus = RecordingBus()
    ids = CountingIds()
    service = ValidationService(tmp_path, bus, clock=FakeClock(), id_factory=ids, max_results=3)
    service.register(FakeCheck("c", [_ev("pass")]))
    for _ in range(4):
        await service.run(ValidationSpec(subject=_subject()))
    held = [r.validation_id for r in service.snapshot().results]
    assert held == ["val_0002", "val_0003", "val_0004"]  # val_0001 evicted, oldest first
    assert service.get("val_0001") is None


# ---- the payload store bound ------------------------------------------------


def test_the_payload_store_is_bounded_per_kind_and_round_trips() -> None:
    store = PayloadStore(cap=2)
    refs = [store.put("numeric", _subject()) for _ in range(3)]
    # The oldest ref is gone; the last two survive and read back.
    assert store.get("numeric", refs[0]) is None
    assert store.get("numeric", refs[1]) is not None
    assert store.get("numeric", refs[2]) is not None
    # A different kind has its own budget.
    assert MAX_PAYLOADS_PER_KIND >= 1


# ---- re-rooting forgets results ---------------------------------------------


@pytest.mark.asyncio
async def test_switching_workspace_forgets_every_result(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path)
    service.register(FakeCheck("c", [_ev("pass")]))
    await service.run(ValidationSpec(subject=_subject()))
    assert service.snapshot().results != []
    service.set_workspace_root(tmp_path / "other")
    assert service.snapshot().results == []


# ---- approval ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_records_a_server_stamped_decision_and_republishes(tmp_path: Path) -> None:
    service, bus, clock, _ = _service(tmp_path)
    service.register(FakeCheck("c", [_ev("warn")]))
    result = await service.run(ValidationSpec(subject=_subject()))

    updated = service.approve(result.validation_id, "alice", note="looks fine")
    assert updated is not None
    assert updated.approval is not None
    assert updated.approval.approver == "alice"
    assert updated.approval.note == "looks fine"
    assert updated.approval.timestamp == clock.now  # server-minted, not caller-supplied
    # The stored copy carries the approval, and a second frame was published.
    assert service.get(result.validation_id) == updated
    assert len(bus.validation_events()) == 2


def test_approve_on_an_unknown_id_is_none_not_a_fabricated_result(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path)
    assert service.approve("val_does_not_exist", "alice") is None


# ---- the model round-trips --------------------------------------------------


def test_validation_result_round_trips_through_json() -> None:
    result = ValidationResult(
        validation_id="val_0001",
        subject=_subject(),
        risk="medium",
        evidence=[EvidenceItem(kind="numeric", label="recon", outcome="warn", detail="off by 2")],
        summary="medium: 1 checks (1 warn).",
        created_at=datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 8, 8, 12, 0, 1, tzinfo=UTC),
    )
    again = ValidationResult.model_validate_json(result.model_dump_json())
    assert again == result


# ---- the seam: run -> bus -> /ws/events -> GET /api/validation --------------


def _app(tmp_path: Path) -> Any:
    return create_app(Settings(workspace_root=tmp_path, fake_agent=True))


def test_the_endpoints_run_replay_and_approve(tmp_path: Path) -> None:
    """The whole pipe through the production wiring: a run publishes on
    ``/ws/events`` and shows up on ``GET /api/validation``; approve records the
    decision; a stale id is 404.

    A check is registered on the live service so ``/run`` produces evidence — the
    frame ships no real check, so the test supplies one exactly where a later PR
    plugs the reconciliation gate in.
    """
    app = _app(tmp_path)
    service: ValidationService = app.state.validation
    service.register(
        FakeCheck(
            "recon",
            [EvidenceItem(kind="numeric", label="recon", outcome="warn", detail="off by 2")],
        )
    )

    with TestClient(app) as client:
        assert client.get("/api/validation").json()["results"] == []  # nothing yet

        with client.websocket_connect("/ws/events") as events:
            body = {"subject": {"kind": "file", "ref": "book.xlsx", "label": "book.xlsx"}}
            posted = client.post("/api/validation/run", json=body).json()
            vid = posted["validation_id"]
            assert posted["risk"] == "medium"

            frame = json.loads(events.receive_text())
            assert frame["type"] == "validation"
            assert frame["result"]["validation_id"] == vid

        # Replayed for a client that just loaded.
        replay = client.get("/api/validation").json()["results"]
        assert [r["validation_id"] for r in replay] == [vid]

        # GET one by id.
        assert client.get(f"/api/validation/{vid}").json()["risk"] == "medium"
        assert client.get("/api/validation/nope").status_code == 404

        # Approve records the decision; the timestamp is server-minted.
        approved = client.post(f"/api/validation/{vid}/approve", json={"approver": "alice"}).json()
        assert approved["approval"]["approver"] == "alice"
        assert approved["approval"]["timestamp"] is not None

        # A stale/superseded id is 404, not a 200 misread as a decision.
        assert (
            client.post(
                "/api/validation/val_missing/approve", json={"approver": "alice"}
            ).status_code
            == 404
        )
