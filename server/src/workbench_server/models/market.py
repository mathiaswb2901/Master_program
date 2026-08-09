"""The market-rules catalog, and the typed I/O of the gate-closure/bid-window check.

Reconciliation (``models/reconciliation.py``) answers *do the numbers agree*. This
answers the question that comes before it for anything that will actually be
submitted to a power exchange — **is this artifact a legal bid at all?** A schedule
whose numbers reconcile perfectly is still worthless if it was stamped one second
after gate closure, if its delivery periods are on the wrong grid, or if it carries
24 hourly rows for a day that has 25 of them.

Three properties are in the schema rather than in a comment, because each is a
promise a later edit could quietly break:

* **The rules are server-owned data, selected by id.** A :class:`MarketSpec` is
  picked out of :data:`MARKETS` by :attr:`MarketCheckSpec.market`; the artifact
  under test never states its own gate closure. This is the ``GateCommand``
  catalog posture (``docs/plan/staged-review.md``) and it exists for the same
  reason: a rule the *subject* supplies is a rule the subject can define away, and
  "this bid says its own deadline had not passed" is not evidence of anything.
  There is deliberately no per-workspace ``markets.json`` — opening a folder must
  not be able to change what a gate means.
* **A market's resolution is a schedule, not a scalar.** The Nordic and Central
  European day-ahead auctions moved from a 60-minute to a 15-minute market time
  unit; a file for a delivery date before the switch is *correctly* hourly and one
  after it is *correctly* quarter-hourly. A single ``settlement_resolution`` field
  would make the catalog wrong for half of any real archive, so
  :attr:`MarketSpec.resolutions` carries ``(effective_from, resolution)`` rows and
  :meth:`MarketSpec.resolution_for` picks by delivery date. A delivery date older
  than the first row is **refused**, never assumed (:meth:`resolution_for` returns
  ``None``) — the catalog says what it vouches for and no more.
* **A settlement resolution divides an hour.** ``PT15M``/``PT30M``/``PT60M`` only.
  That is what every market in the catalog uses, and it is also what makes the
  alignment test sound as written (see ``services/market_check.py``).

None of these types is a REST/WS body of its own — the same posture as
``models/reconciliation.py``. A :class:`MarketCheckSpec` travels *inside*
``ValidationSpec.params`` (a ``dict`` on the wire) and a
:class:`MarketComplianceReport` is a bounded detail payload behind an
``EvidenceItem.payload_ref``. The Review panel renders ``EvidenceItem`` generically,
so nothing here needs mirroring into ``ui/src/types.ts``.
"""

from __future__ import annotations

from datetime import date, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator

from workbench_server.models.validation import CheckOutcome, EvidenceTruncation

#: The market time unit a delivery period must land on. ISO-8601 durations, and
#: deliberately only the three that **divide an hour**: every catalog entry uses
#: one of them, and a resolution that did not divide an hour would make "aligned
#: to the grid" depend on where the day started rather than on the clock.
SettlementResolution = Literal["PT15M", "PT30M", "PT60M"]

#: The one place a resolution becomes minutes. Keyed by the literal so a new
#: member of :data:`SettlementResolution` cannot be added without a duration.
RESOLUTION_MINUTES: dict[SettlementResolution, int] = {"PT15M": 15, "PT30M": 30, "PT60M": 60}

#: Which product's deadline binds. ``day_ahead`` is one auction gate closure for
#: the whole delivery day; ``intraday`` is a rolling per-period deadline
#: (:attr:`MarketSpec.intraday_lead_minutes` before each period starts).
MarketProduct = Literal["day_ahead", "intraday"]

#: What went wrong with one delivery period. Each is a distinct fix, which is why
#: they are a closed set rather than free text: ``misaligned`` is "move it onto the
#: grid", ``missing``/``duplicate`` are "the day's row set is wrong", ``nonexistent``
#: and ``invalid_offset`` are "this instant never happened", ``unparseable`` is "the
#: cell is not a timestamp", and ``after_intraday_gate`` is a closed market.
ViolationKind = Literal[
    "misaligned",
    "missing",
    "duplicate",
    "nonexistent",
    "ambiguous",
    "invalid_offset",
    "unparseable",
    "after_intraday_gate",
]


