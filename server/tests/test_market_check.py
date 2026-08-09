"""The gate-closure / bid-window gate — M6's market-rules domain check.

Fake-first / no Office and no network: every fixture is a tiny ``.xlsx`` built with
openpyxl or a ``.csv`` written with the stdlib, and every market rule comes from the
server-owned catalog, so all three CI legs prove the whole thing. What is under test
is the *domain* behaviour that has no analogue in a generic file linter:

* a bid at **11:59:59 local** passes and one at **12:00:00 local** fails, for a
  delivery day that is itself a DST-transition Sunday — and a *naive* submission
  timestamp whose local and UTC readings disagree about the verdict is reported as
  that disagreement rather than silently resolved;
* the Europe/Oslo fall-back Sunday has **25 hourly** delivery periods (**100**
  quarter-hourly after the MTU switch) and the spring-forward one **23** (**92**), so
  a file carrying 24/96 is a named failure and never a pass;
* delivery periods off the market time unit are caught **in instants**, so a
  quarter-hourly file against an hourly market fails and a half-hourly GB market
  disagrees with an hourly one;
* a wall clock that never existed is a fail, an undisambiguated repeated hour is a
  warn naming both fixes, and a bogus UTC offset is a fail;
* ``MW`` against a 15-minute market time unit is a warn that names the factor of 4,
  and a NOK price column on a EUR market is the same **principled refusal**
  ``services/reconciliation.py`` makes — never an invented rate, never "unknown unit";
* an artifact the check cannot parse is a stated ``fail``, never a pass;
* the check registers into the production wiring, and one result stays inside a
  stated byte ceiling.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.market import (
    MARKETS,
    RESOLUTION_MINUTES,
    MarketCheckSpec,
    MarketComplianceReport,
    MarketSpec,
    ResolutionPeriod,
    market_ids,
)
from workbench_server.models.validation import ValidationResult, ValidationSpec, ValidationSubject
from workbench_server.services.event_bus import EventBus
from workbench_server.services.market_check import (
    MarketRulesCheck,
    first_real_instant,
    periods_in_local_day,
    split_header,
)
from workbench_server.services.validation import ValidationService

OSLO = ZoneInfo("Europe/Oslo")
LONDON = ZoneInfo("Europe/London")

#: Ceiling on one serialized ``ValidationResult`` from this check. Sized from the
#: measured payloads: a clean 25-hour day is 2,027 bytes over 6 evidence lines, and
#: the chattiest failure — 40 delivery days each short a period, capped at 20 named
#: lines plus a roll-up — is 6,866 over 25. This leaves ~1.7x headroom on the worst
#: case for wording changes. The violation *table* does not ride the result at all;
#: it sits behind ``payload_ref``, which is what keeps this bound flat as files grow.
#: A budget that lives outside the quality gate does not bind, so it lives here.
MAX_RESULT_BYTES = 12_000

# --------------------------------------------------------------------------- fixtures


def write_xlsx(path: Path, headers: list[str], rows: list[tuple[Any, ...]]) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Bid"
    sheet.append(headers)
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)


def write_csv(path: Path, headers: list[str], rows: list[tuple[Any, ...]]) -> None:
    lines = [",".join(headers)]
    lines += [",".join("" if cell is None else str(cell) for cell in row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def oslo_fallback_hours() -> list[datetime]:
    """The 25 local wall clocks of Europe/Oslo's 2024-10-27, in delivery order.

    Written out rather than derived, so the fixture is independent evidence: 02:00
    appears twice (the CEST pass, then the CET pass an hour later)."""
    rows = [datetime(2024, 10, 27, 0), datetime(2024, 10, 27, 1)]
    rows += [datetime(2024, 10, 27, 2), datetime(2024, 10, 27, 2)]
    rows += [datetime(2024, 10, 27, hour) for hour in range(3, 24)]
    return rows


def oslo_spring_hours() -> list[datetime]:
    """The 23 local wall clocks of Europe/Oslo's 2024-03-31 — 02:00 never happens."""
    hours = [*range(0, 2), *range(3, 24)]
    return [datetime(2024, 3, 31, hour) for hour in hours]


