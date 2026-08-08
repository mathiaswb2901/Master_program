"""The workbook↔code numeric reconciliation gate — M6's first domain check.

Fake-first / no Office: every fixture is a tiny ``.xlsx`` built with openpyxl in the
test, read back with openpyxl, so the whole gate is exercised deterministically on the
CI machine. What is under test is the *domain* behaviour that makes this more than a
spreadsheet diff:

* a matching workbook is ``pass`` and a mismatch beyond tolerance is a ``fail`` that
  derives risk ``high`` — never a silent green;
* a value 1000x off because kWh was read as MWh is **caught**, and a legitimate unit
  conversion is *named*; an incompatible dimension (energy vs power) is a fail;
* a fall-back DST day's 25 hourly rows (two 02:00s) align by local wall-clock time, not
  by row position;
* a missing/empty cell and an unreadable/unfound workbook are explicit fails that name
  the reason;
* the full comparison table is stored as a bounded payload and reachable through the
  service's payload accessor — it never rides the result;
* the check registers into the production wiring and ``POST /api/validation/run`` with
  its id runs a real reconciliation.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.reconciliation import (
    ExpectedValue,
    ReconciliationReport,
    ReconciliationSpec,
    TimeExpectation,
    TimeIndexSpec,
    Tolerance,
)
from workbench_server.models.validation import ValidationResult, ValidationSpec, ValidationSubject
from workbench_server.services.event_bus import EventBus
from workbench_server.services.reconciliation import (
    ReconciliationCheck,
    UnitMismatch,
    convert,
    within_tolerance,
)
from workbench_server.services.validation import ValidationService

# --------------------------------------------------------------------------- helpers


def make_workbook(path: Path, cells: dict[str, Any], sheet: str = "Sheet1") -> None:
    """A tiny workbook with the given A1 cells populated."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for addr, value in cells.items():
        ws[addr] = value
    wb.save(path)