class ResolutionPeriod(BaseModel):
    """One row of a market's resolution schedule: the resolution in force from a
    delivery date onwards, until the next row supersedes it."""

    #: First **local delivery date** this resolution applies to.
    effective_from: date
    resolution: SettlementResolution
    #: Why this row exists, quoted into the evidence when it is the one that
    #: decided the verdict — an analyst reading "expected 96 periods" deserves to
    #: know it was the MTU switch and not a typo.
    note: str = ""


class MarketSpec(BaseModel):
    """One market's rules: the whole of what the check is allowed to know.

    Times are **local wall-clock in** :attr:`timezone`. That is not a convenience —
    it is the single fact this check exists to defend. Gate closure for the Nordic
    day-ahead auction is 12:00 in Oslo, which is 10:00 UTC in winter and 11:00 UTC
    in summer; code that stores "12:00 UTC" is wrong for half the year and wrong in
    the direction that submits late.
    """

    #: Catalog handle, named by :attr:`MarketCheckSpec.market`. A stable contract:
    #: a saved spec references it.
    id: str
    #: What the evidence calls it, e.g. "Nord Pool day-ahead (Nordic/Baltic)".
    label: str
    #: IANA zone name, e.g. ``Europe/Oslo``. Never a fixed UTC offset.
    timezone: str
    #: The currency the market's prices are denominated in, upper-case ISO-4217
    #: (``EUR``, ``GBP``). A price column in any other currency is a **refusal**,
    #: not a conversion — see ``services/market_check.py``.
    currency: str
    #: Day-ahead auction gate closure, as **local wall-clock time** in
    #: :attr:`timezone` on the day :attr:`day_ahead_gate_closure_days_before`
    #: before delivery.
    day_ahead_gate_closure: time
    #: How many days before the delivery day the auction closes. 1 for every
    #: day-ahead auction in the catalog; a field rather than a constant because a
    #: two-day-ahead product is the same shape.
    day_ahead_gate_closure_days_before: int = 1
    #: Intraday trading closes this many minutes before each delivery period
    #: *starts*. Used only for :data:`MarketProduct` ``intraday``.
    intraday_lead_minutes: int
    #: Ordered oldest-first, unique dates. See the module docstring: a scalar here
    #: would be wrong for every archive that spans an MTU change.
    resolutions: list[ResolutionPeriod] = Field(min_length=1)
    #: Where the numbers came from, quoted into the evidence so a disagreement is
    #: settled against a named rule rather than against this file's opinion.
    source: str

    @model_validator(mode="after")
    def _coherent(self) -> MarketSpec:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {self.timezone!r}") from exc
        if self.currency != self.currency.upper() or not self.currency.isalpha():
            raise ValueError(f"currency must be an upper-case ISO-4217 code, got {self.currency!r}")
        if self.day_ahead_gate_closure_days_before < 1:
            raise ValueError("day_ahead_gate_closure_days_before must be at least 1")
        if self.intraday_lead_minutes < 0:
            raise ValueError("intraday_lead_minutes must be non-negative")
        dates = [row.effective_from for row in self.resolutions]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ValueError("resolutions must be ordered oldest-first with unique effective_from")
        return self

    def resolution_for(self, day: date) -> ResolutionPeriod | None:
        """The resolution row in force for a **local delivery date**.

        ``None`` when ``day`` predates the first row: the catalog does not vouch
        for rules it was never given, and guessing the oldest one would silently
        validate a 2010 file against 2025's market time unit.
        """
        chosen: ResolutionPeriod | None = None
        for row in self.resolutions:
            if row.effective_from <= day:
                chosen = row
            else:
                break
        return chosen


# --------------------------------------------------------------------------- the catalog

