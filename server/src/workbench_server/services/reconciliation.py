"""The workbook↔code numeric reconciliation gate — M6's first *domain*
:class:`~workbench_server.services.validation.ValidationCheck`.

It reads the addressed ``.xlsx`` cells **directly with openpyxl**, deterministically,
on the machine that runs CI — it does *not* go through the live-Office COM bridge, so
the whole gate is testable with no Office installed. The COM path (reconciling against
a workbook a user has open and unsaved, with live formula results) is an optional later
PR that slots in behind the same :class:`WorkbookReader` protocol.

Three domain failure modes are first-class here, not comments:

* **Units are compared, not assumed** (:func:`convert`). A value read in kWh against an
  MWh expectation, or MWh against MW, cannot pass silently — the x1000 that hides inside
  a spreadsheet diff is caught, and a legitimate unit change is *named* in the evidence.
  Prices are currency-aware: NOK/SEK/DKK are first-class alongside EUR, scaling within a
  currency is a named multiply, and comparing *across* currencies is an explicit refusal
  (:class:`CrossCurrency`) rather than an invented FX rate.
* **Time-indexed rows are aligned by local wall-clock time** (:func:`_align_time_rows`),
  telling the repeated 02:00 of a fall-back DST day apart from an ordinary row that was
  pasted twice by asking the *zone* whether that wall clock is genuinely ambiguous
  (:func:`_is_ambiguous`) — never by order of appearance alone. A naive positional join
  that drops or invents the duplicated hour is refused, and so is a duplicate row
  masquerading as a fold.
* **No look-ahead in the pairing.** Rows are matched by their own timestamp value, never
  by position, so a missing or extra row surfaces as an unmatched expectation rather than
  a comparison against the wrong hour.

The check returns **one grouped** :class:`EvidenceItem` (kind ``numeric``) whose outcome
is the worst across all comparisons, and stores the whole
:class:`~workbench_server.models.reconciliation.ReconciliationReport` as a bounded payload
via :meth:`ValidationContext.store_payload` — the table never rides the result or the bus.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import openpyxl
import structlog
from pydantic import ValidationError

from workbench_server.models.reconciliation import (
    CellComparison,
    ExpectedValue,
    ReconciliationReport,
    ReconciliationSpec,
    TimeExpectation,
    TimeIndexSpec,
    Tolerance,
)
from workbench_server.models.validation import (
    CheckOutcome,
    EvidenceItem,
    EvidenceTruncation,
)
from workbench_server.services.validation import ValidationContext

log = structlog.get_logger()

#: Cap on the comparison rows carried inside one stored report. The table is a
#: bounded window, worst-first; beyond this, :class:`EvidenceTruncation` says how
#: much was cut and how to narrow the run (AXI shape 1).
MAX_COMPARISONS = 200

#: Least-to-most severe, for rolling per-cell outcomes up into one grouped verdict.
_OUTCOME_SEVERITY: dict[CheckOutcome, int] = {"pass": 0, "skipped": 1, "warn": 2, "fail": 3}


class _Unit(NamedTuple):
    """One known unit: what it measures, how it scales to that dimension's base
    unit, and — for monetary units only — which currency it is denominated in."""

    dimension: str
    factor: float
    currency: str | None = None


def _currency_units(code: str) -> dict[str, _Unit]:
    """The three monetary units of one currency: the bare amount and the two
    per-energy denominations an analyst actually types."""
    return {
        f"{code}/MWH": _Unit("price_energy", 1.0, code),
        f"{code}/KWH": _Unit("price_energy", 1e3, code),
        code: _Unit("money", 1.0, code),
    }


#: unit (upper-cased) → (dimension, factor to the dimension's base unit, currency).
#: Two units are comparable iff they share a dimension **and**, when monetary, the
#: same currency; the factor turns a value in the unit into the base unit (MWh for
#: energy, MW for power, <ccy>/MWh for price, <ccy> for money). A x1000 conversion
#: (kWh↔MWh, NOK/kWh↔NOK/MWh) therefore cannot happen by accident — it is a named,
#: explicit multiply or it is a fail.
#:
#: The Nordic currencies are first-class, not an afterthought: Nord Pool publishes
#: NO/SE/DK zone prices in both EUR and the local currency, and a model denominated
#: in NOK/MWh is the normal case for this product's users — it must reconcile with a
#: named x1000 like the EUR path does, not fall off the table as an "unknown unit".
#: Deliberately hand-rolled rather than pulled from a units library: the whole value
#: here is the *refusal* rules (dimension, currency), which a general converter would
#: happily paper over.
_UNIT_TO_BASE: dict[str, _Unit] = {
    "MWH": _Unit("energy", 1.0),
    "KWH": _Unit("energy", 1e-3),
    "GWH": _Unit("energy", 1e3),
    "WH": _Unit("energy", 1e-6),
    "MW": _Unit("power", 1.0),
    "KW": _Unit("power", 1e-3),
    "GW": _Unit("power", 1e3),
    "W": _Unit("power", 1e-6),
    "%": _Unit("ratio", 1.0),
    "": _Unit("dimensionless", 1.0),
    **_currency_units("EUR"),
    **_currency_units("NOK"),
    **_currency_units("SEK"),
    **_currency_units("DKK"),
}


class UnitMismatch(Exception):
    """Raised when two units name different physical dimensions (energy vs power),
    or when an unknown unit cannot be matched by string equality."""


class CrossCurrency(UnitMismatch):
    """Raised when two *known, same-dimension* monetary units are denominated in
    different currencies.

    A subclass of :class:`UnitMismatch` so every existing caller keeps failing
    safe, but a distinct type because it is a different verdict with a different
    fix: nothing is unknown here and nothing is malformed — the gate is refusing
    to invent an FX rate. Converting NOK/MWh to EUR/MWh needs a rate *for a
    particular hour*, which is an input the analyst owns and the gate never
    guesses. Silently applying one would be the same class of hidden multiply the
    unit table exists to catch, only worse: wrong by a number nobody chose.
    """


def convert(value: float, from_unit: str, to_unit: str) -> tuple[float, str | None]:
    """Convert ``value`` from ``from_unit`` into ``to_unit``.

    Returns ``(converted, note)`` where ``note`` names the conversion when one
    actually happened (so a x1000 is always visible in the evidence) and is ``None``
    for an identity. Raises :class:`UnitMismatch` across incompatible dimensions or
    for an unknown unit that does not string-match the target, and the narrower
    :class:`CrossCurrency` for two prices in different currencies.
    """
    a = from_unit.strip().upper()
    b = to_unit.strip().upper()
    if a == b:
        return value, None
    known_a = _UNIT_TO_BASE.get(a)
    known_b = _UNIT_TO_BASE.get(b)
    if known_a is None or known_b is None:
        raise UnitMismatch(f"unknown unit in {from_unit!r} vs {to_unit!r}")
    if known_a.dimension != known_b.dimension:
        raise UnitMismatch(f"{from_unit} is {known_a.dimension}, {to_unit} is {known_b.dimension}")
    if known_a.currency != known_b.currency:
        raise CrossCurrency(
            f"{from_unit} is in {known_a.currency} and {to_unit} is in {known_b.currency}; "
            "no FX rate will be invented — restate the expectation in "
            f"{known_a.currency}, or convert it in your own code at the rate you meant"
        )
    converted = value * known_a.factor / known_b.factor
    return converted, f"converted {value:g} {from_unit} → {converted:g} {to_unit}"


def within_tolerance(expected: float, actual: float, tol: Tolerance) -> bool:
    """A match iff the absolute delta is within ``abs`` *or* within ``rel*|expected|``.

    Never a naive ``==``: at least one band is guaranteed set by
    :class:`Tolerance`'s own validator."""
    delta = abs(actual - expected)
    if tol.abs is not None and delta <= tol.abs:
        return True
    return tol.rel is not None and delta <= tol.rel * abs(expected)


