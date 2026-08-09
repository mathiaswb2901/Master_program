"""Validation schemas: does an agent's result stand up to evidence.

Provenance (``models/provenance.py``) answers *who* changed a file. Validation
answers the next question — *and is what they wrote correct* — and it produces
one :class:`ValidationResult`, the atom the rest of M6 composes with.

Two properties are in the schema rather than in a comment, because each is a
promise a later edit could quietly break:

* **``risk`` is derived, never asserted.** It is the max severity over
  ``evidence`` through an explicit table (``services/validation.py``). A result
  with **no evidence** is ``blocked`` — a validation that could not judge — never
  a silent green. The caller never sends a ``risk``; the service computes it.
* **Payloads are referenced, not inlined.** An :class:`EvidenceItem` names its
  detail payload by ``payload_ref`` into a bounded per-kind store, so a big
  reconciliation table never rides the result (or the ``/ws/events`` frame).

Held in memory only, bounded by an LRU keyed by ``validation_id`` (the same
posture as provenance's 500-path bound): a server restart forgets every result,
and a reconnecting client re-reads ``GET /api/validation``.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

#: The badge value. Derived from the evidence, ordered least-to-most severe.
#: ``blocked`` is the "could not judge" state — distinct from a red ``high``
#: *fail* — and sorts as the most severe (a validation that could not run at all
#: is worse than one that ran and failed a check).
RiskLevel = Literal["pass", "low", "medium", "high", "blocked"]

#: One check's verdict on one thing. ``skipped`` is "did not apply / could not
#: evaluate this item", distinct from ``fail`` (evaluated, disagreed).
#:
#: ``blocked`` is the fourth, and it is a *refusal to judge that still speaks*.
#: Before it existed the only way a result could reach a ``blocked`` risk was by
#: producing **no evidence at all** — which threw away the one sentence that
#: makes a refusal useful (what is wrong, and what to do about it). A check that
#: can see it would have to guess now says so on the record: the reconciliation
#: gate uses it when a docked workbook has unsaved changes it cannot read
#: (``services/reconciliation.py``), because reconciling the stale file on disk
#: would be a green badge about numbers nobody read. It is the most severe
#: outcome — worse than ``fail`` — for the reason ``RiskLevel.blocked`` already
#: sorts highest: a gate that could not run is worse than one that ran and
#: disagreed.
CheckOutcome = Literal["pass", "warn", "fail", "skipped", "blocked"]

#: The kind of evidence an item carries, which is also the key its detail
#: payload is stored under (the per-kind payload store).
EvidenceKind = Literal["numeric", "gate", "diff", "log", "artifact"]


class EvidenceTruncation(BaseModel):
    """AXI shape 1: a capped window says how much was cut and how to widen it.

    Attached whenever a bounded list (evidence here, comparisons in a
    reconciliation report) dropped rows, so a reader never mistakes a cap for
    "that was everything".
    """

    #: How many rows the payload actually carries.
    shown: int
    #: How many there were before the cap.
    total: int
    #: One line naming what was cut and the way to get the rest.
    detail: str


class EvidenceItem(BaseModel):
    """One line of evidence: a check's verdict on one thing, plus where its
    detail payload lives."""

    kind: EvidenceKind
    #: What was checked, e.g. "workbook↔code reconciliation", "ruff", "pytest".
    label: str
    outcome: CheckOutcome
    #: One line a human reads — the headline, not the payload.
    detail: str
    #: Id into the bounded per-kind payload store, or None when there is no
    #: payload behind this line. Never the payload itself (see module docstring).
    payload_ref: str | None = None


class ValidationSubject(BaseModel):
    """What was validated. The same two anchors provenance already established
    (a session's output, a file) plus an objective, so the three compose without
    a third mechanism."""

    kind: Literal["session_output", "file", "objective"]
    #: A session_id, a workspace-relative path, or an objective_id.
    ref: str
    label: str


class ValidationApproval(BaseModel):
    """The one mandatory human decision on a ``medium``-or-worse result. Recorded
    on the result by ``POST /api/validation/{id}/approve``; the timestamp is
    server-minted, never taken from the caller."""

    approver: str
    timestamp: datetime
    note: str | None = None


class ValidationResult(BaseModel):
    """One validation's whole answer — the atom the milestone composes with."""

    #: Server-minted, the stable handle. Saved cards and the Review panel key on
    #: it, and it is what ``approve`` and ``GET /{id}`` address.
    validation_id: str
    subject: ValidationSubject
    #: The badge value — the max severity across ``evidence``. Derived, never
    #: asserted by the caller (``services/validation.py``).
    risk: RiskLevel
    evidence: list[EvidenceItem] = Field(default_factory=list)
    #: One sentence for the badge tooltip and the card. For a ``blocked`` result
    #: this is where the *why* lives ("no checks produced evidence").
    summary: str
    created_at: datetime
    #: None while the validation is still running; stamped when it finishes.
    completed_at: datetime | None = None
    #: Set when ``evidence`` was capped (AXI shape 1). None means the list is
    #: whole.
    truncated: EvidenceTruncation | None = None
    #: The human decision, once made. None means "awaiting approval" for a
    #: ``medium``-or-worse result, and "not required" otherwise.
    approval: ValidationApproval | None = None


class ValidationSpec(BaseModel):
    """``POST /api/validation/run`` — what to validate and with which checks.

    The caller names the subject and, optionally, a subset of registered check
    ids (empty = every registered check). ``params`` carries per-check input as
    data — never code (a shell in a JSON body is exactly what the never-execute
    doctrine exists to stop).
    """

    subject: ValidationSubject
    #: Registered check ids to run; empty runs every registered check. An id
    #: that is not registered becomes a ``gate``/``fail`` evidence line rather
    #: than a silent no-op.
    checks: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class ApproveRequest(BaseModel):
    """``POST /api/validation/{id}/approve`` — the human decision. The server
    stamps the timestamp; a stale/superseded id is 404, never a 200 misread as a
    decision."""

    approver: str
    note: str | None = None


class ValidationResults(BaseModel):
    """``GET /api/validation`` — every result currently held, for initial load
    and reconnect (the usage/activity precedent). Oldest first, newest last.

    In-memory only and bounded by the LRU: a restart returns an empty list, and
    a result the LRU evicted is gone from here too."""

    results: list[ValidationResult] = Field(default_factory=list)


class ValidationEvent(BaseModel):
    """Broadcast on ``/ws/events`` when a result is created or approved.

    Published on the shared ``EventBus`` exactly as ``FileProvenanceEvent`` and
    ``SessionActivityEvent`` are, so a window that never issued the run still
    tracks it. Carries the whole result, not a delta — the client holds its own
    map keyed by ``validation_id`` and replaces the entry."""

    type: Literal["validation"] = "validation"
    result: ValidationResult
