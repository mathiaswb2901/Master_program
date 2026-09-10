"""The disk source: what survives a restart, and what is refused rather than guessed.

Every test here is about one of the four promises ``services/validation_store.py``
makes in its docstring:

* a written result **reads back identical** through the model round trip, and a
  restart is exactly that round trip;
* a **corrupt or half-written trailing line costs one line**, never the file;
* a payload over the byte budget is written **truncated with its
  ``EvidenceTruncation``**, never dropped;
* retention **deletes by age** and never touches an approved result inside the
  window — and a workspace switch loads the other project's evidence, not this
  one's.

The last case is the one the frame got wrong before this PR: ``set_workspace_root``
cleared and stopped, which read as "the other project has no evidence" when what
was true is "nobody looked".
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.gates import GateLog
from workbench_server.models.reconciliation import CellComparison, ReadSource, ReconciliationReport
from workbench_server.models.validation import (
    EvidenceItem,
    EvidenceTruncation,
    ValidationApproval,
    ValidationResult,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.models.validation_store import (
    MAX_PAYLOAD_BYTES,
    RetentionPolicy,
    StoredValidation,
)
from workbench_server.services import validation_store
from workbench_server.services.event_bus import EventBus
from workbench_server.services.validation import (
    MAX_PAYLOADS_PER_KIND,
    ValidationContext,
    ValidationService,
)
from workbench_server.services.validation_store import (
    VALIDATION_DIR,
    ValidationStore,
    fit_payload,
    payload_envelope,
)

NOW = datetime(2026, 8, 9, 14, 2, 11, tzinfo=UTC)


def _subject(ref: str = "book.xlsx") -> ValidationSubject:
    return ValidationSubject(kind="file", ref=ref, label=ref)


def _result(validation_id: str = "val_0001", *, created_at: datetime = NOW) -> ValidationResult:
    return ValidationResult(
        validation_id=validation_id,
        subject=_subject(),
        risk="medium",
        evidence=[
            EvidenceItem(kind="numeric", label="recon", outcome="warn", detail="off by 2"),
        ],
        summary="medium: 1 checks (1 warn).",
        created_at=created_at,
        completed_at=created_at,
    )


def _store(tmp_path: Path) -> ValidationStore:
    return ValidationStore(tmp_path)


def _results_file(tmp_path: Path, stamp: datetime = NOW) -> Path:
    return tmp_path / VALIDATION_DIR / f"results-{stamp.year:04d}-{stamp.month:02d}.jsonl"


# ---- the round trip ----------------------------------------------------------


def test_a_written_result_reads_back_identical(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = _result()
    store.append_result(result, [], now=NOW)

    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)

    assert [r.validation_id for r in found.results] == ["val_0001"]
    # Byte-identical through the model, which is the claim a restart depends on.
    assert found.results[0].model_dump_json() == result.model_dump_json()
    assert found.skipped == 0


def test_an_approval_is_its_own_line_and_lands_on_the_result(tmp_path: Path) -> None:
    """Append-only in the shape that matters: the result's line is never rewritten."""
    store = _store(tmp_path)
    store.append_result(_result(), [], now=NOW)
    before = _results_file(tmp_path).read_bytes()

    store.append_approval(
        "val_0001",
        ValidationApproval(approver="mathi", timestamp=NOW, note="checked by hand"),
        now=NOW,
    )

    assert _results_file(tmp_path).read_bytes() == before  # untouched
    loaded = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND).results[0]
    assert loaded.approval is not None
    assert loaded.approval.approver == "mathi"
    assert loaded.approval.note == "checked by hand"


def test_the_last_approval_line_wins(tmp_path: Path) -> None:
    """A re-approval is an append, not an edit — so the *newest* line decides."""
    store = _store(tmp_path)
    store.append_result(_result(), [], now=NOW)
    store.append_approval("val_0001", ValidationApproval(approver="first", timestamp=NOW), now=NOW)
    store.append_approval("val_0001", ValidationApproval(approver="second", timestamp=NOW), now=NOW)

    loaded = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND).results[0]
    assert loaded.approval is not None
    assert loaded.approval.approver == "second"