def oslo_fallback_quarters() -> list[datetime]:
    """The 100 quarter-hourly wall clocks of Europe/Oslo's 2025-10-26 — after the
    market time unit switched, so the fall-back day is 100 periods, not 96."""
    quarters = (0, 15, 30, 45)
    rows = [datetime(2025, 10, 26, hour, minute) for hour in (0, 1) for minute in quarters]
    for _ in range(2):  # the two passes of the repeated 02:00 hour
        rows += [datetime(2025, 10, 26, 2, minute) for minute in quarters]
    rows += [datetime(2025, 10, 26, hour, minute) for hour in range(3, 24) for minute in quarters]
    return rows


def oslo_spring_quarters() -> list[datetime]:
    """The 92 quarter-hourly wall clocks of Europe/Oslo's 2026-03-29."""
    quarters = (0, 15, 30, 45)
    hours = [*range(0, 2), *range(3, 24)]
    return [datetime(2026, 3, 29, hour, minute) for hour in hours for minute in quarters]


def ordinary_hours(day: date) -> list[datetime]:
    return [datetime.combine(day, time(hour, 0)) for hour in range(24)]


def bid_rows(stamps: list[datetime], volume: float = 10.0) -> list[tuple[Any, ...]]:
    return [(stamp, volume, 40.0) for stamp in stamps]


async def run_check(
    root: Path, spec: MarketCheckSpec
) -> tuple[ValidationService, ValidationResult]:
    """Register the real check on a service and run it end-to-end."""
    service = ValidationService(root, EventBus())
    service.register(MarketRulesCheck())
    subject = ValidationSubject(kind="file", ref=spec.artifact, label=spec.artifact)
    result = await service.run(
        ValidationSpec(subject=subject, checks=["market_rules"], params=spec.model_dump())
    )
    return service, result


def line(result: ValidationResult, needle: str) -> Any:
    """The one evidence line whose label contains ``needle``."""
    hits = [item for item in result.evidence if needle in item.label]
    assert len(hits) == 1, [item.label for item in result.evidence]
    return hits[0]


def report_of(service: ValidationService, result: ValidationResult) -> MarketComplianceReport:
    ref = line(result, "delivery-period grid").payload_ref
    assert ref is not None
    payload = service.payload("artifact", ref)
    assert isinstance(payload, MarketComplianceReport)
    return payload


def hourly_spec(**overrides: Any) -> MarketCheckSpec:
    base: dict[str, Any] = {
        "artifact": "bid.xlsx",
        "market": "nordpool-day-ahead",
        "timestamp_column": "delivery_start",
        "volume_column": "volume",
    }
    base.update(overrides)
    return MarketCheckSpec.model_validate(base)


# --------------------------------------------------------------- the zone arithmetic


def test_a_local_day_is_not_always_24_hours() -> None:
    """The whole check rests on this. If it is wrong, everything above it is."""
    assert periods_in_local_day(date(2024, 10, 27), OSLO, 60) == 25
    assert periods_in_local_day(date(2024, 3, 31), OSLO, 60) == 23
    assert periods_in_local_day(date(2024, 6, 15), OSLO, 60) == 24
    # …and the quarter-hourly market time unit multiplies each of them by four.
    assert periods_in_local_day(date(2025, 10, 26), OSLO, 15) == 100
    assert periods_in_local_day(date(2026, 3, 29), OSLO, 15) == 92
    assert periods_in_local_day(date(2025, 6, 15), OSLO, 15) == 96
    # A different zone transitions at a different *wall clock*, same instant.
    assert periods_in_local_day(date(2024, 10, 27), LONDON, 30) == 50
    assert periods_in_local_day(date(2024, 6, 15), LONDON, 30) == 48


def test_gate_closure_is_a_local_wall_clock_not_a_fixed_utc_offset() -> None:
    """12:00 in Oslo is 11:00Z in summer and 10:00Z in winter. Code that stores one
    of those two numbers is wrong for half the year."""
    summer = first_real_instant(datetime(2024, 6, 14, 12, 0), OSLO)
    winter = first_real_instant(datetime(2024, 12, 13, 12, 0), OSLO)
    assert summer.hour == 10  # CEST, UTC+2
    assert winter.hour == 11  # CET, UTC+1


def test_split_header_reads_the_unit_an_analyst_actually_types() -> None:
    assert split_header("volume_mw") == ("volume", "mw")
    assert split_header("Volume (MWh)") == ("Volume", "MWh")
    assert split_header("price_eur_mwh") == ("price", "eur/mwh")
    assert split_header("Price [NOK/kWh]") == ("Price", "NOK/kWh")
    assert split_header("delivery_start") == ("delivery_start", None)


# --------------------------------------------------------------------- the catalog


