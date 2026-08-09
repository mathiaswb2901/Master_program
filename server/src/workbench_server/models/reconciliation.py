"""Workbook↔code numeric reconciliation: the typed inputs and outputs of M6's
first *domain* validation check.

The gate answers one question — *do the numbers the agent computed agree with the
numbers a workbook actually contains?* — and answers it in the way an electricity
analyst needs it answered, not as a spreadsheet diff:

* **units are compared, not assumed** — a value 1000x off because kWh was read as
  MWh is the single most common silent bug in this domain, and this gate is where
  it is caught rather than where it lives;
* **time-indexed rows are aligned by local wall-clock time**, so a workbook whose
  fall-back DST day has 25 hourly rows (two 02:00s) lines up against code that
  computed the same 25 local hours — a naive positional join that silently drops
  or invents the duplicated hour is exactly the boundary bug this refuses.

None of these types is a REST/WS body of its own. A :class:`ReconciliationSpec`
travels *inside* ``ValidationSpec.params`` (a ``dict`` on the wire) and a
:class:`ReconciliationReport` is a bounded detail payload stored behind an
``EvidenceItem.payload_ref`` — so they are internal typed structures, not new wire
contracts, and the Review panel that renders them (M6 PR3) is what will mirror the
report into ``ui/src/types.ts``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from workbench_server.models.office_bridge import CalculationState
from workbench_server.models.validation import CheckOutcome, EvidenceTruncation


class Tolerance(BaseModel):
    """How close is close enough. Absolute (in the value's own unit), relative
    (a fraction — ``0.001`` is 0.1 %), or both.

    A match is ``|actual - expected| <= abs`` **or** ``<= rel * |expected|``; both
    set is the standard way to compare numbers spanning orders of magnitude — an
    absolute floor for near-zero values plus a relative band for large ones. At
    least one must be set: a missing tolerance is a **spec error reported up
    front**, never a silent exact-``==`` that fails on the last bit of a float.
    """

    abs: float | None = None
    rel: float | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> Tolerance:
        if self.abs is None and self.rel is None:
            raise ValueError("tolerance needs at least one of `abs` or `rel`")
        if self.abs is not None and self.abs < 0:
            raise ValueError("tolerance `abs` must be non-negative")
        if self.rel is not None and self.rel < 0:
            raise ValueError("tolerance `rel` must be non-negative")
        return self


class ExpectedValue(BaseModel):
    """One code-computed number to check against one workbook cell.

    The expected value is supplied *as data* — never as code to execute (a shell
    in a JSON body is exactly what the never-execute doctrine forbids). Where it
    came from is the analyst's own script; the gate's contract is "here is what the
    code says, here is the cell, tell me if they agree".
    """

    #: A1 address, sheet-qualified (``Sheet1!D14``) or bare (``D14`` → active sheet).
    cell: str
    #: The code-computed value, in :attr:`unit`.
    expected: float
    #: Unit of the *expected* value: ``MWh``, ``MW``, a price or amount in any of
    #: EUR/NOK/SEK/DKK (``NOK/MWh``, ``EUR/kWh``, ``SEK``…), ``%`` or ``""``
    #: (dimensionless). Scaling *within* a currency is a named conversion; comparing
    #: *across* currencies is refused rather than resolved with an invented FX rate.
    unit: str = ""
    #: Declared unit of the *workbook cell*. ``None`` means "same as
    #: :attr:`unit`" (compare directly). When it differs, a compatible pair is
    #: converted through the explicit factor table and the conversion is named in
    #: the evidence; an incompatible pair (energy vs power) is a ``fail``.
    cell_unit: str | None = None
    #: Human label for the evidence, e.g. "Day-ahead revenue, 2024-03-31 hour 02".
    label: str | None = None


class TimeExpectation(BaseModel):
    """One code-computed number addressed by *local wall-clock time* rather than by
    cell — how a time-indexed workbook is reconciled without a positional join.

    The address is a **naive local wall clock plus a fold**, never an instant. Those
    two fields together are what name one hour unambiguously in a zone that repeats
    one: an offset-bearing timestamp is refused rather than normalised, because
    stripping the offset would give the fall-back day's two 02:00s the same key.
    """

    #: **Naive** local timestamp, ISO-8601, e.g. ``2024-10-27T02:00:00``. Interpreted
    #: in :attr:`ReconciliationSpec.timezone`, so it carries **no UTC offset and no
    #: trailing ``Z``** — ``2024-10-27T02:00:00+01:00`` is a *fail* naming this rule,
    #: not a value quietly stripped to its wall clock. Use :attr:`fold` to say which
    #: occurrence of a repeated hour is meant; that is the whole reason the field
    #: exists. (Enforced by the check, one named fail per row, rather than by a
    #: validator here — a spec-level rejection would sink the whole run over one row.)
    timestamp: str
    #: The code-computed value for that hour.
    expected: float
    #: Unit of the expected value (see :class:`ExpectedValue`).
    unit: str = ""
    #: Which occurrence of a repeated wall-clock time to match on the fall-back DST
    #: day: ``0`` is the first 02:00 (the summer-offset one), ``1`` the second. This
    #: is the *only* way to address the second occurrence — an offset on
    #: :attr:`timestamp` is refused. A non-zero fold is only meaningful where
    #: :attr:`ReconciliationSpec.timezone` genuinely repeats that wall clock; asking
    #: for one anywhere else is a **fail** naming the timestamp, never silently
    #: ignored — that silence is what let a duplicated row pass.
    fold: int = 0
    label: str | None = None


class TimeIndexSpec(BaseModel):
    """A time-indexed comparison: a timestamp column and a value column, and the
    expectations addressed by wall-clock time.

    The gate reads the two columns and looks each expectation up by
    ``(wall-clock, fold)`` — never by row position. A second row at the same wall
    clock becomes ``fold=1`` only where the spec's zone *actually* repeats that hour;
    any other repeat is a duplicated row and is reported as its own ``fail``."""

    #: Column letter carrying the local timestamps, e.g. ``A``. Naive local wall
    #: clock, same contract as :attr:`TimeExpectation.timestamp`: a cell carrying a
    #: UTC offset (a tz-aware value, or text like ``2024-10-27T02:00:00+01:00``) is
    #: reported as its own ``fail`` and not indexed, because stripping the offset
    #: would leave row order to decide which fall-back hour is which.
    timestamp_column: str
    #: Column letter carrying the values to reconcile, e.g. ``B``.
    value_column: str
    #: Declared unit of the value column (``None`` = each expectation's own unit).
    value_unit: str | None = None
    #: 1-based row the data starts on (past any header). Default ``2``.
    start_row: int = 2
    #: Sheet the two columns live on; ``None`` = active sheet.
    sheet: str | None = None
    expectations: list[TimeExpectation] = Field(default_factory=list)


class ReconciliationSpec(BaseModel):
    """The whole reconciliation request — carried inside ``ValidationSpec.params``.

    Either or both modes may be populated: :attr:`expectations` (direct cells) and
    :attr:`time_index` (wall-clock-aligned rows). A spec with neither is a spec
    error the check reports as a ``fail`` rather than a silent empty pass.
    """

    #: Workspace-relative path to the ``.xlsx`` (jailed against the workspace root).
    workbook: str
    expectations: list[ExpectedValue] = Field(default_factory=list)
    #: The default match band, applied to any cell without a per-cell override.
    default_tolerance: Tolerance
    #: cell → tolerance override, keyed by the expectation's ``cell`` string.
    per_cell_tolerance: dict[str, Tolerance] = Field(default_factory=dict)
    #: IANA zone name for a time-indexed workbook, e.g. ``Europe/Oslo``. Required
    #: when :attr:`time_index` is set.
    timezone: str | None = None
    time_index: TimeIndexSpec | None = None


class ReadSource(BaseModel):
    """Where the numbers in a report actually came from.

    Once there are two places a workbook's values can be read — the ``.xlsx`` on
    disk and the live Excel that has it open — a badge that does not say which
    is not proof, it is a colour. Every report carries this, and the grouped
    evidence line ends with it in words.

    **The two shapes are deliberately different**, because what makes each one
    trustworthy is different. A live read is trustworthy when the instance had
    finished calculating (:attr:`calculation`), and its relationship to the file
    is the interesting fact (:attr:`saved`). A file read is trustworthy when the
    file is the newest thing there is (:attr:`mtime`) and when Excel had
    actually cached values into it (:attr:`cached_values`) — a workbook written
    by a script and never opened has formulas and no numbers, which reads as a
    column of empties rather than as a disagreement.
    """

    #: ``live`` = read out of the running Excel that has the workbook open;
    #: ``file`` = read from the ``.xlsx`` on disk with openpyxl.
    kind: Literal["live", "file"]
    #: When the read happened, as **naive local wall clock** — the module's own
    #: contract for every timestamp, and server-minted. Never taken from a COM
    #: object: those arrive tz-aware with an offset that is not this machine's
    #: zone (``office_com.naive_local``).
    read_at: datetime
    #: Live only: whether Excel had finished calculating when the values were
    #: taken. Anything but ``done`` means the numbers were still moving.
    calculation: CalculationState | None = None
    #: Live only: ``Workbook.Saved`` at read time. ``False`` says the workbook
    #: had edits the file on disk did not — which is exactly the case a disk read
    #: would have got wrong.
    saved: bool | None = None
    #: File only: the file's modification time, naive local.
    mtime: datetime | None = None
    #: File only, and load-bearing: whether any addressed cell actually held a
    #: cached value. ``False`` means openpyxl saw an empty workbook — usually one
    #: no Excel has ever opened — rather than a workbook full of zeros.
    cached_values: bool | None = None


class CellComparison(BaseModel):
    """One expected-vs-actual verdict — the row an analyst reads in the table."""

    #: The address checked (an A1 cell, or a wall-clock label for a time row).
    cell: str
    label: str | None = None
    #: Expected value, in the *compared* unit (after any conversion).
    expected: float
    #: Actual value from the workbook in the compared unit, or ``None`` when the
    #: cell was empty, non-numeric or unreadable.
    actual: float | None = None
    #: The unit the comparison was made in.
    unit: str = ""
    #: ``actual - expected`` in the compared unit, or ``None`` when there is no
    #: actual to subtract.
    delta: float | None = None
    outcome: CheckOutcome = "pass"
    #: Why, when it is not a plain pass: "unit mismatch: expected MWh, cell is MW",
    #: "outside tolerance (Δ 4995.0)", "empty cell", "converted 5000 kWh → 5 MWh".
    reason: str | None = None


class ReconciliationReport(BaseModel):
    """The full table behind an ``EvidenceItem.payload_ref`` (kind ``numeric``).

    :attr:`comparisons` is a bounded window, worst-first; :attr:`truncated` says so
    when it was capped (AXI shape 1) so a reader never mistakes the cap for "that
    was everything"."""

    workbook: str
    matched: int
    mismatched: int
    total: int
    #: Which of the two readers produced the numbers above, and what made it
    #: trustworthy. Required, not optional: a report that cannot say where its
    #: values came from is the thing this field exists to make impossible.
    source: ReadSource
    comparisons: list[CellComparison] = Field(default_factory=list)
    truncated: EvidenceTruncation | None = None