class WorkbookReader(Protocol):
    """The seam the openpyxl reader satisfies now and a COM reader satisfies later.

    A cell value is a number when numeric, ``None`` when empty, or a ``str`` when the
    cell holds text (which the gate treats as unreadable-for-numbers)."""

    def cell_value(self, sheet: str | None, cell: str) -> float | int | str | None: ...

    def column_pairs(
        self, sheet: str | None, ts_column: str, value_column: str, start_row: int
    ) -> list[tuple[object, object]]: ...


class OpenpyxlReader:
    """Reads cached, computed values from an ``.xlsx`` with no Office installed.

    ``data_only=True`` reads the values Excel last cached — the correct source for
    reconciling *numbers* rather than formula strings. ``read_only=True`` keeps a large
    workbook cheap. A workbook that was never opened in Excel has no cached values; the
    check turns the resulting all-``None`` into a **blocked-style fail that names the
    fix** rather than a silent green.
    """

    def __init__(self, path: Path) -> None:
        self._wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    def close(self) -> None:
        self._wb.close()

    def _sheet(self, sheet: str | None) -> object:
        if sheet is None:
            return self._wb.active
        if sheet not in self._wb.sheetnames:
            raise KeyError(sheet)
        return self._wb[sheet]

    def cell_value(self, sheet: str | None, cell: str) -> float | int | str | None:
        ws = self._sheet(sheet)
        value = ws[cell].value  # type: ignore[index]
        if value is None or isinstance(value, int | float | str):
            return value
        return str(value)

    def column_pairs(
        self, sheet: str | None, ts_column: str, value_column: str, start_row: int
    ) -> list[tuple[object, object]]:
        ws = self._sheet(sheet)
        pairs: list[tuple[object, object]] = []
        row = start_row
        while True:
            ts = ws[f"{ts_column}{row}"].value  # type: ignore[index]
            val = ws[f"{value_column}{row}"].value  # type: ignore[index]
            if ts is None and val is None:
                break
            pairs.append((ts, val))
            row += 1
        return pairs