def test_the_catalog_is_a_catalog_not_one_market_hard_coded() -> None:
    """Two entries that disagree in every field that matters — which is the only way
    to know the rules are read rather than assumed."""
    nordic = MARKETS["nordpool-day-ahead"]
    gb = MARKETS["gb-day-ahead"]
    assert nordic.timezone != gb.timezone
    assert nordic.day_ahead_gate_closure != gb.day_ahead_gate_closure
    assert nordic.currency != gb.currency
    assert nordic.resolution_for(date(2024, 6, 15)) != gb.resolution_for(date(2024, 6, 15))
    assert market_ids() == sorted(MARKETS)


def test_resolution_is_a_schedule_so_the_mtu_switch_is_not_a_lie() -> None:
    nordic = MARKETS["nordpool-day-ahead"]
    before = nordic.resolution_for(date(2025, 9, 30))
    after = nordic.resolution_for(date(2025, 10, 1))
    assert before is not None and before.resolution == "PT60M"
    assert after is not None and after.resolution == "PT15M"
    # …and a date the catalog does not vouch for is refused, not guessed.
    assert nordic.resolution_for(date(1999, 12, 31)) is None


def test_a_market_spec_refuses_incoherent_rules() -> None:
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        MarketSpec(
            id="x",
            label="x",
            timezone="Europe/Nowhere",
            currency="EUR",
            day_ahead_gate_closure=time(12, 0),
            intraday_lead_minutes=60,
            resolutions=[ResolutionPeriod(effective_from=date(2020, 1, 1), resolution="PT60M")],
            source="test",
        )
    with pytest.raises(ValueError, match="oldest-first"):
        MarketSpec(
            id="x",
            label="x",
            timezone="Europe/Oslo",
            currency="EUR",
            day_ahead_gate_closure=time(12, 0),
            intraday_lead_minutes=60,
            resolutions=[
                ResolutionPeriod(effective_from=date(2025, 1, 1), resolution="PT15M"),
                ResolutionPeriod(effective_from=date(2020, 1, 1), resolution="PT60M"),
            ],
            source="test",
        )


@pytest.mark.asyncio
async def test_an_unknown_market_lists_the_catalog(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec(market="epex-mars"))
    assert result.risk == "high"
    assert len(result.evidence) == 1
    detail = result.evidence[0].detail
    assert "epex-mars" in detail
    for market_id in market_ids():  # AXI shape 3: the fix, in the same round trip
        assert market_id in detail


# ------------------------------------------------------------------- gate closure


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("2024-10-26T11:59:59+02:00", "pass"),  # one second before the auction closes
        ("2024-10-26T12:00:00+02:00", "fail"),  # *at* gate closure is already late
        ("2024-10-26T12:00:01+02:00", "fail"),
        ("2024-10-26T09:00:00Z", "pass"),  # 11:00 in Oslo — an hour early
        ("2024-10-26T10:00:00Z", "fail"),  # 12:00 in Oslo — the same instant as above
    ],
)
@pytest.mark.asyncio
async def test_gate_closure_on_a_dst_delivery_day(
    tmp_path: Path, submitted: str, expected: str
) -> None:
    """The headline, on the hardest delivery day of the year: the Sunday the clocks
    go back. Gate closure is 12:00 *in Oslo* on the Saturday, which is 10:00Z."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh", "price_eur_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    _, result = await run_check(tmp_path, hourly_spec(submitted_at=submitted))
    gate = line(result, "gate closure")
    assert gate.outcome == expected, gate.detail
    assert "12:00" in gate.detail


@pytest.mark.asyncio
async def test_a_naive_submission_whose_two_readings_disagree_is_reported(
    tmp_path: Path,
) -> None:
    """The bug this check exists to catch, made visible instead of resolved.

    11:30 with no offset is 09:30Z read as Oslo local (before the 10:00Z gate) and
    11:30Z read as UTC (after it). A gate that silently picked one would either bless
    a late bid or reject an early one — so it picks neither and says so."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    service, result = await run_check(tmp_path, hourly_spec(submitted_at="2024-10-26T11:30:00"))
    gate = line(result, "gate closure")
    assert gate.outcome == "warn"
    assert "no UTC offset" in gate.detail
    assert "+02:00" in gate.detail  # the fix is named
    finding = report_of(service, result).gate_closure
    assert finding.naive_input and finding.readings_disagree


