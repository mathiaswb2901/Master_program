"""Claude plan usage: the buckets the Agent SDK reports, and nothing else.

The source is one event on a live session's message stream — the CLI's
``rate_limit_event``, surfaced by the SDK as ``RateLimitEvent`` carrying a
single :class:`~claude_agent_sdk.RateLimitInfo`. Everything in this module is a
field that event actually has (see ``services/usage.py`` for the field-by-field
mapping); nothing here is computed from a trend, a rate or an assumption.

Four properties of that source are load-bearing enough to be part of the schema
rather than a note in the UI:

1. **It is a transition event, not a query.** Every figure carries the
   ``observed_at`` it arrived at, and the snapshot carries its own ``age_s``, so
   a stale number can never be rendered as a current one.
2. **There is no polling.** The CLI emits when the status *changes*; nothing on
   this side can ask for a fresh value.
3. **An account may never emit one.** ``buckets == []`` is a first-class state,
   not an error, and :class:`UsageSessionCost` is what we fall back to — money
   this process has spent, which is *not* plan usage and is labelled as such.
4. **We report the buckets we are given.** There is no synthesized per-model
   breakdown and no extrapolated burn rate; a window the SDK never named is
   simply absent.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: Which window a figure belongs to. The first five are the SDK's own
#: ``RateLimitType`` values, verbatim. ``unspecified`` is ours and covers the one
#: case the SDK's own type allows: ``rate_limit_type=None`` — the CLI said the
#: status changed without naming the window. Dropping such an event would hide a
#: real limit signal; guessing which window it was would be synthesis.
UsageBucketKind = Literal[
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "overage",
    "unspecified",
]

#: The SDK's ``RateLimitStatus``: approaching, or already refused.
UsageStatus = Literal["allowed", "allowed_warning", "rejected"]


class UsageBucket(BaseModel):
    """One rate-limit window, exactly as the last event described it."""

    kind: UsageBucketKind
    status: UsageStatus
    #: Fraction of the window consumed, 0.0 to 1.0. None when the event carried no
    #: ``utilization`` — the status is then all we know, and the UI says so
    #: rather than drawing an empty bar as if it meant zero.
    utilization: float | None = None
    #: Unix seconds when this window resets, or None if the event omitted it.
    resets_at: float | None = None
    #: Unix seconds when *this* bucket last arrived. Per bucket, not per
    #: snapshot: windows transition independently, so the five-hour figure can
    #: be minutes old while the weekly one is hours old.
    observed_at: float


class UsageOverage(BaseModel):
    """Pay-as-you-go state, when an event carried any.

    Reported separately from :class:`UsageBucket` because the SDK reports it
    both ways: as its own ``rate_limit_type`` *and* as ``overageStatus`` fields
    riding on another window's event. The latter lands here.
    """

    status: UsageStatus
    resets_at: float | None = None
    #: Why overage is unavailable, when the CLI says so. Passed through as text.
    disabled_reason: str | None = None
    observed_at: float


class UsageModelCost(BaseModel):
    """What one model cost this process, from ``ResultMessage.model_usage``.

    Part of the degraded view, never of plan usage: these are dollars and tokens
    this server watched go by, not a share of any weekly limit.
    """

    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


class UsageSessionCost(BaseModel):
    """What we know without a rate-limit event: this process's own spend.

    Accumulated from the ``ResultMessage`` that ends every turn, across every
    session this server has run since it started. It answers a different
    question from plan usage and the UI must never present it as an answer to
    the same one.
    """

    turns: int = 0
    total_cost_usd: float = 0.0
    last_turn_cost_usd: float | None = None
    #: Newest first is not promised; sorted by cost so the loudest model leads.
    models: list[UsageModelCost] = Field(default_factory=list)
    #: Unix seconds of the last turn that reported a cost, or None if none has.
    observed_at: float | None = None


class UsageSessionEntry(BaseModel):
    """What one *session* has cost this process, from its own turns.

    The same arithmetic as :class:`UsageSessionCost`, split by the session that
    produced it — and it exists for one reason: Mission Control's per-worker
    budget has to be enforced against a number, and a second accumulator
    somewhere else would be two authorities on what a worker spent (ROADMAP
    item 7 re-scope). ``services/orchestrator.py`` checks its ceiling against
    exactly what the board renders.

    Still **not plan usage**: these are dollars this server watched go by, the
    same caveat :class:`UsageSessionCost` carries.
    """

    session_id: str
    turns: int = 0
    cost_usd: float = 0.0
    #: Unix seconds of the last turn this session reported.
    observed_at: float


class UsageSnapshot(BaseModel):
    """``GET /api/usage`` and the payload of :class:`UsageEvent`.

    ``buckets == []`` with ``observed_at is None`` is the honest "this account
    has never emitted a rate-limit event" state — the one most likely to be what
    a given user sees, since the CLI only emits on a *transition*.
    """

    #: Every window the SDK has named this run, in a fixed order (the order of
    #: ``UsageBucketKind``) rather than by arrival — meters that reshuffle when
    #: one window transitions would be unreadable at a glance.
    buckets: list[UsageBucket] = Field(default_factory=list)
    overage: UsageOverage | None = None
    #: Unix seconds of the newest bucket observation; None = never emitted.
    observed_at: float | None = None
    #: Seconds since ``observed_at``, computed server-side at build time so the
    #: age does not depend on the browser's clock agreeing with the server's.
    #: None when nothing has ever been observed.
    age_s: float | None = None
    session_cost: UsageSessionCost = Field(default_factory=UsageSessionCost)
    #: Per-session spend, most expensive first and bounded. A session that has
    #: never finished a turn is absent — zero and "not yet" are different
    #: statements, and the board renders them differently.
    sessions: list[UsageSessionEntry] = Field(default_factory=list)


class UsageEvent(BaseModel):
    """Broadcast on ``/ws/events`` whenever a snapshot changes.

    The whole snapshot rather than the one bucket that moved: it is small, it is
    what every surface renders, and it makes a client's reconnect path
    (``GET /api/usage``) and its live path identical.
    """

    type: Literal["usage"] = "usage"
    snapshot: UsageSnapshot
