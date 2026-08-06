"""Claude plan usage, captured from the one place the SDK offers it.

**The source, verified against claude-agent-sdk 0.2.129 (bundled CLI 2.1.221)**
rather than taken on trust. ``claude_agent_sdk.types`` defines::

    RateLimitStatus = Literal["allowed", "allowed_warning", "rejected"]
    RateLimitType   = Literal["five_hour", "seven_day", "seven_day_opus",
                              "seven_day_sonnet", "overage"]

    @dataclass
    class RateLimitInfo:
        status: RateLimitStatus
        resets_at: int | None = None
        rate_limit_type: RateLimitType | None = None
        utilization: float | None = None          # 0.0 - 1.0
        overage_status: RateLimitStatus | None = None
        overage_resets_at: int | None = None
        overage_disabled_reason: str | None = None
        raw: dict[str, Any] = field(default_factory=dict)

    @dataclass
    class RateLimitEvent:
        rate_limit_info: RateLimitInfo
        uuid: str
        session_id: str

``RateLimitEvent`` is a member of the ``Message`` union, so it arrives on the
same ``receive_response()`` stream as assistant text and tool results, and
``_internal/message_parser.py`` builds it from a CLI frame of type
``rate_limit_event``.

**One event describes one window.** This is the part worth stating plainly,
because it is not what "a usage event carrying the five-hour, weekly and
per-model figures" would suggest: an event carries a *single* ``rate_limit_type``
and a single ``utilization``. The five-hour figure and the weekly figures arrive
as separate events, whenever each of them transitions. So a snapshot is
*accumulated* here, one window at a time, and a window nobody has transitioned
in is simply absent. Building the missing windows out of the ones we have would
be exactly the synthesis this feature promises not to do.

**There is no query API.** ``claude --help`` on the bundled CLI lists no
``usage`` subcommand (``agents``, ``auth``, ``auto-mode``, ``doctor``,
``gateway``, ``install``, ``mcp``, ``plugin``, ``project``, ``setup-token``,
``ultrareview``, ``update``); ``/usage`` is an interactive-TUI command; and
nothing in the SDK exposes a "read current utilization" call. The transition
event is the whole of the supply, which is why every figure here carries the
moment it arrived and why the UI must show its age.

**Nothing is persisted** — and that includes the log. This is live state about
an *account*, not workspace data: a restart starts empty, which reads as "not
known yet", the same honest state as an account that never emits at all. Writing
no file of our own is only half of it, though: the packaged desktop shell runs
this server as a child process and copies its **stdout** into ``shell.log``,
kept across restarts (``desktop/src-tauri/src/backend.rs``), and structlog
prints to stdout. So a figure logged at the default level is a figure on a real
user's disk. Utilization and reset times are therefore ``debug`` only, and
``test_no_usage_figure_is_logged_at_the_default_level`` holds that at the file
descriptor the shell actually reads.
"""

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final

import structlog

from workbench_server.models.usage import (
    UsageBucket,
    UsageBucketKind,
    UsageEvent,
    UsageModelCost,
    UsageOverage,
    UsageSessionCost,
    UsageSessionEntry,
    UsageSnapshot,
    UsageStatus,
)
from workbench_server.services.event_bus import EventBus

log = structlog.get_logger()

#: Presentation order, and the order ``UsageSnapshot.buckets`` comes back in:
#: the shortest window first (it is the one that bites first), then the weekly
#: total, then the per-model weeklies, then overage, then anything the CLI did
#: not attribute to a window. Spelled out rather than derived from the Literal
#: because a derived tuple types as ``Any`` and would defeat the narrowing
#: below; ``test_usage.py`` asserts the two stay in step.
BUCKET_ORDER: Final[tuple[UsageBucketKind, ...]] = (
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "overage",
    "unspecified",
)
STATUSES: Final[tuple[UsageStatus, ...]] = ("allowed", "allowed_warning", "rejected")

#: Name -> our own Literal, which is the whole of the "is this a value we know"
#: test. ``unspecified`` is ours, never something the CLI sends, so it is not a
#: name an event may resolve to.
_KIND_BY_NAME: Final[dict[str, UsageBucketKind]] = {
    kind: kind for kind in BUCKET_ORDER if kind != "unspecified"
}
_STATUS_BY_NAME: Final[dict[str, UsageStatus]] = {status: status for status in STATUSES}

#: Cap on per-model cost rows kept in the degraded view. A long-lived server
#: sees a handful of model ids; this only stops an unbounded map.
MAX_MODELS: Final = 12

#: Cap on per-session cost rows. Comfortably above any concurrent-session
#: ceiling *and* above the orchestrator's worker limit, because a stopped
#: worker's cost stays worth showing — "that one cost $2.40 before it failed" is
#: the number the next spawn decision is made on. LRU by last turn, so what
#: falls out is the oldest, not the most expensive.
MAX_SESSION_COSTS: Final = 32