#: The catalog. Built in, keyed by :attr:`MarketSpec.id`, and the only source of
#: market rules the check will consult (see the module docstring on why an
#: artifact may not carry its own).
#:
#: Two entries, chosen so the second **proves the first is not hard-coded**: they
#: differ in every field that matters — zone (Europe/Oslo vs Europe/London, whose
#: DST transitions happen at the same instant but at *different local wall clocks*),
#: gate closure (12:00 vs 09:20), settlement resolution (60/15 vs 30 minutes) and
#: currency (EUR vs GBP). Adding a third market is one row here and one line in
#: ``test_market_check.py``.
MARKETS: dict[str, MarketSpec] = {
    market.id: market
    for market in (
        MarketSpec(
            id="nordpool-day-ahead",
            label="Nord Pool day-ahead auction (Nordic/Baltic)",
            timezone="Europe/Oslo",
            # The auction clears in EUR; the local-currency figures NO/SE/DK users
            # also see are a published conversion of that clearing price, not a
            # second auction. So a bid priced in NOK is refused rather than
            # converted — the same refusal `services/reconciliation.py` makes.
            currency="EUR",
            day_ahead_gate_closure=time(12, 0),
            day_ahead_gate_closure_days_before=1,
            # The conservative (cross-border) intraday lead time. Within a single
            # bidding zone continuous trading runs closer to delivery; a check that
            # used the looser number would bless a bid the cross-border rule kills.
            intraday_lead_minutes=60,
            resolutions=[
                ResolutionPeriod(
                    effective_from=date(2000, 1, 1),
                    resolution="PT60M",
                    note="hourly market time unit",
                ),
                ResolutionPeriod(
                    effective_from=date(2025, 10, 1),
                    resolution="PT15M",
                    note="15-minute market time unit (the European MTU switch)",
                ),
            ],
            source="Nord Pool day-ahead auction rules; EU 15-minute MTU go-live",
        ),
        MarketSpec(
            id="gb-day-ahead",
            label="GB day-ahead auction",
            timezone="Europe/London",
            currency="GBP",
            day_ahead_gate_closure=time(9, 20),
            day_ahead_gate_closure_days_before=1,
            # GB gate closure is one hour ahead of the settlement period.
            intraday_lead_minutes=60,
            resolutions=[
                ResolutionPeriod(
                    effective_from=date(2000, 1, 1),
                    resolution="PT30M",
                    note="half-hourly settlement period",
                ),
            ],
            source="GB day-ahead auction rules; half-hourly settlement periods",
        ),
    )
}


def market_ids() -> list[str]:
    """Every catalog id, sorted — what an unknown-market failure lists so the
    caller fixes it in one round trip instead of two (AXI shape 3)."""
    return sorted(MARKETS)


# --------------------------------------------------------------------------- the spec


class MarketCheckSpec(BaseModel):
    """``ValidationSpec.params`` for check id ``market_rules``.

    Columns are addressed **by header name**, not by column letter, and the same
    spec drives ``.xlsx`` and ``.csv``. That is forced by the domain rather than by
    taste: the unit of a bid file's volume column lives in its header
    (``volume_mw``, ``Volume (MWh)``), so the check has to read headers anyway, and
    having read them it would be perverse to make the caller count columns.
    """

    #: Workspace-relative path to the bid/schedule artifact (``.xlsx`` or ``.csv``),
    #: jailed against the workspace root.
    artifact: str
    #: Catalog id — see :data:`MARKETS`. An unknown id is a ``fail`` that lists the
    #: known ids, never a skip.
    market: str
    product: MarketProduct = "day_ahead"
    #: When the bid was (or would be) submitted, ISO-8601. **Supply a UTC offset**
    #: (``2024-10-26T11:59:59+02:00``) or a trailing ``Z``. A naive value is read as
    #: local wall-clock in the market's zone and the evidence says so — and when
    #: the local and UTC readings disagree about the verdict, that disagreement is
    #: itself reported, because it is the exact bug this check exists to catch.
    #: ``None`` leaves the deadline unevaluated as an explicit ``skipped`` line.
    submitted_at: str | None = None
    #: Sheet holding the table (``.xlsx`` only). ``None`` = the active sheet.
    sheet: str | None = None
    #: 1-based row carrying the column headers. Data starts on the next row.
    header_row: int = 1
    #: Header of the column carrying each delivery period's **start**.
    timestamp_column: str = "delivery_start"
    #: Header of the column carrying the bid/schedule volume.
    volume_column: str = "volume"
    #: Header of the price column, when the artifact has one. ``None`` skips the
    #: currency check with a named ``skipped``, never silently.
    price_column: str | None = None
    #: Header of an explicit fold column (``0``/``1``) disambiguating the repeated
    #: hour of a DST fall-back day. ``None`` falls back to order of appearance —
    #: and says so.
    fold_column: str | None = None
    #: Declared unit of the volume column, overriding whatever its header says.
    volume_unit: str | None = None
    #: Declared unit of the price column, overriding its header.
    price_unit: str | None = None
    #: Expected local delivery date (``YYYY-MM-DD``). ``None`` = infer from the
    #: rows; when set, a row for any other date is reported.
    delivery_date: str | None = None


