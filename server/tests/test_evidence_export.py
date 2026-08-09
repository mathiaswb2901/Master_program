"""The export: a result rendered as something you can hand to somebody.

What is under test is the *document*, not the plumbing — specifically the two
things a reader has to be able to trust about it:

* **it names everything the reader needs to act**: the subject, the workbook it
  was about, the risk, every check's evidence line, and who approved it and when;
* **it says "none" out loud** where PR-C cannot know the answer. A result that
  came from no reconciliation spec prints ``not run from a spec`` and an
  unapproved one prints ``not approved`` — because a section that goes *missing*
  reads as a hole in this build rather than as a fact about the run (AXI shape 2,
  applied to a human reader).

The no-spec case is asserted with the spec models absent, which they are: the
spec-from-code PR has not landed, and this test is what keeps the renderer honest
about landing first rather than quietly leaving a gap for later.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.reconciliation import CellComparison, ReconciliationReport
from workbench_server.models.validation import (
    EvidenceItem,
    EvidenceTruncation,
    ValidationApproval,
    ValidationResult,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.evidence_export import MAX_REPORT_EVIDENCE, render_evidence
from workbench_server.services.validation import PayloadStore, ValidationContext, ValidationService
from workbench_server.services.validation_store import ValidationStore

NOW = datetime(2026, 8, 9, 14, 2, 11, tzinfo=UTC)


def _result(**patch: object) -> ValidationResult:
    base = ValidationResult(
        validation_id="val_9f3c1d2e",
        subject=ValidationSubject(
            kind="file", ref="models/se3-dispatch.xlsx", label="SE3 dispatch reconciliation"
        ),
        risk="medium",
        evidence=[
            EvidenceItem(
                kind="numeric",
                label="workbook↔code reconciliation",
                outcome="pass",
                detail="all 8,760 cells within tolerance (rel 0.1%)",
            ),
            EvidenceItem(
                kind="gate", label="ruff check .", outcome="pass", detail="exit 0 in 3.2 s"
            ),
            EvidenceItem(
                kind="numeric",
                label="as-of causality",
                outcome="warn",
                detail="4 forecast cells reconcile against values dated after their as-of stamp",
            ),
        ],
        summary="medium: 3 checks (1 warn, 2 pass).",
        created_at=NOW,
        completed_at=NOW,
    )
    return base.model_copy(update=patch)


def _render(result: ValidationResult, root: Path, payloads: PayloadStore | None = None) -> str:
    return render_evidence(
        result,
        payloads=payloads or PayloadStore(),
        workspace_root=root,
        generated_at=NOW,
    )


# ---- what the report names ---------------------------------------------------


def test_the_report_names_the_subject_the_risk_and_every_evidence_line(tmp_path: Path) -> None:
    markdown = _render(_result(), tmp_path)

    assert "# Validation evidence — SE3 dispatch reconciliation" in markdown
    assert "val_9f3c1d2e" in markdown
    assert "**medium**" in markdown
    assert "3 evidence line(s): 1 warn, 2 pass" in markdown
    for label in ("workbook↔code reconciliation", "ruff check .", "as-of causality"):
        assert label in markdown
    # The outcome word leads each line, so the document is readable as text and
    # not only as a coloured table it will never be.
    assert "- **warn** · as-of causality" in markdown


def test_the_workbook_is_named_and_digested_from_the_reconciliation_payload(
    tmp_path: Path,
) -> None:
    """The payload names the workbook the check *opened*, which is the honest
    answer when it differs from the subject's label."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "se3-dispatch.xlsx").write_bytes(b"not really a workbook")
    payloads = PayloadStore()
    ref = payloads.put(
        "numeric",
        ReconciliationReport(
            workbook="models/se3-dispatch.xlsx",
            matched=1,
            mismatched=0,
            total=1,
            comparisons=[CellComparison(cell="D14", expected=1.0, actual=1.0)],
        ),
    )
    result = _result(
        evidence=[
            EvidenceItem(
                kind="numeric", label="recon", outcome="pass", detail="ok", payload_ref=ref
            )
        ]
    )

    markdown = _render(result, tmp_path, payloads)

    assert "**Workbook** `models/se3-dispatch.xlsx`" in markdown
    assert "sha256 " in markdown
    assert "21 bytes" in markdown
    # And it is stamped with *when* the digest was taken, so nobody reads it as
    # the bytes the check saw.
    assert "Read when this report was written" in markdown


def test_a_workbook_that_is_gone_says_so_rather_than_failing(tmp_path: Path) -> None:
    markdown = _render(_result(), tmp_path)
    assert "no longer on disk when this report was written" in markdown


def test_a_subject_outside_the_workspace_is_never_opened(tmp_path: Path) -> None:
    result = _result(
        subject=ValidationSubject(kind="file", ref="../../secrets.xlsx", label="elsewhere")
    )
    markdown = _render(result, tmp_path)
    assert "outside this workspace — not read" in markdown


def test_a_session_result_says_it_is_about_no_file(tmp_path: Path) -> None:
    result = _result(
        subject=ValidationSubject(kind="session_output", ref="sess-1", label="Worker output")
    )
    markdown = _render(result, tmp_path)
    assert "this result is about a session output, not a file" in markdown


# ---- the two sections that must say "none" -----------------------------------