def _split_cell(cell: str) -> tuple[str | None, str]:
    """``Sheet1!D14`` → ``("Sheet1", "D14")``; ``D14`` → ``(None, "D14")``."""
    if "!" in cell:
        sheet, addr = cell.split("!", 1)
        return sheet or None, addr
    return None, cell


def _as_number(value: object) -> float | None:
    """A cell's value as a float, or ``None`` when it is empty or non-numeric.

    Booleans are *not* numbers here — a ``TRUE`` cell reconciled as ``1.0`` is a lie."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _tolerance_for(spec: ReconciliationSpec, cell: str) -> Tolerance:
    return spec.per_cell_tolerance.get(cell, spec.default_tolerance)


def _compare_one(
    address: str,
    label: str | None,
    expected: float,
    expected_unit: str,
    cell_unit: str,
    raw: object,
    tol: Tolerance,
) -> CellComparison:
    """Turn one expected/actual pair into a verdict, unit-aware."""
    actual = _as_number(raw)
    if actual is None:
        reason = "empty cell" if raw is None else f"non-numeric cell ({raw!r})"
        return CellComparison(
            cell=address,
            label=label,
            expected=expected,
            actual=None,
            unit=expected_unit,
            delta=None,
            outcome="fail",
            reason=reason,
        )
    try:
        converted, note = convert(actual, cell_unit, expected_unit)
    except CrossCurrency as exc:
        # Named apart from a unit mismatch on purpose: the units are both known and
        # both prices, so "unit mismatch" would send the reader looking for a typo.
        return CellComparison(
            cell=address,
            label=label,
            expected=expected,
            actual=actual,
            unit=expected_unit,
            delta=None,
            outcome="fail",
            reason=f"cross-currency comparison refused: {exc}",
        )
    except UnitMismatch as exc:
        return CellComparison(
            cell=address,
            label=label,
            expected=expected,
            actual=actual,
            unit=expected_unit,
            delta=None,
            outcome="fail",
            reason=f"unit mismatch: {exc}",
        )
    delta = converted - expected
    if within_tolerance(expected, converted, tol):
        return CellComparison(
            cell=address,
            label=label,
            expected=expected,
            actual=converted,
            unit=expected_unit,
            delta=delta,
            outcome="pass",
            reason=note,
        )
    band = f"outside tolerance (Δ {delta:g})"
    reason = f"{note}; {band}" if note else band
    return CellComparison(
        cell=address,
        label=label,
        expected=expected,
        actual=converted,
        unit=expected_unit,
        delta=delta,
        outcome="fail",
        reason=reason,
    )


def _time_address(exp: TimeExpectation) -> str:
    """The wall-clock label a time-row comparison is keyed by in the evidence."""
    return exp.timestamp + (f" (fold {exp.fold})" if exp.fold else "")


def _parse_local(ts: str) -> datetime:
    """A naive local ISO timestamp string. Raises ``ValueError`` on anything else."""
    parsed = datetime.fromisoformat(ts)
    return parsed.replace(tzinfo=None)


def _as_local_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return _parse_local(value)
        except ValueError:
            return None
    return None


def _is_ambiguous(local: datetime, zone: ZoneInfo) -> bool:
    """Is this naive local wall-clock time one the zone genuinely writes **twice**?

    True only on a fall-back transition — 2024-10-27 02:30 in ``Europe/Oslo`` is both
    the CEST 02:30 and the CET 02:30 an hour later. PEP 495's own test: the two folds
    of the same wall time carry different UTC offsets. The *imaginary* times of a
    spring-forward gap also disagree on offset, so they are excluded first by the
    round-trip — a wall time that does not survive local → UTC → local never happened,
    and a row carrying it is not a second occurrence of anything.
    """
    fold0 = local.replace(tzinfo=zone, fold=0)
    if fold0.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
        return False  # imaginary: inside the spring-forward gap
    return fold0.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset()


class _AlignedRows(NamedTuple):
    """The wall-clock index, plus the timestamps that repeated when the zone says
    they should not have."""

    #: ``(local wall-clock, fold)`` → the value that row carried.
    values: dict[tuple[datetime, int], object]
    #: local wall-clock → how many rows carried it, for timestamps that repeated
    #: **without** being an ambiguous (fall-back) local time in the zone.
    duplicates: dict[datetime, int]


def _align_time_rows(pairs: list[tuple[object, object]], zone: ZoneInfo) -> _AlignedRows:
    """Map ``(local wall-clock, fold)`` → value, keyed by the timestamp the row
    *carries* and never by its position.

    A second row at the same wall-clock is a ``fold=1`` **only where ``zone`` actually
    repeats that hour**. That test is the whole point: order of appearance alone cannot
    tell the CET 02:00 of a fall-back day apart from an ordinary row someone pasted
    twice, and a gate that guesses will *match* a fold-1 expectation against corrupt
    input and report it reconciled. So a repeat at a non-ambiguous local time (and any
    third-and-later repeat, which no zone produces) is not indexed at all: it is
    reported to the caller as a duplicate row, and the expectation it would have
    satisfied gets no value to satisfy it.

    Aligning by value (not index) is what keeps a missing or extra row from silently
    shifting every later hour."""
    seen: dict[datetime, int] = {}
    values: dict[tuple[datetime, int], object] = {}
    duplicates: dict[datetime, int] = {}
    for ts_raw, value in pairs:
        local = _as_local_datetime(ts_raw)
        if local is None:
            continue
        occurrence = seen.get(local, 0)
        seen[local] = occurrence + 1
        if occurrence == 0:
            values[(local, 0)] = value
            continue
        if occurrence == 1 and _is_ambiguous(local, zone):
            values[(local, 1)] = value  # the genuine second pass of a fall-back hour
            continue
        duplicates[local] = seen[local]
    return _AlignedRows(values=values, duplicates=duplicates)


class ReconciliationCheck:
    """The registered gate. ``id`` is the handle a
    :class:`~workbench_server.models.validation.ValidationSpec` names to run it."""

    id = "reconciliation"

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        try:
            spec = ReconciliationSpec.model_validate(ctx.params)
        except ValidationError as exc:
            return [self._spec_error(f"invalid reconciliation spec: {exc.error_count()} error(s)")]

        workbook = self._resolve(ctx.root, spec.workbook)
        if workbook is None:
            return [self._spec_error(f"workbook path escapes the workspace: {spec.workbook!r}")]
        if not workbook.is_file():
            return [self._spec_error(f"workbook not found: {spec.workbook}")]

        if not spec.expectations and not (spec.time_index and spec.time_index.expectations):
            return [self._spec_error("spec has no expectations to reconcile")]

        try:
            reader = OpenpyxlReader(workbook)
        except Exception as exc:  # a corrupt/locked workbook is a fail that names why
            log.warning("reconciliation.unreadable", workbook=spec.workbook, error=str(exc))
            return [self._spec_error(f"workbook could not be opened: {exc}")]

        try:
            comparisons = self._compare_cells(spec, reader)
            time_rows, structural = self._compare_time_rows(spec, reader)
            comparisons += time_rows
        except _CheckBlocked as blocked:
            return [self._spec_error(blocked.reason)]
        finally:
            reader.close()

        # The grouped comparison line first (it is what a reader looks at), then any
        # structural findings about the rows themselves. Both feed `derive_risk`, so a
        # duplicated-row fail cannot be outvoted by comparisons that happened to match.
        return [self._grouped(spec, comparisons, ctx), *structural]

    def _resolve(self, root: Path, relative: str) -> Path | None:
        """Jail the workbook path against the workspace root (the ``safe_path``
        rule, inline: a check is handed the root, not the ``Workspace``)."""
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            return None
        return candidate

    def _compare_cells(
        self, spec: ReconciliationSpec, reader: WorkbookReader
    ) -> list[CellComparison]:
        out: list[CellComparison] = []
        empties = 0
        for exp in spec.expectations:
            sheet, addr = _split_cell(exp.cell)
            try:
                raw = reader.cell_value(sheet, addr)
            except Exception as exc:
                # The reader seam: a malformed address (e.g. a bare row "14", which
                # openpyxl resolves to a *tuple* of cells whose `.value` raises
                # AttributeError) or an unknown sheet is one isolated fail row that
                # names the reason — never an exception that sinks the whole run.
                log.warning("reconciliation.cell_unreadable", cell=exp.cell, error=str(exc))
                out.append(self._unreadable_cell(exp, f"cannot read {exp.cell}: {exc}"))
                empties += 1
                continue
            if raw is None:
                empties += 1
            out.append(
                _compare_one(
                    exp.cell,
                    exp.label,
                    exp.expected,
                    exp.unit,
                    exp.cell_unit if exp.cell_unit is not None else exp.unit,
                    raw,
                    _tolerance_for(spec, exp.cell),
                )
            )
        # A workbook never opened in Excel caches no values: every addressed cell
        # reads empty. Report that as its own blocked-style failure that names the
        # fix, rather than a wall of identical "empty cell" rows.
        if spec.expectations and empties == len(spec.expectations):
            raise _CheckBlocked(
                f"every addressed cell in {spec.workbook} is empty — open and save the "
                "workbook in Excel once so values are cached, then re-run"
            )
        return out

    def _compare_time_rows(
        self, spec: ReconciliationSpec, reader: WorkbookReader
    ) -> tuple[list[CellComparison], list[EvidenceItem]]:
        """The time-indexed comparisons, plus any structural evidence about the rows
        themselves (duplicated timestamps) that is not a per-expectation verdict."""
        ti = spec.time_index
        if ti is None or not ti.expectations:
            return [], []
        if spec.timezone is None:
            raise _CheckBlocked("time_index is set but `timezone` is missing")
        try:
            zone = ZoneInfo(spec.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise _CheckBlocked(f"unknown timezone {spec.timezone!r}: {exc}") from exc

        try:
            pairs = reader.column_pairs(
                ti.sheet, ti.timestamp_column, ti.value_column, ti.start_row
            )
        except Exception as exc:
            # A bad sheet (KeyError) or a malformed column letter is isolated to the
            # time-index rows — each becomes a fail that names why — so the direct-cell
            # comparisons in the same run still land in the stored report.
            log.warning(
                "reconciliation.time_index_unreadable",
                workbook=spec.workbook,
                error=str(exc),
            )
            reason = (
                f"cannot read time-index columns ({ti.timestamp_column}/{ti.value_column}): {exc}"
            )
            return [
                CellComparison(
                    cell=_time_address(exp),
                    label=exp.label,
                    expected=exp.expected,
                    actual=None,
                    unit=exp.unit,
                    outcome="fail",
                    reason=reason,
                )
                for exp in ti.expectations
            ], []
        aligned = _align_time_rows(pairs, zone)
        structural = self._duplicate_evidence(spec, ti, aligned)

        out: list[CellComparison] = []
        for exp in ti.expectations:
            address = _time_address(exp)
            try:
                local = _parse_local(exp.timestamp)
            except ValueError:
                out.append(
                    CellComparison(
                        cell=address,
                        label=exp.label,
                        expected=exp.expected,
                        actual=None,
                        unit=exp.unit,
                        outcome="fail",
                        reason=f"unparseable timestamp {exp.timestamp!r}",
                    )
                )
                continue
            if exp.fold and not _is_ambiguous(local, zone):
                # A fold only exists where the zone repeats an hour. Asking for one
                # anywhere else is a spec error, and saying "alignment gap" here would
                # send the analyst hunting for a missing workbook row that was never
                # supposed to exist.
                out.append(
                    CellComparison(
                        cell=address,
                        label=exp.label,
                        expected=exp.expected,
                        actual=None,
                        unit=exp.unit,
                        outcome="fail",
                        reason=(
                            f"fold {exp.fold} was requested but {exp.timestamp} is not an "
                            f"ambiguous local time in {spec.timezone} — that wall clock "
                            "occurs once, so there is no second occurrence to match"
                        ),
                    )
                )
                continue
            key = (local, exp.fold)
            if key not in aligned.values:
                out.append(
                    CellComparison(
                        cell=address,
                        label=exp.label,
                        expected=exp.expected,
                        actual=None,
                        unit=exp.unit,
                        outcome="fail",
                        reason="no workbook row at this local time (alignment gap)",
                    )
                )
                continue
            cell_unit = ti.value_unit if ti.value_unit is not None else exp.unit
            out.append(
                _compare_one(
                    address,
                    exp.label,
                    exp.expected,
                    exp.unit,
                    cell_unit,
                    aligned.values[key],
                    _tolerance_for(spec, exp.timestamp),
                )
            )
        return out, structural

    def _duplicate_evidence(
        self, spec: ReconciliationSpec, ti: TimeIndexSpec, aligned: _AlignedRows
    ) -> list[EvidenceItem]:
        """One ``fail`` line per duplicated timestamp — the defect that used to be a
        silent ``fold=1``.

        It is its own :class:`EvidenceItem` rather than a comparison row because it is
        a statement about the *workbook*, not about any one expectation: the duplicate
        is a fail whether or not an expectation happens to address that hour, and a
        result that carries it can never derive ``pass``.
        """
        out: list[EvidenceItem] = []
        for local, count in sorted(aligned.duplicates.items()):
            stamp = local.isoformat()
            log.warning(
                "reconciliation.duplicate_timestamp",
                workbook=spec.workbook,
                timestamp=stamp,
                count=count,
                timezone=spec.timezone,
            )
            out.append(
                EvidenceItem(
                    kind="numeric",
                    label=f"duplicate timestamp row ({spec.workbook})",
                    outcome="fail",
                    detail=(
                        f"{stamp} appears {count} times in column {ti.timestamp_column}, but "
                        f"it is not an ambiguous local time in {spec.timezone} — that wall "
                        "clock occurs once, so these are duplicate rows, not a DST fall-back "
                        "repeat. The duplicates were not matched against any expectation. "
                        "Remove them (or fix the timestamps), then re-run."
                    ),
                )
            )
        return out

    def _grouped(
        self,
        spec: ReconciliationSpec,
        comparisons: list[CellComparison],
        ctx: ValidationContext,
    ) -> EvidenceItem:
        """One grouped ``numeric`` evidence line; the full table goes to the payload
        store and is named by ``payload_ref``."""
        total = len(comparisons)
        matched = sum(1 for c in comparisons if c.outcome == "pass")
        mismatched = sum(1 for c in comparisons if c.outcome == "fail")
        worst = max(
            (c.outcome for c in comparisons),
            key=lambda o: _OUTCOME_SEVERITY[o],
            default="pass",
        )

        # Worst-first, then bound the stored window (AXI shape 1).
        ordered = sorted(comparisons, key=lambda c: -_OUTCOME_SEVERITY[c.outcome])
        truncated: EvidenceTruncation | None = None
        window = ordered
        if total > MAX_COMPARISONS:
            window = ordered[:MAX_COMPARISONS]
            truncated = EvidenceTruncation(
                shown=MAX_COMPARISONS,
                total=total,
                detail=(
                    f"showing {MAX_COMPARISONS} of {total} comparisons, worst first; "
                    "reconcile fewer cells per run to see the rest"
                ),
            )
        report = ReconciliationReport(
            workbook=spec.workbook,
            matched=matched,
            mismatched=mismatched,
            total=total,
            comparisons=window,
            truncated=truncated,
        )
        ref = ctx.store_payload("numeric", report)

        if mismatched == 0:
            detail = f"All {total} cells reconcile within tolerance."
        else:
            first_bad = next(c for c in ordered if c.outcome == "fail")
            detail = (
                f"{mismatched} of {total} cells mismatch beyond tolerance. "
                f"First: {first_bad.cell} in {spec.workbook}."
            )
        return EvidenceItem(
            kind="numeric",
            label=f"workbook↔code reconciliation ({spec.workbook})",
            outcome=worst,
            detail=detail,
            payload_ref=ref,
        )

    def _unreadable_cell(self, exp: ExpectedValue, reason: str) -> CellComparison:
        return CellComparison(
            cell=exp.cell,
            label=exp.label,
            expected=exp.expected,
            actual=None,
            unit=exp.unit,
            outcome="fail",
            reason=reason,
        )

    def _spec_error(self, reason: str) -> EvidenceItem:
        """A single ``fail`` evidence line — never a silent skip and never a
        blank result a reader has to interpret (AXI shape 2)."""
        return EvidenceItem(
            kind="numeric",
            label="workbook↔code reconciliation",
            outcome="fail",
            detail=reason,
        )


class _CheckBlocked(Exception):
    """Internal: a condition that stops the whole reconciliation with one reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