@pytest.mark.asyncio
async def test_a_naive_submission_whose_readings_agree_is_a_plain_verdict(
    tmp_path: Path,
) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    service, result = await run_check(tmp_path, hourly_spec(submitted_at="2024-10-26T09:00:00"))
    gate = line(result, "gate closure")
    assert gate.outcome == "pass"
    assert "no UTC offset" in gate.detail  # still says how it was read
    assert not report_of(service, result).gate_closure.readings_disagree


@pytest.mark.asyncio
async def test_no_submission_timestamp_is_an_explicit_skip(tmp_path: Path) -> None:
    """AXI shape 2: "not evaluated" is stated, with the argument that would evaluate
    it — never blankness a reader has to interpret."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec())
    gate = line(result, "gate closure")
    assert gate.outcome == "skipped"
    assert "submitted_at" in gate.detail
    assert result.risk == "low"  # a skip is not a pass


@pytest.mark.asyncio
async def test_an_unparseable_submission_timestamp_is_a_fail(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec(submitted_at="yesterday lunchtime"))
    assert line(result, "gate closure").outcome == "fail"


@pytest.mark.asyncio
async def test_intraday_uses_a_rolling_per_period_lead_time(tmp_path: Path) -> None:
    """A different product, a different deadline, from the same catalog row."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    # 2024-06-15T00:00 Oslo is 2024-06-14T22:00Z; a 60-minute lead closes it at 21:00Z.
    _, early = await run_check(
        tmp_path, hourly_spec(product="intraday", submitted_at="2024-06-14T20:59:00Z")
    )
    assert line(early, "intraday gate closure").outcome == "pass"
    service, late = await run_check(
        tmp_path, hourly_spec(product="intraday", submitted_at="2024-06-14T21:00:00Z")
    )
    assert line(late, "intraday gate closure").outcome == "fail"
    closed = [
        violation
        for violation in report_of(service, late).violations
        if violation.kind == "after_intraday_gate"
    ]
    assert closed, "the closed delivery periods are named, not just counted"


# --------------------------------------------------------------- the DST period count


@pytest.mark.asyncio
async def test_the_fall_back_day_validates_at_25_hours(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh", "price_eur_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    service, result = await run_check(
        tmp_path, hourly_spec(submitted_at="2024-10-26T11:00:00+02:00")
    )
    assert line(result, "delivery-period grid").outcome == "pass"
    assert line(result, "delivery-day period count").outcome == "pass"
    day = report_of(service, result).days[0]
    assert (day.expected, day.observed) == (25, 25)
    assert "fall-back" in day.dst_transition


@pytest.mark.asyncio
async def test_a_fall_back_day_with_24_rows_does_not_validate(tmp_path: Path) -> None:
    """The silent failure this check exists to refuse: a schedule that assumed the
    day was 24 hours long and is therefore short by exactly the repeated hour."""
    stamps = [stamp for stamp in oslo_fallback_hours()]
    del stamps[3]  # drop the second 02:00 → an ordinary-looking 24-row day
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], bid_rows(stamps))
    service, result = await run_check(tmp_path, hourly_spec())
    count = line(result, "delivery-day period count")
    assert count.outcome == "fail"
    assert "25" in count.detail and "24" in count.detail
    assert "fall-back" in count.detail
    assert result.risk == "high"
    report = report_of(service, result)
    assert [violation.kind for violation in report.violations] == ["missing"]
    assert "02:00" in report.violations[0].period


@pytest.mark.asyncio
async def test_the_spring_forward_day_validates_at_23_hours(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(oslo_spring_hours()),
    )
    service, result = await run_check(tmp_path, hourly_spec())
    assert line(result, "delivery-day period count").outcome == "pass"
    assert report_of(service, result).days[0].expected == 23


@pytest.mark.asyncio
async def test_a_delivery_period_that_never_existed_is_a_fail(tmp_path: Path) -> None:
    """02:30 on the spring-forward Sunday is not a late bid — it is not a moment."""
    stamps = [*oslo_spring_hours(), datetime(2024, 3, 31, 2, 0)]
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], bid_rows(stamps))
    service, result = await run_check(tmp_path, hourly_spec())
    assert line(result, "delivery-period grid").outcome == "fail"
    kinds = {violation.kind for violation in report_of(service, result).violations}
    assert "nonexistent" in kinds