def test_the_newest_results_are_the_ones_kept_when_the_limit_bites(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        store.append_result(
            _result(f"val_{index:04d}", created_at=NOW + timedelta(minutes=index)), [], now=NOW
        )

    found = store.load(3, payload_cap=MAX_PAYLOADS_PER_KIND)

    # Oldest-first, and the *newest* three: an LRU that replayed the oldest three
    # would come back full of evidence nobody is looking at.
    assert [r.validation_id for r in found.results] == ["val_0002", "val_0003", "val_0004"]


def test_older_month_files_fill_what_the_newest_leaves(tmp_path: Path) -> None:
    store = _store(tmp_path)
    july = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    store.append_result(_result("val_july", created_at=july), [], now=july)
    store.append_result(_result("val_aug", created_at=NOW), [], now=NOW)

    assert (tmp_path / VALIDATION_DIR / "results-2026-07.jsonl").exists()
    assert (tmp_path / VALIDATION_DIR / "results-2026-08.jsonl").exists()
    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)
    assert [r.validation_id for r in found.results] == ["val_july", "val_aug"]


# ---- a bad line costs a line -------------------------------------------------


def test_a_truncated_trailing_line_is_skipped_and_the_rest_survives(tmp_path: Path) -> None:
    """The shape a power cut leaves: half a JSON object and no newline."""
    store = _store(tmp_path)
    store.append_result(_result("val_0001"), [], now=NOW)
    store.append_result(_result("val_0002"), [], now=NOW)
    path = _results_file(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"version": 1, "written_at": "2026-08-09T14:0')

    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)

    assert [r.validation_id for r in found.results] == ["val_0001", "val_0002"]
    assert found.skipped == 1