def test_a_result_from_no_spec_says_so_rather_than_omitting_the_section(tmp_path: Path) -> None:
    """PR-C lands before the spec-from-code PR, and the report is honest about it."""
    markdown = _render(_result(), tmp_path)
    assert "**Spec**" in markdown
    assert "not run from a spec" in markdown
    assert "no approved spec digest" in markdown


def test_an_unapproved_result_says_it_is_unapproved(tmp_path: Path) -> None:
    markdown = _render(_result(), tmp_path)
    assert "**Approval** — not approved." in markdown
    # …and *why* it matters here: medium-or-worse still owes a human decision.
    assert "still awaiting a human decision" in markdown


def test_a_pass_result_says_no_approval_was_required(tmp_path: Path) -> None:
    markdown = _render(_result(risk="pass"), tmp_path)
    assert "None was required" in markdown


def test_an_approved_result_names_who_and_when_and_the_note(tmp_path: Path) -> None:
    approved = _result(
        approval=ValidationApproval(
            approver="mathi", timestamp=NOW, note="checked the four flagged hours by hand"
        )
    )
    markdown = _render(approved, tmp_path)
    assert "**Approval** mathi" in markdown
    assert "checked the four flagged hours by hand" in markdown
    assert "2026-08-09" in markdown


def test_the_source_line_is_present_and_says_it_is_not_recorded(tmp_path: Path) -> None:
    """The other half PR-C cannot answer: read-source provenance rides the live
    reader, and its absence is stated rather than left as a missing row."""
    markdown = _render(_result(), tmp_path)
    assert "**Source**" in markdown
    assert "not recorded" in markdown


# ---- bounds ------------------------------------------------------------------


def test_an_evidence_list_longer_than_the_page_says_how_much_it_cut(tmp_path: Path) -> None:
    many = [
        EvidenceItem(kind="gate", label=f"check {i}", outcome="pass", detail="ok")
        for i in range(MAX_REPORT_EVIDENCE + 7)
    ]
    markdown = _render(_result(evidence=many), tmp_path)
    assert f"and {7} more evidence line(s)" in markdown
    assert "Review panel" in markdown


def test_the_results_own_truncation_is_carried_into_the_report(tmp_path: Path) -> None:
    result = _result(
        truncated=EvidenceTruncation(shown=100, total=140, detail="showing 100 of 140 evidence")
    )
    assert "showing 100 of 140 evidence" in _render(result, tmp_path)


def test_a_blocked_result_with_no_evidence_still_renders(tmp_path: Path) -> None:
    blocked = _result(risk="blocked", evidence=[], summary="Blocked: no checks produced evidence.")
    markdown = _render(blocked, tmp_path)
    assert "no evidence — nothing was judged" in markdown
    assert "(none —" in markdown


# ---- the service seam and the route ------------------------------------------


class PassingCheck:
    id = "ok"

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        return [EvidenceItem(kind="gate", label="ok", outcome="pass", detail="fine")]


@pytest.mark.asyncio
async def test_export_writes_the_report_into_the_workspace(tmp_path: Path) -> None:
    service = ValidationService(tmp_path, EventBus(), clock=lambda: NOW)
    service.register(PassingCheck())
    result = await service.run(
        ValidationSpec(
            subject=ValidationSubject(kind="file", ref="a.xlsx", label="A"), checks=["ok"]
        )
    )

    export = service.export(result.validation_id)

    assert export is not None
    assert export.path == f".workbench/validation/exports/{result.validation_id}.md"
    written = tmp_path / ".workbench" / "validation" / "exports" / f"{result.validation_id}.md"
    assert written.read_text(encoding="utf-8") == export.markdown
    assert export.bytes == len(export.markdown.encode("utf-8"))


def test_export_of_an_unknown_id_is_none_not_an_empty_document(tmp_path: Path) -> None:
    service = ValidationService(tmp_path, EventBus(), clock=lambda: NOW)
    assert service.export("val_nope") is None


def test_an_id_that_is_not_a_safe_filename_is_refused(tmp_path: Path) -> None:
    """A replayed id came off a file on disk, so the export name is checked.

    Without this, a hand-edited ``results-*.jsonl`` naming ``../../evil`` would
    be a write outside the workspace through the one method here that may raise.
    """
    ValidationStore(tmp_path).append_result(_result(validation_id="../../evil"), [], now=NOW)
    service = ValidationService(tmp_path, EventBus(), clock=lambda: NOW)
    service.start()

    with pytest.raises(OSError, match="refusing to write an export"):
        service.export("../../evil")
    assert not (tmp_path.parent / "evil.md").exists()


def test_the_export_endpoint_writes_and_404s_a_stale_id(tmp_path: Path) -> None:
    app = create_app(Settings(workspace_root=tmp_path, fake_agent=True))
    with TestClient(app) as client:
        body = {"subject": {"kind": "file", "ref": "book.xlsx", "label": "Book"}}
        posted = client.post("/api/validation/run", json=body).json()
        vid = posted["validation_id"]
        client.post(f"/api/validation/{vid}/approve", json={"approver": "you", "note": "seen"})

        exported = client.post(f"/api/validation/{vid}/export").json()
        assert exported["validation_id"] == vid
        assert exported["filename"] == f"{vid}.md"
        assert "**Approval** you" in exported["markdown"]
        assert "seen" in exported["markdown"]
        assert (tmp_path / ".workbench" / "validation" / "exports" / f"{vid}.md").exists()

        assert client.post("/api/validation/val_missing/export").status_code == 404