@pytest.mark.asyncio
async def test_the_quarter_hourly_fall_back_day_is_100_periods_not_96(
    tmp_path: Path,
) -> None:
    """After the market time unit switch the same Sunday is 100 periods. A file with
    96 is the identical bug one resolution finer."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(oslo_fallback_quarters()),
    )
    service, good = await run_check(tmp_path, hourly_spec())
    assert line(good, "delivery-day period count").outcome == "pass"
    day = report_of(service, good).days[0]
    assert (day.resolution, day.expected, day.observed) == ("PT15M", 100, 100)

    short = [stamp for stamp in oslo_fallback_quarters()]
    del short[8:12]  # the whole repeated 02:00 hour → an ordinary-looking 96
    write_xlsx(tmp_path / "short.xlsx", ["delivery_start", "volume_mwh"], bid_rows(short))
    _, result = await run_check(tmp_path, hourly_spec(artifact="short.xlsx"))
    count = line(result, "delivery-day period count")
    assert count.outcome == "fail"
    assert "100" in count.detail and "96" in count.detail


@pytest.mark.asyncio
async def test_the_quarter_hourly_spring_day_is_92_periods(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(oslo_spring_quarters()),
    )
    service, result = await run_check(tmp_path, hourly_spec())
    assert line(result, "delivery-day period count").outcome == "pass"
    assert report_of(service, result).days[0].expected == 92


# ------------------------------------------------------------------ the period grid


@pytest.mark.asyncio
async def test_a_quarter_hourly_file_against_an_hourly_market_is_misaligned(
    tmp_path: Path,
) -> None:
    """The same file is correct after the MTU switch and wrong before it — which is
    the point of a resolution *schedule*."""
    quarters = [
        datetime(2024, 6, 15, hour, minute) for hour in range(24) for minute in (0, 15, 30, 45)
    ]
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], bid_rows(quarters))
    service, result = await run_check(tmp_path, hourly_spec())
    grid = line(result, "delivery-period grid")
    assert grid.outcome == "fail"
    assert "PT60M" in grid.detail
    misaligned = [v for v in report_of(service, result).violations if v.kind == "misaligned"]
    assert len(misaligned) == 72  # three of every four quarter-hours are off the grid


@pytest.mark.asyncio
async def test_the_gb_market_settles_half_hourly(tmp_path: Path) -> None:
    """The second catalog entry earning its place: an hourly file is *aligned* to a
    half-hourly grid but only half of it, so the failure is 24 missing periods —
    a different verdict than the Nordic market would give the same file."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh", "price_gbp_mwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    service, result = await run_check(
        tmp_path,
        hourly_spec(market="gb-day-ahead", price_column="price", submitted_at="2024-06-14T09:00Z"),
    )
    report = report_of(service, result)
    assert (report.days[0].resolution, report.days[0].expected) == ("PT30M", 48)
    assert len([v for v in report.violations if v.kind == "missing"]) == 24
    # 09:00Z is 10:00 in London — after the 09:20 local gate closure.
    assert line(result, "gate closure").outcome == "fail"
    assert line(result, "price unit").outcome == "pass"  # GBP is what GB clears in


@pytest.mark.asyncio
async def test_a_duplicated_row_on_an_ordinary_day_is_not_a_dst_fold(
    tmp_path: Path,
) -> None:
    """June has no transition in Europe/Oslo, so a second 02:00 is a paste accident.
    Reading it as a fold would bless corrupt input."""
    stamps = [*ordinary_hours(date(2024, 6, 15)), datetime(2024, 6, 15, 2, 0)]
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], bid_rows(stamps))
    service, result = await run_check(tmp_path, hourly_spec())
    duplicates = [v for v in report_of(service, result).violations if v.kind == "duplicate"]
    assert len(duplicates) == 1
    assert "not an ambiguous local time" in duplicates[0].detail


@pytest.mark.asyncio
async def test_a_bogus_utc_offset_is_a_fail(tmp_path: Path) -> None:
    """+03:00 is not an offset Europe/Oslo has ever had in June. The timestamp names
    a different moment than it appears to."""
    rows = [(f"2024-06-15T{hour:02d}:00:00+02:00", 10.0) for hour in range(24)]
    rows[5] = ("2024-06-15T05:00:00+03:00", 10.0)
    write_csv(tmp_path / "bid.csv", ["delivery_start", "volume_mwh"], rows)
    service, result = await run_check(tmp_path, hourly_spec(artifact="bid.csv"))
    kinds = [v.kind for v in report_of(service, result).violations]
    assert "invalid_offset" in kinds
    assert line(result, "delivery-period grid").outcome == "fail"


# -------------------------------------------------------------- local-time ambiguity