# --------------------------------------------------------------------------- the report


class PeriodViolation(BaseModel):
    """One thing wrong with one delivery period — the row an analyst reads."""

    #: The period as the artifact expressed it, or the grid slot that is missing,
    #: e.g. ``2024-10-27T02:00:00`` or ``2024-10-27T02:00:00 (2nd occurrence)``.
    period: str
    kind: ViolationKind
    #: 1-based row in the artifact, when the violation came from one. ``None`` for
    #: a ``missing`` period, which by definition has no row.
    row: int | None = None
    #: One line naming the defect *and* its fix.
    detail: str


class DayCoverage(BaseModel):
    """One local delivery day's period arithmetic — where a 23-/25-hour day is
    either honoured or caught."""

    #: Local delivery date, ``YYYY-MM-DD``.
    day: str
    resolution: SettlementResolution
    #: How many periods that local day actually has, derived from the zone's own
    #: UTC offsets — 24/23/25 hourly, 96/92/100 quarter-hourly.
    expected: int
    #: How many distinct delivery instants the artifact carried for that day.
    observed: int
    #: ``""`` on an ordinary day; otherwise which transition made it short or long.
    dst_transition: str = ""


class GateClosureFinding(BaseModel):
    """The deadline verdict, with both readings of a naive submission timestamp so
    the disagreement that hides the classic bug is visible rather than inferred."""

    market: str
    product: MarketProduct
    #: The binding delivery day (the earliest in the artifact).
    delivery_day: str
    #: Gate closure as local wall-clock, e.g. ``2024-10-26T12:00:00``.
    gate_closure_local: str
    #: …and as the instant it resolves to, which is the comparison actually made.
    gate_closure_utc: str
    #: The submission as given, or ``None`` when the spec carried none.
    submitted_input: str | None = None
    #: The instant the submission resolved to under the documented reading.
    submitted_utc: str | None = None
    #: True when :attr:`submitted_input` carried no UTC offset.
    naive_input: bool = False
    #: True when reading a naive submission as local vs as UTC gives **different
    #: verdicts**. The whole point of the field: that is the bug.
    readings_disagree: bool = False
    outcome: CheckOutcome = "skipped"


class UnitFinding(BaseModel):
    """One column's unit verdict: what the header said, what the market needs."""

    #: ``volume`` or ``price``.
    column: str
    #: The header text as written in the artifact.
    header: str
    #: The unit that was used — declared in the spec or read off the header.
    unit: str | None = None
    #: True when it came from the spec rather than the header.
    declared: bool = False
    outcome: CheckOutcome = "pass"
    detail: str = ""


class MarketComplianceReport(BaseModel):
    """The full finding behind an ``EvidenceItem.payload_ref`` (kind ``artifact``).

    :attr:`violations` is a bounded window; :attr:`truncated` says so when it was
    capped (AXI shape 1) so a reader never mistakes the cap for "that was
    everything"."""

    artifact: str
    market: str
    market_label: str
    timezone: str
    product: MarketProduct
    gate_closure: GateClosureFinding
    days: list[DayCoverage] = Field(default_factory=list)
    units: list[UnitFinding] = Field(default_factory=list)
    #: How many delivery-period rows the artifact carried (before de-duplication).
    rows: int = 0
    violations: list[PeriodViolation] = Field(default_factory=list)
    truncated: EvidenceTruncation | None = None
