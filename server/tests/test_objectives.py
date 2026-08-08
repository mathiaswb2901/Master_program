"""Objective sessions (plan §3): a goal bound to a named session, closed by
evidence rather than opinion.

Two halves, mirroring the rest of M6:

* the **store** gains set/get/clear on an objective without restructuring #70 —
  the manifest round-trips it, and a whole-manifest ``PUT`` never wipes it;
* the **status is derived**, never asserted — a pure join over the validation
  results, with ``met`` gated on a human approval, so a stale "done" cannot
  outlive (or precede) the evidence.

The plural obligation is asserted the way every plural thing here is: two
sessions carry two independent objectives, and one's evidence never moves the
other's badge.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.sessions import (
    CreateNamedSessionRequest,
    Objective,
    UpdateNamedSessionRequest,
)
from workbench_server.models.validation import (
    EvidenceItem,
    RiskLevel,
    ValidationApproval,
    ValidationResult,
    ValidationSubject,
)
from workbench_server.services.sessions import (
    SessionsStore,
    derive_objective_status,
    objective_view,
)
from workbench_server.services.validation import ValidationContext, ValidationService


def _create(name: str, workspace: str) -> CreateNamedSessionRequest:
    return CreateNamedSessionRequest(name=name, workspace=workspace)


_BASE = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _result(
    ref: str,
    risk: RiskLevel,
    *,
    vid: str,
    approved: bool = False,
    kind: str = "objective",
    minutes: int = 0,
) -> ValidationResult:
    """A ValidationResult for an objective (or another subject kind), at a chosen
    risk and approval, timestamped so "latest" is deterministic."""
    subject = ValidationSubject(kind=kind, ref=ref, label=ref)
    created = _BASE + timedelta(minutes=minutes)
    approval = (
        ValidationApproval(approver="you", timestamp=created, note=None) if approved else None
    )
    return ValidationResult(
        validation_id=vid,
        subject=subject,
        risk=risk,
        evidence=[EvidenceItem(kind="gate", label="x", outcome="pass", detail="d")],
        summary="s",
        created_at=created,
        completed_at=created,
        approval=approval,
    )


# ---- the store: set/get/clear, additive to #70 ------------------------------


def test_set_get_clear_objective_round_trips(tmp_path: Path) -> None:
    store = SessionsStore(tmp_path / "appdata")
    ws = str(tmp_path / "project")
    session = store.create(_create("bid run", ws))
    assert session.objective is None  # default: no goal

    updated = store.set_objective(
        session.id, Objective(statement="Reconcile Åsen 2 revenue", acceptance="within 0.1%")
    )
    assert updated.objective is not None
    assert updated.objective.statement == "Reconcile Åsen 2 revenue"
    assert updated.objective.acceptance == "within 0.1%"

    # Survives a reload from disk (a fresh store over the same file).
    reloaded = SessionsStore(tmp_path / "appdata").get(session.id)
    assert reloaded.objective is not None
    assert reloaded.objective.statement == "Reconcile Åsen 2 revenue"

    cleared = store.clear_objective(session.id)
    assert cleared.objective is None


def test_whole_manifest_update_preserves_the_objective(tmp_path: Path) -> None:
    """A rename/re-arrange goes through the whole-manifest ``PUT``, which does not
    carry the objective — so the store must keep it, or a rename would silently
    drop the goal."""
    store = SessionsStore(tmp_path / "appdata")
    ws = str(tmp_path / "project")
    session = store.create(_create("bid run", ws))
    store.set_objective(session.id, Objective(statement="ship it"))

    renamed = store.update(session.id, UpdateNamedSessionRequest(name="renamed", workspace=ws))
    assert renamed.name == "renamed"
    assert renamed.objective is not None
    assert renamed.objective.statement == "ship it"


def test_two_sessions_carry_independent_objectives(tmp_path: Path) -> None:
    store = SessionsStore(tmp_path / "appdata")
    ws = str(tmp_path / "project")
    a = store.create(_create("A", ws))
    b = store.create(_create("B", ws))

    store.set_objective(a.id, Objective(statement="goal A"))
    store.set_objective(b.id, Objective(statement="goal B"))

    got_a = store.get(a.id)
    got_b = store.get(b.id)
    assert got_a.objective is not None and got_a.objective.statement == "goal A"
    assert got_b.objective is not None and got_b.objective.statement == "goal B"

    store.clear_objective(a.id)
    assert store.get(a.id).objective is None
    # B is untouched by A's clear.
    assert store.get(b.id).objective is not None


# ---- the derivation: status is evidence, gated on a human --------------------


def _objective() -> Objective:
    return Objective(statement="reconcile")


def test_open_with_no_objective_or_no_result() -> None:
    assert derive_objective_status(None, None) == "open"
    assert derive_objective_status(_objective(), None) == "open"


def test_met_only_with_an_approved_result() -> None:
    passing = _result("sess", "pass", vid="val_1", approved=True)
    assert derive_objective_status(_objective(), passing) == "met"
    # A clean pass that nobody signed off is *not* met — approval is the gate.
    unsigned = _result("sess", "pass", vid="val_1", approved=False)
    assert derive_objective_status(_objective(), unsigned) == "open"


def test_at_risk_and_failing_come_from_the_unapproved_latest() -> None:
    assert derive_objective_status(_objective(), _result("s", "medium", vid="v")) == "at-risk"
    assert derive_objective_status(_objective(), _result("s", "high", vid="v")) == "failing"
    assert derive_objective_status(_objective(), _result("s", "blocked", vid="v")) == "failing"
    # A human can sign off a risky result — that override closes it (plan §3).
    approved_high = _result("s", "high", vid="v", approved=True)
    assert derive_objective_status(_objective(), approved_high) == "met"


def test_the_view_takes_the_latest_matching_result_by_kind_and_ref(tmp_path: Path) -> None:
    store = SessionsStore(tmp_path / "appdata")
    session = store.create(_create("A", str(tmp_path)))
    store.set_objective(session.id, _objective())

    results = [
        # An older approved result…
        _result(session.id, "pass", vid="val_old", approved=True, minutes=0),
        # …superseded by a newer *unapproved* medium one — the latest wins, so the
        # objective is at-risk, not falsely still met (the superseded case).
        _result(session.id, "medium", vid="val_new", approved=False, minutes=5),
        # A same-ref result of a different kind must not be mistaken for the
        # objective's evidence.
        _result(session.id, "high", vid="val_out", kind="session_output", minutes=9),
    ]
    view = objective_view(store.get(session.id), results)
    assert view.status == "at-risk"
    assert view.evidence is not None and view.evidence.validation_id == "val_new"


def test_an_evicted_result_does_not_keep_met(tmp_path: Path) -> None:
    """The approved result is gone from the held list (LRU eviction / restart):
    the objective falls back to ``open``, never a stale ``met``."""
    store = SessionsStore(tmp_path / "appdata")
    session = store.create(_create("A", str(tmp_path)))
    store.set_objective(session.id, _objective())
    # Was met while the approved result was held; now the list holds none for it.
    view = objective_view(store.get(session.id), [])
    assert view.status == "open"
    assert view.evidence is None


# ---- the endpoints, through the production wiring ----------------------------


def _app(tmp_path: Path) -> Any:
    return create_app(Settings(workspace_root=tmp_path, fake_agent=True))


def test_objective_endpoints_404_on_unknown_session(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/api/sessions/nope/objective").status_code == 404
        assert (
            client.put("/api/sessions/nope/objective", json={"statement": "x"}).status_code == 404
        )
        assert client.delete("/api/sessions/nope/objective").status_code == 404


def test_set_run_approve_flips_the_objective_to_met(tmp_path: Path) -> None:
    """The whole journey through the real wiring: bind a goal, validate it,
    approve the result, and the objective's derived status flips to ``met`` — and
    a second session's objective is untouched (plural)."""
    app = _app(tmp_path)
    service: ValidationService = app.state.validation

    class PassCheck:
        id = "goalcheck"

        async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
            return [EvidenceItem(kind="gate", label="goal", outcome="pass", detail="ok")]

    service.register(PassCheck())
    ws = str(tmp_path)

    with TestClient(app) as client:
        first = client.post("/api/sessions", json={"name": "worker", "workspace": ws}).json()
        second = client.post("/api/sessions", json={"name": "other", "workspace": ws}).json()

        # Bind the goal. Status is open — no evidence yet.
        view = client.put(
            f"/api/sessions/{first['id']}/objective",
            json={"statement": "reconcile Åsen 2 revenue"},
        ).json()
        assert view["status"] == "open"
        assert view["objective"]["statement"] == "reconcile Åsen 2 revenue"

        client.put(
            f"/api/sessions/{second['id']}/objective", json={"statement": "a different goal"}
        )

        # Validate the objective. The subject is this objective (kind + session id).
        run = client.post(
            "/api/validation/run",
            json={
                "subject": {
                    "kind": "objective",
                    "ref": first["id"],
                    "label": "reconcile Åsen 2 revenue",
                },
                "checks": ["goalcheck"],
            },
        ).json()
        vid = run["validation_id"]

        # A passing result nobody signed off is not yet met — the human gate.
        assert client.get(f"/api/sessions/{first['id']}/objective").json()["status"] == "open"

        # Approve it — the one mandatory human decision — and it flips to met.
        approved = client.post(f"/api/validation/{vid}/approve", json={"approver": "you"})
        assert approved.status_code == 200
        met = client.get(f"/api/sessions/{first['id']}/objective").json()
        assert met["status"] == "met"
        assert met["evidence"]["validation_id"] == vid

        # The second session's objective never moved — its status is still open.
        other = client.get(f"/api/sessions/{second['id']}/objective").json()
        assert other["status"] == "open"
        assert other["objective"]["statement"] == "a different goal"

        # Clearing drops the goal and the status returns to open.
        cleared = client.delete(f"/api/sessions/{first['id']}/objective").json()
        assert cleared["objective"] is None
        assert cleared["status"] == "open"