def make_dst_workbook(path: Path) -> None:
    """A price sheet for the Europe/Oslo fall-back day 2024-10-27: 25 hourly rows,
    with 02:00 appearing twice (the CEST hour then the CET hour).

    The value at each hour is deliberately its hour number, except the two 02:00s
    which carry distinct values (20.0 then 21.0) so a fold mix-up is visible. The
    05:00 row sits at *data index 6* (two 02:00s pushed it down), so any positional
    join would pair the 05:00 expectation against the 04:00 row and fail.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws["A1"] = "timestamp"
    ws["B1"] = "price_eur_mwh"
    rows: list[tuple[datetime, float]] = []
    rows.append((datetime(2024, 10, 27, 0), 0.0))
    rows.append((datetime(2024, 10, 27, 1), 1.0))
    rows.append((datetime(2024, 10, 27, 2), 20.0))  # fold 0 (CEST)
    rows.append((datetime(2024, 10, 27, 2), 21.0))  # fold 1 (CET)
    for hour in range(3, 24):
        rows.append((datetime(2024, 10, 27, hour), float(hour)))
    for offset, (ts, value) in enumerate(rows):
        r = 2 + offset
        ws[f"A{r}"] = ts
        ws[f"B{r}"] = value
    wb.save(path)


async def run_recon(
    root: Path, spec: ReconciliationSpec
) -> tuple[ValidationService, ValidationResult]:
    """Register the real check on a service and run a reconciliation end-to-end."""
    service = ValidationService(root, EventBus())
    service.register(ReconciliationCheck())
    subject = ValidationSubject(kind="file", ref=spec.workbook, label=spec.workbook)
    vspec = ValidationSpec(subject=subject, checks=["reconciliation"], params=spec.model_dump())
    result = await service.run(vspec)
    return service, result


def report_of(service: ValidationService, result: ValidationResult) -> ReconciliationReport:
    """The stored table behind the grouped evidence line."""
    ref = result.evidence[0].payload_ref
    assert ref is not None
    payload = service.payload("numeric", ref)
    assert isinstance(payload, ReconciliationReport)
    return payload


# --------------------------------------------------------------- unit conversion core


def test_within_tolerance_is_never_a_naive_equality() -> None:
    tol = Tolerance(abs=0.5)
    assert within_tolerance(10.0, 10.4, tol)
    assert not within_tolerance(10.0, 10.6, tol)
    rel = Tolerance(rel=0.01)
    assert within_tolerance(1000.0, 1009.0, rel)
    assert not within_tolerance(1000.0, 1011.0, rel)


def test_convert_names_a_conversion_and_refuses_incompatible_dimensions() -> None:
    converted, note = convert(5000.0, "kWh", "MWh")
    assert converted == pytest.approx(5.0)
    assert note is not None and "kWh" in note and "MWh" in note
    # identity is silent
    assert convert(5.0, "MWh", "MWh") == (5.0, None)
    # energy vs power cannot be coerced
    with pytest.raises(UnitMismatch):
        convert(5.0, "MW", "MWh")


# --------------------------------------------------------------- the matching cases


@pytest.mark.asyncio
async def test_a_matching_workbook_is_all_pass_and_risk_pass(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 42.5, "E2": 1000.0})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[
            ExpectedValue(cell="Sheet1!D14", expected=42.5, unit="MWh"),
            ExpectedValue(cell="E2", expected=1000.0, unit="EUR"),
        ],
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "pass"
    assert result.evidence[0].outcome == "pass"
    assert "reconcile within tolerance" in result.evidence[0].detail
    report = report_of(service, result)
    assert (report.matched, report.mismatched, report.total) == (2, 0, 2)


@pytest.mark.asyncio
async def test_within_tolerance_passes(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 42.5004})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[ExpectedValue(cell="D14", expected=42.5, unit="MWh")],
    )
    _, result = await run_recon(tmp_path, spec)
    assert result.risk == "pass"


@pytest.mark.asyncio
async def test_a_mismatch_beyond_tolerance_is_a_fail_and_risk_high(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 50.0})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001, rel=0.001),
        expectations=[ExpectedValue(cell="D14", expected=42.5, unit="MWh")],
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    assert result.evidence[0].outcome == "fail"
    # AXI shape 3: the detail ends by naming the workbook and the first bad cell.
    assert "book.xlsx" in result.evidence[0].detail and "D14" in result.evidence[0].detail
    row = report_of(service, result).comparisons[0]
    assert row.delta == pytest.approx(7.5)
    assert "outside tolerance" in (row.reason or "")


@pytest.mark.asyncio
async def test_per_cell_tolerance_overrides_the_default(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 43.0})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        per_cell_tolerance={"D14": Tolerance(abs=1.0)},
        expectations=[ExpectedValue(cell="D14", expected=42.5, unit="MWh")],
    )
    _, result = await run_recon(tmp_path, spec)
    assert result.risk == "pass"  # would fail under the 0.001 default


# --------------------------------------------------------------- the unit failure modes


@pytest.mark.asyncio
async def test_a_kwh_value_against_an_mwh_expectation_is_caught_not_silent(
    tmp_path: Path,
) -> None:
    """The whole point of the gate: a cell holding 5000 (kWh magnitude) checked
    against a 5.0 MWh expectation is a 1000x error, and it must be a fail — not a
    green tick — when the analyst did not declare the cell was in kWh."""
    make_workbook(tmp_path / "book.xlsx", {"D14": 5000.0})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(rel=0.01),
        expectations=[ExpectedValue(cell="D14", expected=5.0, unit="MWh")],
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    row = report_of(service, result).comparisons[0]
    assert row.outcome == "fail"
    assert row.delta == pytest.approx(4995.0)


@pytest.mark.asyncio
async def test_a_declared_kwh_cell_converts_and_names_the_conversion(tmp_path: Path) -> None:
    """The legitimate case: the analyst declares the cell is in kWh, so 5000 kWh
    converts to 5 MWh, matches, and the conversion is named in the evidence so the
    1000x can never hide inside the pass."""
    make_workbook(tmp_path / "book.xlsx", {"D14": 5000.0})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(rel=0.001),
        expectations=[ExpectedValue(cell="D14", expected=5.0, unit="MWh", cell_unit="kWh")],
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "pass"
    row = report_of(service, result).comparisons[0]
    assert row.outcome == "pass"
    assert "kWh" in (row.reason or "") and "MWh" in (row.reason or "")


@pytest.mark.asyncio
async def test_incompatible_dimensions_are_a_fail(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 42.5})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[ExpectedValue(cell="D14", expected=42.5, unit="MWh", cell_unit="MW")],
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    row = report_of(service, result).comparisons[0]
    assert row.outcome == "fail"
    assert "unit mismatch" in (row.reason or "")


# --------------------------------------------------------------- the DST alignment


@pytest.mark.asyncio
async def test_a_fall_back_dst_day_aligns_by_wall_clock_not_position(tmp_path: Path) -> None:
    make_dst_workbook(tmp_path / "prices.xlsx")
    # Expectations given deliberately out of order, and including both 02:00 folds.
    spec = ReconciliationSpec(
        workbook="prices.xlsx",
        timezone="Europe/Oslo",
        default_tolerance=Tolerance(abs=0.001),
        time_index=TimeIndexSpec(
            timestamp_column="A",
            value_column="B",
            value_unit="EUR/MWh",
            sheet="Prices",
            expectations=[
                TimeExpectation(timestamp="2024-10-27T05:00:00", expected=5.0, unit="EUR/MWh"),
                TimeExpectation(
                    timestamp="2024-10-27T02:00:00", expected=21.0, unit="EUR/MWh", fold=1
                ),
                TimeExpectation(
                    timestamp="2024-10-27T02:00:00", expected=20.0, unit="EUR/MWh", fold=0
                ),
                TimeExpectation(timestamp="2024-10-27T23:00:00", expected=23.0, unit="EUR/MWh"),
            ],
        ),
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "pass", result.evidence[0].detail
    report = report_of(service, result)
    assert report.total == 4
    # The two 02:00 folds resolved to their *distinct* values, proving the join is
    # not positional.
    by_cell = {c.cell: c for c in report.comparisons}
    assert by_cell["2024-10-27T02:00:00"].actual == pytest.approx(20.0)  # fold 0
    assert by_cell["2024-10-27T02:00:00 (fold 1)"].actual == pytest.approx(21.0)
    assert by_cell["2024-10-27T05:00:00"].actual == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_a_time_row_with_no_matching_wall_clock_is_a_fail(tmp_path: Path) -> None:
    make_dst_workbook(tmp_path / "prices.xlsx")
    spec = ReconciliationSpec(
        workbook="prices.xlsx",
        timezone="Europe/Oslo",
        default_tolerance=Tolerance(abs=0.001),
        time_index=TimeIndexSpec(
            timestamp_column="A",
            value_column="B",
            sheet="Prices",
            expectations=[
                # a third 02:00 (fold 2) does not exist in the workbook
                TimeExpectation(
                    timestamp="2024-10-27T02:00:00", expected=99.0, unit="EUR/MWh", fold=2
                ),
            ],
        ),
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    assert "alignment gap" in (report_of(service, result).comparisons[0].reason or "")


# --------------------------------------------------------------- unreadable / missing


@pytest.mark.asyncio
async def test_an_empty_cell_is_a_fail_with_a_reason(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 42.5})  # D15 left empty
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[
            ExpectedValue(cell="D14", expected=42.5, unit="MWh"),
            ExpectedValue(cell="D15", expected=10.0, unit="MWh"),
        ],
    )
    service, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    rows = {c.cell: c for c in report_of(service, result).comparisons}
    assert rows["D15"].outcome == "fail"
    assert "empty cell" in (rows["D15"].reason or "")
    assert rows["D14"].outcome == "pass"


@pytest.mark.asyncio
async def test_a_workbook_with_no_cached_values_is_blocked_style_fail(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"A1": "header"})  # addressed cells all empty
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[ExpectedValue(cell="D14", expected=42.5, unit="MWh")],
    )
    _, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    assert "open and save" in result.evidence[0].detail.lower()


@pytest.mark.asyncio
async def test_a_missing_workbook_is_an_explicit_fail(tmp_path: Path) -> None:
    spec = ReconciliationSpec(
        workbook="nope.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[ExpectedValue(cell="D14", expected=1.0, unit="MWh")],
    )
    _, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    assert "not found" in result.evidence[0].detail


@pytest.mark.asyncio
async def test_a_workbook_path_escaping_the_workspace_is_refused(tmp_path: Path) -> None:
    spec = ReconciliationSpec(
        workbook="../secret.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[ExpectedValue(cell="D14", expected=1.0, unit="MWh")],
    )
    _, result = await run_recon(tmp_path, spec)
    assert result.risk == "high"
    assert "escapes the workspace" in result.evidence[0].detail


# --------------------------------------------------------------- the payload seam


@pytest.mark.asyncio
async def test_the_full_table_is_stored_as_a_payload_not_inlined(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 42.5, "D15": 10.0})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[
            ExpectedValue(cell="D14", expected=42.5, unit="MWh", label="revenue"),
            ExpectedValue(cell="D15", expected=10.0, unit="MWh"),
        ],
    )
    service, result = await run_recon(tmp_path, spec)
    # One grouped evidence line; the table is behind its payload_ref, never inline.
    assert len(result.evidence) == 1
    report = report_of(service, result)
    assert report.total == 2
    assert {c.cell for c in report.comparisons} == {"D14", "D15"}
    assert report.comparisons[0].label == "revenue" or report.comparisons[1].label == "revenue"


# --------------------------------------------------------------- registration wiring


def test_the_check_is_registered_in_the_production_wiring(tmp_path: Path) -> None:
    """POST /api/validation/run with the check id runs a real reconciliation through
    the app create_app wires up — proof the additive registration landed."""
    make_workbook(tmp_path / "book.xlsx", {"D14": 42.5})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(abs=0.001),
        expectations=[ExpectedValue(cell="D14", expected=42.5, unit="MWh")],
    )
    app = create_app(Settings(workspace_root=tmp_path, fake_agent=True))
    with TestClient(app) as client:
        body = {
            "subject": {"kind": "file", "ref": "book.xlsx", "label": "book.xlsx"},
            "checks": ["reconciliation"],
            "params": spec.model_dump(),
        }
        posted = client.post("/api/validation/run", json=body).json()
    assert posted["risk"] == "pass"  # not "high"/"not registered" — the check ran
    assert posted["evidence"][0]["kind"] == "numeric"
    assert posted["evidence"][0]["payload_ref"] is not None


def test_a_mismatch_through_the_endpoint_is_high(tmp_path: Path) -> None:
    make_workbook(tmp_path / "book.xlsx", {"D14": 99.0})
    spec = ReconciliationSpec(
        workbook="book.xlsx",
        default_tolerance=Tolerance(rel=0.001),
        expectations=[ExpectedValue(cell="D14", expected=42.5, unit="MWh")],
    )
    app = create_app(Settings(workspace_root=tmp_path, fake_agent=True))
    with TestClient(app) as client:
        body = {
            "subject": {"kind": "file", "ref": "book.xlsx", "label": "book.xlsx"},
            "checks": ["reconciliation"],
            "params": spec.model_dump(),
        }
        posted = client.post("/api/validation/run", json=body).json()
    assert posted["risk"] == "high"
