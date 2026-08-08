"""The compact confirmation the ``office_reconcile`` agent tool hands back.

The reconciliation *request* reuses
:class:`~workbench_server.models.reconciliation.ReconciliationSpec` verbatim — there is
no second spec shape. What is new here is the *answer* an agent
reads after a run: not the whole per-cell table (that lives behind the
:class:`~workbench_server.models.validation.ValidationResult`'s ``payload_ref``), but a
bounded, token-cheap verdict — the derived risk, a one-line summary, the pass/warn/fail
counts, and the worst few mismatches. The full table is *named*, never inlined, so one
tool call costs one small result rather than a whole workbook (CLAUDE.md's AXI shapes).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from workbench_server.models.validation import RiskLevel


class ReconcileMismatch(BaseModel):
    """One flagged cell, in the agent's own vocabulary: where, what it expected,
    what the workbook held, the gap, and why — enough to fix it without fetching
    the full table."""

    #: The A1 address (``Sheet1!D14``) or the wall-clock label of a time row.
    cell: str
    #: The agent's computed value, in the compared unit.
    expected: float
    #: The workbook's value in the compared unit, or ``None`` when the cell was
    #: empty, non-numeric or unreadable.
    actual: float | None = None
    #: ``actual - expected`` in the compared unit, or ``None`` when there is no
    #: actual to subtract.
    delta: float | None = None
    #: The short why — "unit mismatch: MWh vs MW", "outside tolerance (Δ 4995)",
    #: "empty cell". Clipped by the tool so the result stays inside its budget.
    reason: str | None = None


class ReconcileSummary(BaseModel):
    """The whole ``office_reconcile`` answer: a verdict an agent can act on in one
    round trip.

    ``worst`` is a bounded window (worst-first); ``withheld`` says how many flagged
    cells it does *not* show, and ``next_step`` names the full table and what to do —
    the three shapes a capped agent result owes the model."""

    #: The derived badge — never asserted by the tool, taken from the run.
    risk: RiskLevel
    #: One line for the agent: the headline, not the table.
    summary: str
    passed: int
    warned: int
    failed: int
    total: int
    #: The worst few flagged cells, worst-first. Empty on a clean pass, and empty
    #: (with the reason in ``summary``) on an honest fail with no table.
    worst: list[ReconcileMismatch] = Field(default_factory=list)
    #: Flagged cells beyond ``worst`` that were not shown, so the cap is never
    #: mistaken for "that was everything".
    withheld: int = 0
    #: The run's handle — the full per-cell table lives behind this result.
    validation_id: str
    #: What to do next: where the whole table is, and the obvious follow-up.
    next_step: str