@pytest.mark.asyncio
async def test_undisambiguated_local_times_on_a_fall_back_day_are_a_warn(
    tmp_path: Path,
) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    _, result = await run_check(tmp_path, hourly_spec())
    ambiguity = line(result, "local-time disambiguation")
    assert ambiguity.outcome == "warn"
    assert "order of appearance" in ambiguity.detail
    assert "fold" in ambiguity.detail  # both fixes named


@pytest.mark.asyncio
async def test_an_explicit_fold_column_resolves_the_repeated_hour(tmp_path: Path) -> None:
    stamps = oslo_fallback_hours()
    folds = [0] * len(stamps)
    folds[3] = 1  # the second 02:00 says so itself
    rows = [(stamp, 10.0, fold) for stamp, fold in zip(stamps, folds, strict=True)]
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh", "fold"], rows)
    _, result = await run_check(tmp_path, hourly_spec(fold_column="fold"))
    assert line(result, "local-time disambiguation").outcome == "pass"
    assert line(result, "delivery-day period count").outcome == "pass"


@pytest.mark.asyncio
async def test_timestamps_with_offsets_need_no_disambiguation(tmp_path: Path) -> None:
    """The shape a bid file ought to have. Both 02:00s are written out, each with the
    offset that says which one it is, and nothing is inferred from row order."""
    stamps = ["2024-10-27T00:00:00+02:00", "2024-10-27T01:00:00+02:00"]
    stamps += ["2024-10-27T02:00:00+02:00", "2024-10-27T02:00:00+01:00"]
    stamps += [f"2024-10-27T{hour:02d}:00:00+01:00" for hour in range(3, 24)]
    write_csv(
        tmp_path / "bid.csv",
        ["delivery_start", "volume_mwh"],
        [(stamp, 10.0) for stamp in stamps],
    )
    service, result = await run_check(tmp_path, hourly_spec(artifact="bid.csv"))
    assert line(result, "local-time disambiguation").outcome == "pass"
    assert line(result, "delivery-period grid").outcome == "pass"
    assert report_of(service, result).days[0].observed == 25
    assert result.risk == "low"  # only the un-evaluated gate closure keeps it off pass


@pytest.mark.asyncio
async def test_fold_1_where_the_zone_repeats_nothing_is_a_fail(tmp_path: Path) -> None:
    stamps = ordinary_hours(date(2024, 6, 15))
    folds = [0] * len(stamps)
    folds[2] = 1
    rows = [(stamp, 10.0, fold) for stamp, fold in zip(stamps, folds, strict=True)]
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh", "fold"], rows)
    service, result = await run_check(tmp_path, hourly_spec(fold_column="fold"))
    ambiguous = [v for v in report_of(service, result).violations if v.kind == "ambiguous"]
    assert len(ambiguous) == 1
    assert "occurs once" in ambiguous[0].detail


# ------------------------------------------------------------------------ unit sanity