def _finite(value: Any) -> float | None:
    """A real number, or None. Guards the wire against NaN/inf (which are not
    JSON) and against the CLI sending something that is not a number at all."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def bucket_kind(raw: Any) -> UsageBucketKind:
    """The window this event describes.

    ``None`` — which the SDK's own type allows — and any value a future CLI
    invents both land on ``unspecified``: we report that a limit moved without
    claiming to know which one. The unknown case is logged, because a new
    ``RateLimitType`` is a schema change we want to see rather than absorb.
    """
    if raw is None:
        return "unspecified"
    known = _KIND_BY_NAME.get(raw) if isinstance(raw, str) else None
    if known is not None:
        return known
    log.warning("usage.unknown_rate_limit_type", value=str(raw))
    return "unspecified"


def usage_status(raw: Any) -> UsageStatus | None:
    """One of the SDK's three statuses, or None for anything else."""
    return _STATUS_BY_NAME.get(raw) if isinstance(raw, str) else None


class UsageService:
    """Accumulates what the SDK says about the account's limits, and publishes.

    Lives on the event loop: every caller is
    :class:`~workbench_server.services.agent_sessions.AgentSession`, which
    handles SDK messages inside its own turn task. No thread marshalling (unlike
    the provenance correlator, which the sync file handlers call from a worker).
    """

    def __init__(self, bus: EventBus, clock: Callable[[], float] = time.time) -> None:
        self._bus = bus
        self._clock = clock
        self._buckets: OrderedDict[UsageBucketKind, UsageBucket] = OrderedDict()
        self._overage: UsageOverage | None = None
        self._cost = UsageSessionCost()
        self._models: OrderedDict[str, UsageModelCost] = OrderedDict()
        #: Per-session spend, LRU by last turn. The one authority Mission
        #: Control's per-worker budget is enforced against — see
        #: :data:`MAX_SESSION_COSTS` and ``services/orchestrator.py``.
        self._sessions: OrderedDict[str, UsageSessionEntry] = OrderedDict()
        #: Keys of ``RateLimitInfo.raw`` we have already reported as unmodeled,
        #: so a new CLI field is logged once rather than on every event.
        self._unmodeled_seen: set[str] = set()

    # ---- the signal --------------------------------------------------------

    def note_rate_limit(self, info: Any) -> None:
        """Record one ``RateLimitInfo`` and publish the new snapshot.

        Duck-typed on purpose, exactly like ``AgentSession._handle_sdk_message``:
        the SDK's dataclasses are not importable in the test environment the
        fakes run in, and reading attributes keeps the real path and the fake
        path identical.

        An event whose ``status`` we cannot recognize is dropped with a warning
        rather than coerced — ``status`` is the one field the SDK marks
        required, so an unrecognizable value means we are not looking at what we
        think we are.
        """
        raw_status = getattr(info, "status", None)
        status = usage_status(raw_status)
        if status is None:
            log.warning("usage.unusable_rate_limit_event", status=str(raw_status))
            return
        now = self._clock()
        kind = bucket_kind(getattr(info, "rate_limit_type", None))
        bucket = UsageBucket(
            kind=kind,
            status=status,
            utilization=_finite(getattr(info, "utilization", None)),
            resets_at=_finite(getattr(info, "resets_at", None)),
            observed_at=now,
        )
        self._buckets[kind] = bucket
        overage_status = usage_status(getattr(info, "overage_status", None))
        if overage_status is not None:
            reason = getattr(info, "overage_disabled_reason", None)
            self._overage = UsageOverage(
                status=overage_status,
                resets_at=_finite(getattr(info, "overage_resets_at", None)),
                disabled_reason=reason if isinstance(reason, str) and reason else None,
                observed_at=now,
            )
        self._log_unmodeled(getattr(info, "raw", None))
        # Debug, not info, and this is a privacy rule rather than a noise one.
        # "Nothing is persisted" is only true of *our* files: the packaged
        # desktop shell copies this process's stdout into ``shell.log`` and keeps
        # it across restarts, and structlog prints to stdout by default. Logging
        # the account's real utilization and reset time at the default level
        # would therefore write plan usage to a user's disk on the default path —
        # which is the promise this module opens by making. Same gate
        # ``_log_unmodeled`` sits behind, and the same rule the rest of the
        # codebase follows (``agent_sessions.py`` logs identifiers, never
        # values). ``test_no_usage_figure_is_logged_at_the_default_level`` holds
        # it at the file descriptor the shell actually reads.
        log.debug(
            "usage.rate_limit",
            kind=kind,
            status=status,
            utilization=bucket.utilization,
            resets_at=bucket.resets_at,
        )
        self._publish()

    def note_turn(
        self, *, session_id: str, cost_usd: float | None, model_usage: Any = None
    ) -> None:
        """Record what one finished turn cost — the *degraded* view's whole
        supply, and the only figure we have for an account that never emits a
        rate-limit event.

        Called for every turn regardless, because "what has this window cost"
        is worth showing next to plan usage even when plan usage is known.

        ``session_id`` splits the same arithmetic per conversation. It is the
        budget Mission Control enforces (``services/orchestrator.py``) and the
        cost its board renders — deliberately the *same* number, because two
        accumulators for one figure is the duplication ROADMAP item 7 was
        re-scoped to prevent.
        """
        cost = _finite(cost_usd)
        models = self._merge_model_usage(model_usage)
        if cost is None and not models:
            return
        now = self._clock()
        # ``models`` stays empty here and is filled from the live map in
        # ``snapshot`` — one place builds the sorted list, so the two cannot
        # disagree about which model led.
        self._cost = UsageSessionCost(
            turns=self._cost.turns + 1,
            total_cost_usd=self._cost.total_cost_usd + (cost or 0.0),
            last_turn_cost_usd=cost,
            observed_at=now,
        )
        previous = self._sessions.get(session_id)
        self._sessions[session_id] = UsageSessionEntry(
            session_id=session_id,
            turns=(previous.turns if previous else 0) + 1,
            cost_usd=(previous.cost_usd if previous else 0.0) + (cost or 0.0),
            observed_at=now,
        )
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > MAX_SESSION_COSTS:
            self._sessions.popitem(last=False)
        self._publish()

    def session_entry(self, session_id: str) -> UsageSessionEntry | None:
        """What one session has spent, or None if it has never finished a turn.

        The read half of the budget check. ``None`` is not zero: a worker that
        has not settled a turn yet has spent nothing *we know of*, and a caller
        that treated it as a measured zero would be claiming knowledge it does
        not have.
        """
        return self._sessions.get(session_id)

    def _merge_model_usage(self, model_usage: Any) -> bool:
        """Fold ``ResultMessage.model_usage`` into the per-model totals.

        The SDK types it as ``dict[str, ModelUsage]`` where ``ModelUsage`` is a
        camelCase ``TypedDict`` passed through verbatim from the CLI
        (``costUSD``, ``inputTokens``, ``outputTokens``). Anything else in there
        is ignored rather than guessed at. Returns whether anything was merged.
        """
        if not isinstance(model_usage, dict):
            return False
        merged = False
        for name, entry in model_usage.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            cost = _finite(entry.get("costUSD")) or 0.0
            inputs = _finite(entry.get("inputTokens")) or 0.0
            outputs = _finite(entry.get("outputTokens")) or 0.0
            previous = self._models.get(name)
            self._models[name] = UsageModelCost(
                model=name,
                cost_usd=(previous.cost_usd if previous else 0.0) + cost,
                input_tokens=(previous.input_tokens if previous else 0) + int(inputs),
                output_tokens=(previous.output_tokens if previous else 0) + int(outputs),
            )
            merged = True
        while len(self._models) > MAX_MODELS:
            self._models.popitem(last=False)
        return merged

    def _log_unmodeled(self, raw: Any) -> None:
        """Say once, at debug, which CLI fields we are not modelling.

        ``RateLimitInfo.raw`` is the full dict the CLI sent. When it grows a
        field, the choice to ignore it should be visible in a log rather than
        invisible in a schema — but never on every event.
        """
        if not isinstance(raw, dict):
            return
        modeled = {
            "status",
            "resetsAt",
            "rateLimitType",
            "utilization",
            "overageStatus",
            "overageResetsAt",
            "overageDisabledReason",
        }
        extra = sorted(key for key in raw if isinstance(key, str) and key not in modeled)
        unseen = [key for key in extra if key not in self._unmodeled_seen]
        if not unseen:
            return
        self._unmodeled_seen.update(unseen)
        log.debug("usage.unmodeled_fields", fields=unseen)

    # ---- what the UI reads -------------------------------------------------

    def snapshot(self) -> UsageSnapshot:
        """The current picture, with its own age measured server-side."""
        buckets = [self._buckets[kind] for kind in BUCKET_ORDER if kind in self._buckets]
        observed_at = max((bucket.observed_at for bucket in buckets), default=None)
        cost = self._cost.model_copy(
            update={
                "models": sorted(
                    self._models.values(), key=lambda entry: entry.cost_usd, reverse=True
                )
            }
        )
        return UsageSnapshot(
            buckets=buckets,
            overage=self._overage,
            observed_at=observed_at,
            age_s=None if observed_at is None else max(0.0, self._clock() - observed_at),
            session_cost=cost,
            # Most expensive first: the board reads this to answer "which worker
            # is eating the budget", and that question has one obvious order.
            sessions=sorted(self._sessions.values(), key=lambda e: e.cost_usd, reverse=True),
        )

    def _publish(self) -> None:
        self._bus.publish(UsageEvent(snapshot=self.snapshot()))