def test_a_line_from_a_version_this_build_cannot_read_is_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_result(_result("val_0001"), [], now=NOW)
    line = json.loads(_results_file(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    line["version"] = 99
    line["result"]["validation_id"] = "val_future"
    with _results_file(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")

    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)

    assert [r.validation_id for r in found.results] == ["val_0001"]
    assert found.skipped == 1


def test_a_missing_directory_loads_as_nothing_rather_than_raising(tmp_path: Path) -> None:
    found = _store(tmp_path / "never-used").load(500, payload_cap=MAX_PAYLOADS_PER_KIND)
    assert found.results == []
    assert found.payloads == {}


# ---- the payload budget ------------------------------------------------------


def _big_report(rows: int) -> ReconciliationReport:
    return ReconciliationReport(
        workbook="models/se3-dispatch.xlsx",
        # Every report says where its numbers came from (PR-A). A disk read is
        # what these fixtures are: no Excel is involved anywhere in this suite.
        source=ReadSource(kind="file", read_at=NOW.replace(tzinfo=None), cached_values=True),
        matched=rows,
        mismatched=0,
        total=rows,
        comparisons=[
            CellComparison(
                cell=f"Hours!B{index + 2}",
                label=f"hour {index}",
                expected=float(index),
                actual=float(index),
                unit="MWh",
                delta=0.0,
            )
            for index in range(rows)
        ],
    )


def test_a_payload_over_the_budget_is_written_truncated_not_dropped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = _big_report(8_760)  # a year of hours: comfortably over the budget
    assert len(report.model_dump_json().encode("utf-8")) > MAX_PAYLOAD_BYTES

    store.append_result(_result(), [("numeric", "numeric_abc123", report)], now=NOW)

    written = tmp_path / VALIDATION_DIR / "payloads" / "numeric" / "numeric_abc123.json"
    assert written.exists()
    assert written.stat().st_size <= MAX_PAYLOAD_BYTES + 512  # + the envelope's own keys

    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)
    stored = found.payloads[("numeric", "numeric_abc123")]
    assert isinstance(stored, ReconciliationReport)
    # Short, and **saying so**: a cut that does not announce itself is the one
    # thing a proof may not do (AXI shape 1).
    assert 0 < len(stored.comparisons) < 8_760
    assert stored.truncated is not None
    assert stored.truncated.total == 8_760
    assert stored.truncated.shown == len(stored.comparisons)
    assert "budget" in stored.truncated.detail
    # The rest of the report is intact — the counts are what a reader checks first.
    assert stored.total == 8_760
    assert stored.workbook == "models/se3-dispatch.xlsx"


def test_a_payload_inside_the_budget_is_written_whole(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = _big_report(3)
    store.append_result(_result(), [("numeric", "numeric_small", report)], now=NOW)
    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)
    stored = found.payloads[("numeric", "numeric_small")]
    assert isinstance(stored, ReconciliationReport)
    assert len(stored.comparisons) == 3
    assert stored.truncated is None


def test_fit_payload_trims_a_long_string_when_there_is_no_list_to_cut() -> None:
    """A ``GateLog`` is one big string, not a list — the second half of the trimmer."""
    log = GateLog(gate="pytest", argv=["pytest"], exit_code=1, duration_ms=1, text="x" * 200_000)
    trimmed, truncation = fit_payload(log, budget=8_192)
    assert isinstance(trimmed, GateLog)
    assert truncation is not None
    assert len(trimmed.text) < 200_000
    assert len(trimmed.model_dump_json().encode("utf-8")) <= 8_192
    assert trimmed.truncated is not None


def test_a_ref_that_is_not_a_safe_filename_is_refused(tmp_path: Path) -> None:
    """A ``payload_ref`` becomes a filename, so it is checked rather than sanitised."""
    store = _store(tmp_path)
    store.append_result(_result(), [("numeric", "../../escape", _big_report(1))], now=NOW)
    assert not (tmp_path.parent / "escape.json").exists()
    line = StoredValidation.model_validate_json(
        _results_file(tmp_path).read_text(encoding="utf-8").splitlines()[0]
    )
    assert line.payloads == []  # named as unwritten rather than claimed as written


def test_an_unknown_payload_shape_is_named_rather_than_written(tmp_path: Path) -> None:
    class Mystery(BaseModel):
        value: int

    assert payload_envelope("artifact", "artifact_x", Mystery(value=1)) is None
    store = _store(tmp_path)
    store.append_result(_result(), [("artifact", "artifact_x", Mystery(value=1))], now=NOW)
    line = StoredValidation.model_validate_json(
        _results_file(tmp_path).read_text(encoding="utf-8").splitlines()[0]
    )
    assert line.payloads == []


def test_the_payload_cap_keeps_the_newest_results_detail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(4):
        store.append_result(
            _result(f"val_{index:04d}", created_at=NOW + timedelta(minutes=index)),
            [("numeric", f"numeric_{index}", _big_report(1))],
            now=NOW,
        )

    found = store.load(500, payload_cap=2)

    assert set(found.payloads) == {("numeric", "numeric_3"), ("numeric", "numeric_2")}


# ---- the write discipline ----------------------------------------------------
#
# `os.replace` onto a path another process has transiently open raises
# PermissionError on Windows instead of waiting, and this repo already knows that
# is common rather than theoretical (`services/layouts.py`, measured at ~50% for
# two writes to one path ~20 ms apart). Both writers here can repeat a target that
# fast — `exports/<validation_id>.md` is one fixed path per id — so the retry is
# what stands between a double-click and either a 500 or a silently dropped
# payload. These three pin it: transient loses, exhausted still raises, and the
# payload half survives a lock that the export half would report.


#: Captured before any test patches `os.replace`, so a flake that finally lets the
#: write through calls the real one rather than itself.
_REAL_REPLACE = os.replace


class _FlakyReplace:
    """``os.replace`` that refuses the first ``fails`` calls, as Windows does."""

    def __init__(self, fails: int) -> None:
        self.fails = fails
        self.calls = 0

    def __call__(self, src: object, dst: object) -> None:
        self.calls += 1
        if self.calls <= self.fails:
            raise PermissionError(13, "Access is denied")
        _REAL_REPLACE(str(src), str(dst))


def test_an_export_survives_a_transient_lock_on_its_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-exporting the same id twice in quick succession is the collision here.

    Without the retry this is the 500 a user gets for double-clicking "Export
    evidence…" — `write_export` is the one method in the store allowed to raise.
    """
    store = _store(tmp_path)
    flaky = _FlakyReplace(fails=3)
    monkeypatch.setattr(os, "replace", flaky)

    written = store.write_export("val_0001.md", "# proof\n")

    assert flaky.calls == 4  # three refusals absorbed, the fourth landed
    assert written.read_text(encoding="utf-8") == "# proof\n"


def test_a_lock_that_outlasts_the_budget_still_reaches_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry is a budget, not a hang: a real lock (open in an editor, a
    read-only directory) still raises, and the export path is the one that
    reports it to the user waiting on it."""
    store = _store(tmp_path)
    flaky = _FlakyReplace(fails=validation_store.REPLACE_ATTEMPTS)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(validation_store, "REPLACE_BACKOFF_S", 0.0)

    with pytest.raises(PermissionError):
        store.write_export("val_0001.md", "# proof\n")

    assert flaky.calls == validation_store.REPLACE_ATTEMPTS
    # …and the temp file it was writing through does not survive the failure.
    assert list((tmp_path / VALIDATION_DIR / "exports").glob("*.tmp")) == []


def test_a_payload_is_not_silently_dropped_by_a_transient_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_write_payload` swallows its failures — so without the retry the same
    self-resolving contention loses the detail payload for good, and the result
    replays pointing at nothing."""
    store = _store(tmp_path)
    monkeypatch.setattr(os, "replace", _FlakyReplace(fails=2))

    store.append_result(_result(), [("numeric", "numeric_a", _big_report(1))], now=NOW)

    found = store.load(10, payload_cap=MAX_PAYLOADS_PER_KIND)
    assert [r.validation_id for r in found.results] == ["val_0001"]
    assert ("numeric", "numeric_a") in found.payloads


# ---- retention ---------------------------------------------------------------


def test_retention_deletes_by_age_and_keeps_an_approved_result_inside_the_window(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    old = NOW - timedelta(days=400)
    store.append_result(_result("val_old", created_at=old), [], now=old)
    store.append_approval("val_old", ValidationApproval(approver="mathi", timestamp=old), now=old)
    store.append_result(_result("val_new"), [], now=NOW)
    store.append_approval("val_new", ValidationApproval(approver="mathi", timestamp=NOW), now=NOW)

    removed = store.prune(RetentionPolicy(days=90), now=NOW)

    assert removed == 1
    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)
    # The old month is gone; the approved result inside the window is untouched,
    # approval and all — the half that would be easy to sweep by accident.
    assert [r.validation_id for r in found.results] == ["val_new"]
    assert found.results[0].approval is not None


def test_retention_zero_keeps_everything(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ancient = NOW - timedelta(days=4_000)
    store.append_result(_result("val_ancient", created_at=ancient), [], now=ancient)

    assert store.prune(RetentionPolicy(days=0), now=NOW) == 0
    assert [r.validation_id for r in store.load(500, payload_cap=1).results] == ["val_ancient"]


def test_a_swept_month_takes_its_payload_files_with_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = NOW - timedelta(days=400)
    store.append_result(
        _result("val_old", created_at=old), [("numeric", "numeric_old", _big_report(1))], now=old
    )
    payload = tmp_path / VALIDATION_DIR / "payloads" / "numeric" / "numeric_old.json"
    assert payload.exists()

    store.prune(RetentionPolicy(days=90), now=NOW)

    assert not payload.exists()


def test_a_month_whose_results_file_will_not_delete_keeps_its_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-failed sweep leaves the month whole, never proof without its proof.

    The reproduction is the transient Windows lock this module already retries
    ``os.replace`` past — here on the ``unlink`` of a month's ``results-*.jsonl``,
    which is caught and logged rather than raised. Deleting the payload files
    first would leave that month's results on disk with their evidence gone, so
    the next boot replays them with ``payload_ref``\\ s that 404 — indistinguishable
    from ordinary LRU eviction, with nothing anywhere saying retention took them.
    """
    store = _store(tmp_path)
    old = NOW - timedelta(days=400)
    store.append_result(
        _result("val_old", created_at=old), [("numeric", "numeric_old", _big_report(1))], now=old
    )
    payload = tmp_path / VALIDATION_DIR / "payloads" / "numeric" / "numeric_old.json"
    results = _results_file(tmp_path, old)
    assert payload.exists() and results.exists()

    real_unlink = Path.unlink

    def refuse_the_results_file(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == results.name:
            raise PermissionError(13, "Access is denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse_the_results_file)

    assert store.prune(RetentionPolicy(days=90), now=NOW) == 0

    monkeypatch.undo()
    # Both halves are still there, so the result still resolves to its evidence
    # and the next sweep can finish the job.
    assert results.exists()
    assert payload.exists()
    found = store.load(500, payload_cap=MAX_PAYLOADS_PER_KIND)
    assert [r.validation_id for r in found.results] == ["val_old"]
    assert ("numeric", "numeric_old") in found.payloads


def test_a_month_is_only_swept_once_every_line_in_it_is_past_the_window(
    tmp_path: Path,
) -> None:
    """The stated asymmetry: the window is a floor, never a ceiling.

    A result 95 days old sits in a month whose *last* instant is inside a 90-day
    window, so the file stays. Keeping proof a few days too long is the safe
    direction of a trade an append-only file cannot make any other way.
    """
    store = _store(tmp_path)
    store.append_result(_result("val_edge", created_at=NOW - timedelta(days=95)), [], now=NOW)

    assert store.prune(RetentionPolicy(days=90), now=NOW) == 0
    assert [r.validation_id for r in store.load(500, payload_cap=1).results] == ["val_edge"]


# ---- the service: replay on start, re-read on switch -------------------------


class StoringCheck:
    """A check that stashes a payload, exactly as the reconciliation gate does."""

    id = "storing"

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        ref = ctx.store_payload("numeric", _big_report(2))
        return [
            EvidenceItem(
                kind="numeric", label="recon", outcome="warn", detail="off by 2", payload_ref=ref
            )
        ]


@pytest.mark.asyncio
async def test_a_restart_replays_the_result_its_payload_and_its_approval(tmp_path: Path) -> None:
    """The whole feature, at the service seam: two services over one folder."""
    first = ValidationService(tmp_path, EventBus(), clock=lambda: NOW)
    first.register(StoringCheck())
    result = await first.run(ValidationSpec(subject=_subject(), checks=["storing"]))
    first.approve(result.validation_id, "mathi", note="checked the four flagged hours")
    ref = result.evidence[0].payload_ref
    assert ref is not None

    # A second process, same workspace. Nothing is shared but the folder.
    second = ValidationService(tmp_path, EventBus(), clock=lambda: NOW)
    assert second.snapshot().results == []  # nothing until start(), like the real boot
    second.start()

    replayed = second.get(result.validation_id)
    assert replayed is not None
    assert replayed.risk == result.risk
    assert [e.detail for e in replayed.evidence] == [e.detail for e in result.evidence]
    assert replayed.approval is not None
    assert replayed.approval.approver == "mathi"
    # …and the payload behind the evidence's ref is redeemable again, which is
    # what stops a restored result's expander being a dead handle.
    assert isinstance(second.payload("numeric", ref), ReconciliationReport)


@pytest.mark.asyncio
async def test_a_workspace_switch_loads_the_other_projects_evidence_not_this_ones(
    tmp_path: Path,
) -> None:
    """The behaviour the clear-only implementation was standing in for."""
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()

    service = ValidationService(here, EventBus(), clock=lambda: NOW)
    service.register(StoringCheck())
    mine = await service.run(ValidationSpec(subject=_subject("here.xlsx"), checks=["storing"]))

    # Somebody else's project, with its own evidence already on disk.
    ValidationStore(there).append_result(_result("val_theirs"), [], now=NOW)

    service.set_workspace_root(there)

    held = [r.validation_id for r in service.snapshot().results]
    assert held == ["val_theirs"]
    assert mine.validation_id not in held
    # …and switching back finds this project's own again.
    service.set_workspace_root(here)
    assert [r.validation_id for r in service.snapshot().results] == [mine.validation_id]


@pytest.mark.asyncio
async def test_a_run_never_fails_because_the_record_could_not_be_written(
    tmp_path: Path,
) -> None:
    """A read-only workspace costs the record, never the verdict."""
    service = ValidationService(tmp_path / "does" / "not" / "exist", EventBus(), clock=lambda: NOW)
    service.register(StoringCheck())
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    service.set_workspace_root(blocked)

    result = await service.run(ValidationSpec(subject=_subject(), checks=["storing"]))

    assert result.risk == "medium"  # the run stands
    assert service.get(result.validation_id) is not None


@pytest.mark.asyncio
async def test_retention_is_read_per_sweep_so_a_change_needs_no_restart(tmp_path: Path) -> None:
    days = 0
    service = ValidationService(
        tmp_path, EventBus(), clock=lambda: NOW, retention=lambda: RetentionPolicy(days=days)
    )
    old = NOW - timedelta(days=400)
    ValidationStore(tmp_path).append_result(_result("val_old", created_at=old), [], now=old)

    service.start()
    assert [r.validation_id for r in service.snapshot().results] == ["val_old"]

    days = 90  # the user changed the setting; nothing restarted
    service.set_workspace_root(tmp_path)
    assert service.snapshot().results == []


@pytest.mark.asyncio
async def test_a_retention_callable_that_raises_falls_back_to_the_default(tmp_path: Path) -> None:
    def broken() -> RetentionPolicy:
        raise RuntimeError("the settings document is on fire")

    service = ValidationService(tmp_path, EventBus(), clock=lambda: NOW, retention=broken)
    ValidationStore(tmp_path).append_result(_result(), [], now=NOW)
    service.start()  # must not raise
    assert [r.validation_id for r in service.snapshot().results] == ["val_0001"]


# ---- through the production wiring -------------------------------------------


def test_the_endpoints_serve_what_a_previous_process_wrote(tmp_path: Path) -> None:
    """``GET /api/validation`` stops being empty after a restart — and no client
    learned a new call to make that true."""
    ValidationStore(tmp_path).append_result(_result("val_written"), [], now=NOW)
    ValidationStore(tmp_path).append_approval(
        "val_written", ValidationApproval(approver="mathi", timestamp=NOW, note="fine"), now=NOW
    )

    app = create_app(Settings(workspace_root=tmp_path, fake_agent=True))
    with TestClient(app) as client:
        replay = client.get("/api/validation").json()["results"]
        assert [r["validation_id"] for r in replay] == ["val_written"]
        assert replay[0]["approval"]["approver"] == "mathi"
        # And the single-result handle resolves it too, so a restored pane has
        # something to be a view onto.
        assert client.get("/api/validation/val_written").status_code == 200


def test_a_run_through_the_api_lands_on_disk(tmp_path: Path) -> None:
    app = create_app(Settings(workspace_root=tmp_path, fake_agent=True))
    with TestClient(app) as client:
        body = {"subject": {"kind": "file", "ref": "book.xlsx", "label": "book.xlsx"}}
        posted = client.post("/api/validation/run", json=body).json()
        client.post(f"/api/validation/{posted['validation_id']}/approve", json={"approver": "you"})

    lines = _results_file(tmp_path, datetime.now(UTC)).read_text(encoding="utf-8").splitlines()
    assert [StoredValidation.model_validate_json(line).result.validation_id for line in lines] == [
        posted["validation_id"]
    ]
    approvals = (tmp_path / VALIDATION_DIR / "approvals.jsonl").read_text(encoding="utf-8")
    assert posted["validation_id"] in approvals


def test_an_evidence_truncation_survives_the_round_trip() -> None:
    """The stored shape is the wire shape: nothing about a cut is lost in writing."""
    truncation = EvidenceTruncation(shown=1, total=9, detail="8 rows withheld")
    report = _big_report(1).model_copy(update={"truncated": truncation})
    envelope = payload_envelope("numeric", "numeric_x", report)
    assert envelope is not None
    again = type(envelope).model_validate_json(envelope.model_dump_json())
    assert again.reconciliation is not None
    assert again.reconciliation.truncated == truncation