@pytest.mark.asyncio
async def test_mw_over_an_hourly_mtu_is_fine_and_says_why(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mw"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec())
    volume = line(result, "volume unit")
    assert volume.outcome == "pass"
    assert "60-minute" in volume.detail


@pytest.mark.asyncio
async def test_mw_against_a_quarter_hourly_mtu_names_the_factor_of_four(
    tmp_path: Path,
) -> None:
    """The silent x4 the MTU switch introduced, caught from the header and the
    catalog alone — no knowledge of the numbers required."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mw"],
        bid_rows(oslo_fallback_quarters()),
    )
    _, result = await run_check(tmp_path, hourly_spec())
    volume = line(result, "volume unit")
    assert volume.outcome == "warn"
    assert "4x" in volume.detail


@pytest.mark.asyncio
async def test_a_volume_column_with_no_unit_is_a_named_warn(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec())
    volume = line(result, "volume unit")
    assert volume.outcome == "warn"
    assert "volume_unit" in volume.detail  # the fix, named


@pytest.mark.asyncio
async def test_kwh_is_a_named_conversion_not_a_failure(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_kwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec())
    volume = line(result, "volume unit")
    assert volume.outcome == "pass"
    assert "0.001" in volume.detail


@pytest.mark.asyncio
async def test_a_price_in_another_currency_is_refused_not_converted(
    tmp_path: Path,
) -> None:
    """#102's precedent, applied to market rules: NOK is a *known* unit, so the answer
    is a refusal that names the fix — never "unknown unit", and never an FX rate
    nobody chose."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh", "price_nok_mwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec(price_column="price"))
    price = line(result, "price unit")
    assert price.outcome == "fail"
    assert "NOK" in price.detail and "EUR" in price.detail
    assert "No FX rate will be invented" in price.detail
    assert "unknown unit" not in price.detail.lower()


@pytest.mark.asyncio
async def test_a_price_in_the_market_currency_passes(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh", "price_eur_mwh"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec(price_column="price"))
    assert line(result, "price unit").outcome == "pass"


@pytest.mark.asyncio
async def test_a_declared_unit_overrides_the_header(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume"],
        bid_rows(ordinary_hours(date(2024, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec(volume_unit="MWh"))
    assert line(result, "volume unit").outcome == "pass"


# ------------------------------------------------------------- unreadable artifacts


@pytest.mark.parametrize(
    ("setup", "spec_kwargs", "needle"),
    [
        ("none", {"artifact": "missing.xlsx"}, "not found"),
        ("none", {"artifact": "../escape.xlsx"}, "escapes the workspace"),
        ("text", {"artifact": "bid.txt"}, "unsupported artifact type"),
        ("headers_only", {}, "no data rows"),
        ("good", {"timestamp_column": "when"}, "no column named"),
        ("good", {"market": "nordpool-day-ahead", "delivery_date": "not-a-date"}, "ISO date"),
    ],
)
@pytest.mark.asyncio
async def test_an_artifact_the_check_cannot_read_is_a_stated_fail(
    tmp_path: Path, setup: str, spec_kwargs: dict[str, Any], needle: str
) -> None:
    """Never a pass, never blank: one ``fail`` line that names the reason and, where
    there is one, the fix."""
    if setup == "text":
        (tmp_path / "bid.txt").write_text("not a bid", encoding="utf-8")
    elif setup == "headers_only":
        write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], [])
    elif setup == "good":
        write_xlsx(
            tmp_path / "bid.xlsx",
            ["delivery_start", "volume_mwh"],
            bid_rows(ordinary_hours(date(2024, 6, 15))),
        )
    _, result = await run_check(tmp_path, hourly_spec(**spec_kwargs))
    assert result.risk == "high"
    assert len(result.evidence) == 1
    assert result.evidence[0].outcome == "fail"
    assert needle in result.evidence[0].detail


@pytest.mark.asyncio
async def test_a_delivery_date_outside_the_catalog_is_refused(tmp_path: Path) -> None:
    """The catalog says what it vouches for. Guessing the oldest rule would silently
    validate a 1999 file against 2025's market time unit."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(ordinary_hours(date(1999, 6, 15))),
    )
    _, result = await run_check(tmp_path, hourly_spec())
    assert result.evidence[0].outcome == "fail"
    assert "does not cover delivery date" in result.evidence[0].detail


@pytest.mark.asyncio
async def test_a_declared_delivery_date_catches_a_file_that_spans_days(
    tmp_path: Path,
) -> None:
    stamps = ordinary_hours(date(2024, 6, 15)) + ordinary_hours(date(2024, 6, 16))
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], bid_rows(stamps))
    service, result = await run_check(tmp_path, hourly_spec(delivery_date="2024-06-15"))
    details = [v.detail for v in report_of(service, result).violations]
    assert any("2024-06-16" in detail for detail in details)


# ----------------------------------------------------------------------- the report


@pytest.mark.asyncio
async def test_the_violation_table_never_rides_the_result(tmp_path: Path) -> None:
    """Five days of misaligned quarter-hours is hundreds of violations. They live
    behind ``payload_ref``; the evidence carries only the headline and the count."""
    quarters = [
        datetime(2024, 6, day, hour, minute)
        for day in range(1, 6)
        for hour in range(24)
        for minute in (0, 15, 30, 45)
    ]
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], bid_rows(quarters))
    service, result = await run_check(tmp_path, hourly_spec())
    grid = line(result, "delivery-period grid")
    assert grid.payload_ref is not None
    report = report_of(service, result)
    assert report.truncated is not None
    assert (report.truncated.shown, report.truncated.total > 200) == (200, True)
    assert "delivery day per run" in report.truncated.detail  # how to see the rest
    # The table is genuinely absent from the wire result: a violation past the
    # headline appears in the payload and nowhere in the ValidationResult.
    serialized = result.model_dump_json()
    assert report.violations[50].period not in serialized
    assert len(serialized.encode()) < len(json.dumps(report.model_dump(mode="json")).encode())


@pytest.mark.asyncio
async def test_one_result_stays_inside_its_byte_ceiling(tmp_path: Path) -> None:
    """Agent-facing results are judged on token cost, and a budget outside the
    quality gate does not bind — so it is asserted here, on both a clean run and the
    chattiest failing one."""
    write_xlsx(
        tmp_path / "clean.xlsx",
        ["delivery_start", "volume_mwh", "price_eur_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    _, clean = await run_check(
        tmp_path,
        hourly_spec(
            artifact="clean.xlsx", price_column="price", submitted_at="2024-10-26T11:00:00+02:00"
        ),
    )
    assert len(clean.model_dump_json().encode()) < MAX_RESULT_BYTES

    # Every delivery day short by one period → one named line per day, capped.
    stamps: list[datetime] = []
    for offset in range(40):
        day = date(2024, 6, 1) + timedelta(days=offset)
        stamps += [datetime.combine(day, time(hour, 0)) for hour in range(23)]
    write_xlsx(tmp_path / "noisy.xlsx", ["delivery_start", "volume"], bid_rows(stamps))
    _, noisy = await run_check(tmp_path, hourly_spec(artifact="noisy.xlsx"))
    assert noisy.risk == "high"
    assert len(noisy.model_dump_json().encode()) < MAX_RESULT_BYTES
    rollup = [item for item in noisy.evidence if "more (" in item.label]
    assert rollup and "20" in rollup[0].detail  # AXI shape 1: how many were withheld


@pytest.mark.asyncio
async def test_csv_and_xlsx_reach_the_same_verdict(tmp_path: Path) -> None:
    """One spec, two formats, no divergence — the reader is a seam, not two checks."""
    stamps = oslo_fallback_hours()
    write_xlsx(tmp_path / "bid.xlsx", ["delivery_start", "volume_mwh"], bid_rows(stamps))
    write_csv(
        tmp_path / "bid.csv",
        ["delivery_start", "volume_mwh"],
        [(stamp.isoformat(), 10.0) for stamp in stamps],
    )
    _, from_xlsx = await run_check(tmp_path, hourly_spec())
    _, from_csv = await run_check(tmp_path, hourly_spec(artifact="bid.csv"))
    assert [item.outcome for item in from_xlsx.evidence] == [
        item.outcome for item in from_csv.evidence
    ]
    assert from_xlsx.risk == from_csv.risk


# --------------------------------------------------------------- registration wiring


def test_the_check_is_registered_in_the_production_wiring(tmp_path: Path) -> None:
    """POST /api/validation/run with the check id runs a real market-rules check
    through the app ``create_app`` wires up — proof the additive registration landed."""
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh", "price_eur_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    spec = hourly_spec(price_column="price", submitted_at="2024-10-26T11:59:59+02:00")
    app = create_app(Settings(workspace_root=tmp_path, fake_agent=True))
    with TestClient(app) as client:
        posted = client.post(
            "/api/validation/run",
            json={
                "subject": {"kind": "file", "ref": "bid.xlsx", "label": "bid.xlsx"},
                "checks": ["market_rules"],
                "params": spec.model_dump(),
            },
        ).json()
    labels = [item["label"] for item in posted["evidence"]]
    assert any("gate closure" in label for label in labels)
    assert not any("not registered" in item["detail"] for item in posted["evidence"])
    # The naive-timestamp warn is absent (this submission carries an offset); the
    # only non-pass is the undisambiguated fall-back hour.
    assert posted["risk"] == "medium"


def test_a_late_bid_through_the_endpoint_is_high(tmp_path: Path) -> None:
    write_xlsx(
        tmp_path / "bid.xlsx",
        ["delivery_start", "volume_mwh"],
        bid_rows(oslo_fallback_hours()),
    )
    spec = hourly_spec(submitted_at="2024-10-26T12:00:00+02:00")
    app = create_app(Settings(workspace_root=tmp_path, fake_agent=True))
    with TestClient(app) as client:
        posted = client.post(
            "/api/validation/run",
            json={
                "subject": {"kind": "file", "ref": "bid.xlsx", "label": "bid.xlsx"},
                "checks": ["market_rules"],
                "params": spec.model_dump(),
            },
        ).json()
    assert posted["risk"] == "high"


def test_resolution_minutes_covers_every_resolution_the_catalog_uses() -> None:
    for market in MARKETS.values():
        for row in market.resolutions:
            assert row.resolution in RESOLUTION_MINUTES
