"""What a validation looks like once it is written down.

``models/validation.py`` is the *wire*: what a run answers and what a window
renders. This module is the **record** — the same result, stamped and on disk, so
a restart has something to come back to and a person has something to hand over.

Three properties are in the schema rather than in a comment, because each is a
promise a later edit could quietly break:

* **Every line is version-stamped** (:data:`STORE_VERSION`). A line written by a
  version this code cannot read is skipped with a warning and the rest of the
  file is kept — the ``services/layouts.py`` posture, sharpened by the fact that
  this file is append-only: losing one reading costs a reading, and guessing at
  it costs a wrong verdict about work somebody signed off on.
* **The record is append-only.** :class:`StoredValidation` and
  :class:`StoredApproval` are lines, never documents that get rewritten. The
  alternative is editing the file that *is* the record of what was approved.
* **A payload that does not fit is truncated, never dropped.** A detail payload
  is written beside the line under a byte budget, and one over that budget is
  written short with an :class:`~workbench_server.models.validation.EvidenceTruncation`
  saying so (AXI shape 1). Silence about a cut is the one thing a proof may not
  do.

Only :class:`EvidenceExport` is a REST body
(``POST /api/validation/{id}/export``). The stored shapes are the file format,
typed for exactly the same reason every wire body is: a document nothing
validates is a document nothing can be trusted to have written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from workbench_server.models.validation import (
    EvidenceKind,
    EvidenceTruncation,
    ValidationApproval,
    ValidationResult,
)

#: Bumped when a line written by an older version can no longer be read as this
#: one. A line stamped with anything else is skipped rather than guessed at.
STORE_VERSION = 1

#: Ceiling on one stored detail payload, in bytes. Sized from the payloads that
#: exist: a :class:`~workbench_server.models.gates.GateLog` is bounded at 8 KiB
#: by its own capture window, and a full reconciliation table of 8,760 hourly
#: comparisons serialises to roughly 1 MB — so the budget is what decides how
#: much of the *big* one survives the trip to disk. 64 KiB keeps ~350 comparison
#: rows, which is more than a person reads and far less than a year of hours.
MAX_PAYLOAD_BYTES = 64 * 1024

#: Ceiling on one JSONL line. A result carries at most ``MAX_EVIDENCE`` (100)
#: evidence items, each a headline and a ref — never a payload — so a line is
#: normally a few KB. A line over this is written anyway (dropping the record of
#: a run is worse than a fat file) and the ceiling exists to bound the *read*:
#: a line longer than this is refused rather than parsed, so a corrupt file
#: cannot make replay allocate without limit.
MAX_LINE_BYTES = 256 * 1024

#: Days of evidence kept by default. Ninety days covers a quarter — the window
#: an analyst is actually asked to justify — and ``0`` means keep forever.
DEFAULT_RETENTION_DAYS = 90


def as_utc(value: datetime) -> datetime:
    """A timestamp made comparable. Naive values are read as UTC.

    Every timestamp this store mints is aware (the service's clock is
    ``datetime.now(UTC)``), but a line hand-edited or written by an older build
    may not be — and a naive/aware comparison raises rather than sorting wrong,
    which would turn one odd line into a failed replay.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class StoredPayload(BaseModel):
    """One detail payload written beside a result, and what happened to it."""

    kind: EvidenceKind
    #: The ``EvidenceItem.payload_ref`` this file answers.
    ref: str
    #: Size of the written file, in bytes.
    bytes: int
    #: Set when the payload did not fit :data:`MAX_PAYLOAD_BYTES` and was written
    #: short. ``None`` means what is on disk is the whole payload.
    truncated: EvidenceTruncation | None = None


class StoredValidation(BaseModel):
    """One line of ``results-YYYY-MM.jsonl``: a result, as it was written."""

    version: int = STORE_VERSION
    #: When the line was appended — distinct from ``result.created_at``, which is
    #: when the validation ran. They differ by however long the run took.
    written_at: datetime
    result: ValidationResult
    #: The payload files written beside it. Empty is ordinary: most evidence
    #: lines carry a headline and no payload.
    payloads: list[StoredPayload] = Field(default_factory=list)


class StoredApproval(BaseModel):
    """One line of ``approvals.jsonl``: the human decision, on its own.

    Its own file rather than a rewrite of the result's line, which is the whole
    reason the results file can be append-only. Replay applies the *last* line
    for a ``validation_id`` on top of the result, so a re-approval is a new line
    and never an edit.
    """

    version: int = STORE_VERSION
    written_at: datetime
    validation_id: str
    approval: ValidationApproval


class RetentionPolicy(BaseModel):
    """How long written evidence is kept on this machine.

    Read from :class:`~workbench_server.models.settings.WorkbenchSettings`, which
    is **app-data scoped on purpose** — how much disk to spend is a fact about
    the machine, while the files it governs are workspace data. That split is
    stated in the Settings panel rather than papered over.
    """

    #: Days to keep. ``0`` keeps everything, which is a real answer and the one
    #: somebody under audit picks.
    days: int = DEFAULT_RETENTION_DAYS

    def cutoff(self, now: datetime) -> datetime | None:
        """The instant before which evidence may be swept, or ``None`` for never."""
        if self.days <= 0:
            return None
        return as_utc(now) - timedelta(days=self.days)

    def detail(self) -> str:
        """One sentence, used in the log line and in the panel copy."""
        if self.days <= 0:
            return "Kept forever — nothing is swept."
        return f"Kept for {self.days} days, then swept a whole month at a time."


class EvidenceExport(BaseModel):
    """``POST /api/validation/{id}/export`` — the proof, as a document.

    A :class:`~workbench_server.models.validation.ValidationResult` is a wire
    type; this is the thing somebody hands to somebody who was not there. The
    server renders it *and* writes it, so a run from the CLI leaves a file behind
    rather than a string in a terminal that scrolled away.
    """

    validation_id: str
    #: Workspace-relative path of the written report, forward-slashed so it reads
    #: the same on the wire as it does in the panel.
    path: str
    filename: str
    #: The whole report, so the caller that asked for it does not have to read the
    #: file back to show it.
    markdown: str
    bytes: int
    generated_at: datetime
