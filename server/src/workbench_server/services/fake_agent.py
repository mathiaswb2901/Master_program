"""Scripted fake agent (``WORKBENCH_FAKE_AGENT=1``).

A :data:`~workbench_server.services.agent_sessions.ClientFactory` that answers
deterministically without the Agent SDK, a Claude login, or a single token. It
exists so the Playwright suite can drive the *real* backend — real WebSockets,
real session state machine, real plan and permission round-trips — over a
conversation whose every frame is known in advance. Layer 2.5 of the testing
layers in ``ARCHITECTURE.md``: below the live smoke test, above the in-process
integration fakes it is deliberately shaped like.

The seam is the same one the unit tests use: the session injects a client
factory and hands the session itself in as the ``SessionBridge``, so nothing
here is a special path through the production code — only a different client on
the other side of it.

What one user message produces, in order:

* a short markdown reply, streamed as text deltas;
* ``stay busy``      — the turn is held open long enough for a UI to observe
  the working state before it settles. Combined with ``use tool`` the hold
  moves to *after* the tool's result, which is the only way a UI test can tell
  a row that settled on its own result apart from one the UI settled wholesale
  when the turn ended;
* ``use tool``       — a ``Read`` of a real file in the session folder: a
  tool-use note and, separately, its result;
* ``write file``     — a ``Write`` of a real file in the session folder: the
  note first, *then* the bytes hit disk, exactly as a real tool call orders
  them, so the provenance correlator sees the claim before the watcher event
  it has to explain;
* ``refuse write``   — the same ``Write`` announcement for a *different* path,
  settled as a tool **error** with nothing written. The shape of a denied
  permission card or an ``Edit`` whose old string was not found: a claim exists
  for a write that never happened, and whatever changes that path next must
  still come back unattributed;
* ``ask permission`` — a permission prompt through the bridge, then the
  outcome echoed as text;
* ``plan please``    — a fixed :class:`PlanArtifact` through the bridge, then
  the user's verdict and choices echoed as text;
* ``usage please``   — three ``RateLimitEvent`` messages, one per window, the
  shape the CLI really emits: one event describes *one* bucket, so a full
  picture is several events (``services/usage.py``). Deliberately a trigger and
  not a default, because "no rate-limit event has ever arrived" is the state a
  real account is most likely to be in and the surface has to be tested in it;
* ``visual please``  — a plan whose first node is a ``visual``: every leaf kind
  and every block layout at once, on a real 25-hour market day, with a cell
  whose text looks like markup — followed by a two-sentence markdown caveat,
  which is the node a *text-range* anchor points into. A separate trigger
  rather than a bigger ``plan please`` on purpose — that card is the regression
  test for the four original node kinds, and growing it would quietly change
  what that journey proves.

Never enabled by default (``Settings.fake_agent``), and ``main.py`` logs a
warning on startup when it is: a workbench that quietly answers with canned
text instead of an agent is worse than one that fails loudly.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from workbench_server.models.plans import (
    AnnotationAnchor,
    FileRef,
    MarkdownNode,
    OptionGroupNode,
    PlanArtifact,
    PlanOption,
    PlanResponse,
    PlanStep,
    StepListNode,
    VisualNode,
)
from workbench_server.models.visuals import (
    CellHighlight,
    ChartLeaf,
    ChartSeries,
    CodeDiffLeaf,
    DiagramEdge,
    DiagramLeaf,
    DiagramNode,
    Metric,
    MetricsLeaf,
    TableColumn,
    TableLeaf,
    TimeAxis,
    ValueAxis,
    VisualBlock,
)
from workbench_server.services.agent_sessions import ClientFactory, SdkClient, SessionBridge

log = structlog.get_logger()

#: Triggers, matched case-insensitively anywhere in the user's message.
BUSY_TRIGGER = "stay busy"
TOOL_TRIGGER = "use tool"
WRITE_TRIGGER = "write file"
REFUSED_WRITE_TRIGGER = "refuse write"
PERMISSION_TRIGGER = "ask permission"
PLAN_TRIGGER = "plan please"
VISUAL_TRIGGER = "visual please"
USAGE_TRIGGER = "usage please"

#: How long ``stay busy`` holds the turn open (before the reply, or after the
#: tool result when ``use tool`` is asked for too). Long enough for a UI test to
#: see the session chip pulse and to settle again afterwards, short enough that
#: the whole suite stays under a few minutes. This is the *only* wall-clock wait
#: in fake mode — the tests never sleep, they wait on the app's signals.
BUSY_HOLD_S = 1.5

#: Cap on the excerpt a fake ``Read`` returns; the session caps again on the way
#: out (``TOOL_EXCERPT_LIMIT``), this keeps the pretend tool result small too.
READ_EXCERPT_CHARS = 400

#: The command the scripted permission prompt asks about. Never executed —
#: nothing in this module runs anything.
PERMISSION_COMMAND = "echo scripted-permission"

#: File the scripted ``Write`` creates, relative to the session folder. Named so
#: it sorts *after* the file a fake ``Read`` picks (see ``first_workspace_file``)
#: — the two triggers share a workspace in the E2E suite, and a write that stole
#: the "alphabetically first file" slot would silently retarget every Read.
WRITE_TARGET_NAME = "written-by-agent.md"

#: What the scripted ``Write`` puts in that file. Changes on every call so a
#: second write is a real change on disk, not a no-op the watcher never reports.
WRITE_BODY = "# Written by the fake agent\n\nRevision {revision}.\n"

#: Model id the fake reports spending nothing on. Obviously not a real one:
#: a scripted client must never look like it billed a model.
FAKE_MODEL = "fake-agent"

#: The three windows ``usage please`` reports, and the state each is in — one
#: below any threshold, one warning (carrying overage state, which the SDK
#: hangs on another window's event), one already refused. Chosen to cover the
#: whole semantic ramp in a single turn, so the E2E journey can assert that a
#: bar near its cap carries a *label* and not just a colour (DESIGN.md §7).
#: ``resets_at`` is an offset because "resets in 2 h" is the fact under test;
#: it is stamped against the wall clock when the event is built.
FAKE_RATE_LIMITS: tuple[tuple[str, str, float, int], ...] = (
    ("five_hour", "allowed", 0.42, 2 * 3600),
    ("seven_day", "allowed_warning", 0.86, 3 * 86400),
    ("seven_day_opus", "rejected", 1.0, 5 * 86400),
)

#: Overage state the ``seven_day`` event carries alongside its own figures.
FAKE_OVERAGE_STATUS = "rejected"
FAKE_OVERAGE_REASON = "overage is not enabled for this account"

#: Path the *refused* ``Write`` names and never creates. Must sort after the
#: file a fake ``Read`` picks, for the same reason ``WRITE_TARGET_NAME`` does.
REFUSED_WRITE_TARGET_NAME = "refused-by-agent.md"

#: What that refused call comes back with — an error result, no bytes on disk.
REFUSED_WRITE_ERROR = "String to replace not found in file"


# ---- SDK-shaped messages ----------------------------------------------------
#
# ``AgentSession._handle_sdk_message`` duck-types on the class name (the SDK's
# types are not importable without the SDK), so these mirror the names exactly,
# same as the scripted fakes in server/tests.


class StreamEvent:
    """A partial-message frame; carries the text deltas."""

    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event


class ToolUseBlock:
    def __init__(self, name: str, tool_input: dict[str, Any], block_id: str) -> None:
        self.name = name
        self.input = tool_input
        self.id = block_id


class ToolResultBlock:
    def __init__(self, tool_use_id: str, content: str, is_error: bool = False) -> None:
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class UserMessage:
    """The SDK's carrier for tool results — not something the human typed."""

    def __init__(self, content: list[Any]) -> None:
        self.content = content


class RateLimitInfo:
    """One window's state — the SDK dataclass's fields, snake_case as it
    exposes them (the CLI's own JSON is camelCase; the SDK's parser renames)."""

    def __init__(
        self,
        *,
        status: str,
        rate_limit_type: str | None,
        utilization: float | None,
        resets_at: int | None,
        overage_status: str | None = None,
        overage_resets_at: int | None = None,
        overage_disabled_reason: str | None = None,
    ) -> None:
        self.status = status
        self.rate_limit_type = rate_limit_type
        self.utilization = utilization
        self.resets_at = resets_at
        self.overage_status = overage_status
        self.overage_resets_at = overage_resets_at
        self.overage_disabled_reason = overage_disabled_reason
        self.raw: dict[str, Any] = {}


class RateLimitEvent:
    """What the CLI emits when a rate-limit window *transitions*. One event
    carries one window — see ``services/usage.py`` for why that matters."""

    def __init__(self, rate_limit_info: RateLimitInfo, session_id: str) -> None:
        self.rate_limit_info = rate_limit_info
        self.uuid = uuid.uuid4().hex
        self.session_id = session_id


class ResultMessage:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.total_cost_usd = 0.0
        self.is_error = False
        #: Per-model spend, exactly the CLI's camelCase ``modelUsage`` shape.
        #: Zeroed like ``total_cost_usd``: fake mode spends nothing, and a
        #: fabricated dollar figure in a cost readout would be a lie the UI
        #: could not tell from a real one.
        self.model_usage: dict[str, dict[str, Any]] = {
            FAKE_MODEL: {"costUSD": 0.0, "inputTokens": 0, "outputTokens": 0}
        }


def _delta(text: str) -> StreamEvent:
    return StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    )


# ---- scripted content -------------------------------------------------------


def fake_plan() -> PlanArtifact:
    """The card ``plan please`` presents: one option group (with a recommended
    option) and one step list carrying a file ref — the two node kinds whose
    rendering the E2E suite asserts. ``plan_id`` is minted per call, so a
    re-presented plan is always a fresh card."""
    return PlanArtifact(
        title="Scripted plan",
        summary="A fixed plan the fake agent presents so the card can be tested.",
        nodes=[
            OptionGroupNode(
                node_id="approach",
                prompt="Which representation?",
                options=[
                    PlanOption(
                        option_id="local",
                        label="Local time + fold",
                        pros=["Matches the market's own clock"],
                        cons=["Fold handling everywhere"],
                        recommended=True,
                    ),
                    PlanOption(
                        option_id="utc",
                        label="UTC everywhere",
                        pros=["One clock"],
                        cons=["Breaks at DST boundaries"],
                    ),
                ],
            ),
            StepListNode(
                node_id="steps",
                steps=[
                    PlanStep(
                        text="Add a fold-aware index",
                        file_refs=[FileRef(path="notes.md")],
                    )
                ],
            ),
        ],
    )


#: The autumn clock change in the Nordic market: 2026-10-25 is 25 hours long in
#: Europe/Stockholm, and 02:00 happens twice. Hard-coded because the point of
#: the journey is that the *renderer* draws that day correctly — a date computed
#: from "today" would stop testing DST on 364 days of the year.
VISUAL_DAY_START = datetime(2026, 10, 25, 0, 0, tzinfo=timezone(timedelta(hours=2)))
VISUAL_ZONE = "Europe/Stockholm"

#: A cell whose text looks like markup. The card must show these characters,
#: never interpret them — asserted in the E2E journey and in the vitest suite.
VISUAL_MARKUP_CELL = "<script>alert('xss')</script>"

#: 25 hourly prices and the dispatch they imply — a duck curve with a negative
#: midday hour, which is what makes the day worth drawing.
VISUAL_PRICES = [
    41.2, 38.7, 36.1, 35.4, 34.9, 37.8, 46.3, 58.1, 64.7, 52.2,
    31.5, 12.4, -3.8, -6.1, 4.7, 22.9, 48.6, 71.3, 88.4, 79.5,
    63.2, 55.8, 49.1, 44.6, 42.3,
]  # fmt: skip
VISUAL_DISPATCH = [
    0.0, 0.0, -5.0, -5.0, -5.0, 0.0, 2.5, 5.0, 5.0, 0.0,
    -5.0, -5.0, -5.0, -5.0, -5.0, 0.0, 5.0, 5.0, 5.0, 5.0,
    2.5, 0.0, 0.0, 0.0, 0.0,
]  # fmt: skip


#: Prose the annotate journey slices a *range* anchor out of. Two sentences,
#: because the point of the assertion is that the user points at **one** of
#: them. It trails the visual node rather than leading it, so
#: ``plan.nodes[0]`` is still the scene the visual journey reaches for.
VISUAL_CAVEAT = (
    "Prices are provisional until the market clears. The second 02:00 uses the fold-1 curve."
)


def fake_visual_plan() -> PlanArtifact:
    """The card ``visual please`` presents: one visual node exercising every
    leaf kind and every block layout, on a real 25-hour market day — plus a
    two-sentence markdown caveat, which is what a text-range anchor points into."""
    return PlanArtifact(
        title="Åsen 2 — 25 October dispatch",
        summary="The autumn clock change: 25 delivery hours, two of them 02:00.",
        nodes=[
            VisualNode(
                node_id="scene",
                title="Day-ahead result",
                blocks=[
                    VisualBlock(
                        items=[
                            MetricsLeaf(
                                title="Day totals",
                                items=[
                                    Metric(label="Delivery hours", value="25", role="accent"),
                                    Metric(label="Revenue", value="18 420", unit="EUR"),
                                    Metric(label="Cycles", value="1.4", role="success"),
                                    Metric(label="Negative hours", value="2", role="warning"),
                                ],
                            )
                        ]
                    ),
                    VisualBlock(
                        items=[
                            ChartLeaf(
                                title="Price and dispatch",
                                x=TimeAxis(start=VISUAL_DAY_START, timezone=VISUAL_ZONE),
                                y=ValueAxis(label="Price", unit="EUR/MWh"),
                                y_right=ValueAxis(label="Dispatch", unit="MW"),
                                series=[
                                    ChartSeries(label="SE3 day-ahead", values=VISUAL_PRICES),
                                    ChartSeries(
                                        label="Åsen 2",
                                        style="step",
                                        values=VISUAL_DISPATCH,
                                        axis="right",
                                    ),
                                ],
                            )
                        ]
                    ),
                    VisualBlock(
                        layout="split",
                        items=[
                            TableLeaf(
                                title="Before",
                                columns=[
                                    TableColumn(label="Hour"),
                                    TableColumn(label="Price", type="numeric", unit="EUR/MWh"),
                                ],
                                rows=[["02:00 (1st)", "-3.8"], ["02:00 (2nd)", "-6.1"]],
                                highlights=[CellHighlight(row=1, column=1, role="error")],
                            ),
                            TableLeaf(
                                title="After",
                                columns=[
                                    TableColumn(label="Hour"),
                                    TableColumn(label="Price", type="numeric", unit="EUR/MWh"),
                                    TableColumn(label="Source", type="code"),
                                ],
                                rows=[
                                    ["02:00 (1st)", "-3.8", "fold=0"],
                                    ["02:00 (2nd)", "-6.1", VISUAL_MARKUP_CELL],
                                ],
                                highlights=[CellHighlight(row=1, role="success")],
                            ),
                        ],
                    ),
                    VisualBlock(
                        layout="row",
                        items=[
                            DiagramLeaf(
                                title="Pipeline",
                                nodes=[
                                    DiagramNode(id="feed", label="TGN feed"),
                                    DiagramNode(id="curve", label="Price curve"),
                                    DiagramNode(id="opt", label="Optimizer", role="accent"),
                                    DiagramNode(id="bid", label="Bid file", role="success"),
                                    DiagramNode(id="gate", label="Gate 12:00", role="warning"),
                                ],
                                edges=[
                                    DiagramEdge(source="feed", target="curve", label="hourly"),
                                    DiagramEdge(source="curve", target="opt"),
                                    DiagramEdge(source="opt", target="bid"),
                                    DiagramEdge(source="bid", target="gate"),
                                    DiagramEdge(source="feed", target="opt", label="fallback"),
                                ],
                            ),
                            CodeDiffLeaf(
                                title="calendar.py",
                                language="python",
                                before="hours = range(24)\nfor h in hours:\n    bid(h)\n",
                                after=(
                                    "hours = delivery_hours(day, tz)\nfor h in hours:\n    bid(h)\n"
                                ),
                            ),
                        ],
                    ),
                ],
            ),
            MarkdownNode(node_id="caveat", text=VISUAL_CAVEAT),
        ],
    )


def fake_rate_limit_events(session_id: str, now: float | None = None) -> list[RateLimitEvent]:
    """The events ``usage please`` puts on the stream: one per window, with
    reset times relative to when they are emitted."""
    stamp = time.time() if now is None else now
    events: list[RateLimitEvent] = []
    for kind, status, utilization, resets_in in FAKE_RATE_LIMITS:
        overage: dict[str, Any] = {}
        if kind == "seven_day":
            overage = {
                "overage_status": FAKE_OVERAGE_STATUS,
                "overage_disabled_reason": FAKE_OVERAGE_REASON,
            }
        events.append(
            RateLimitEvent(
                RateLimitInfo(
                    status=status,
                    rate_limit_type=kind,
                    utilization=utilization,
                    resets_at=int(stamp + resets_in),
                    **overage,
                ),
                session_id,
            )
        )
    return events


def reply_text(prompt: str) -> str:
    """The markdown reply, as one string (streamed in chunks below)."""
    echoed = " ".join(prompt.split())[:120]
    return f"**Fake agent** answering.\n\n- echo: {echoed}\n- mode: scripted, no tokens spent\n"


def anchor_echo(anchor: AnnotationAnchor) -> str:
    """One anchor as a slash path: ``scene/leaf/2/row/1/col/Price``.

    A flattening for a *test assertion*, not a second wire format — the agent
    receives the typed anchor. Written this way so the E2E journey can assert
    the exact part the user pointed at arrived, in one string, without the
    assertion having to know how a JSON list is serialized.
    """
    if anchor.kind == "plan":
        return "plan"
    return "/".join([anchor.node_id, *(str(segment) for segment in anchor.path)])


def plan_echo(response: PlanResponse) -> str:
    """How the fake reports the decision it got back — the assertion the E2E
    plan journey makes that the *agent* really received the user's choice, and
    (since PR 3) the exact parts of the artifact their notes point at."""
    choices = ", ".join(f"{node}={option}" for node, option in sorted(response.choices.items()))
    notes = "; ".join(f"{anchor_echo(note.anchor)}={note.text}" for note in response.annotations)
    return f"\n\nplan {response.verdict}: {choices or 'no choices'}\n\nnotes: {notes or 'none'}\n"


def first_workspace_file(folder: Path) -> Path | None:
    """The file a fake ``Read`` targets: the alphabetically first regular file
    directly in the session folder. Deliberately a real file — the tool row the
    UI renders then names something the user can actually open."""
    try:
        candidates = sorted(
            (child for child in folder.iterdir() if child.is_file()), key=lambda p: p.name
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


# ---- the client -------------------------------------------------------------


class FakeAgentClient:
    """One scripted conversation. Satisfies the ``SdkClient`` protocol."""

    def __init__(self, folder: Path, bridge: SessionBridge) -> None:
        self._folder = folder
        self._bridge = bridge
        self._session_id = f"fake-{uuid.uuid4().hex[:8]}"
        self._prompt = ""
        self._writes = 0
        #: Every prompt this client was asked, for tests.
        self.prompts: list[str] = []
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self._prompt = prompt
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        prompt = self._prompt
        lowered = prompt.lower()
        busy = BUSY_TRIGGER in lowered
        uses_tool = TOOL_TRIGGER in lowered
        # Where the hold goes is the whole point of combining the two triggers:
        # after the tool's result, the turn is still open while the row is
        # already settled — the state a UI can only reach by handling the
        # tool's own result frame.
        if busy and not uses_tool:
            await asyncio.sleep(BUSY_HOLD_S)
        for chunk in reply_text(prompt).splitlines(keepends=True):
            yield _delta(chunk)
        if uses_tool:
            for message in self._read_a_file():
                yield message
            if busy:
                await asyncio.sleep(BUSY_HOLD_S)
        if WRITE_TRIGGER in lowered:
            call_id = f"fake-tool-{uuid.uuid4().hex[:8]}"
            # The note goes out first and the bytes land after it, which is the
            # order a real tool call has — and the order the provenance
            # correlator needs, since it claims a path before the watcher
            # reports the change that claim explains.
            yield AssistantMessage(
                [ToolUseBlock("Write", {"file_path": WRITE_TARGET_NAME}, call_id)]
            )
            result, failed = self._write_a_file()
            yield UserMessage([ToolResultBlock(call_id, result, is_error=failed)])
        if REFUSED_WRITE_TRIGGER in lowered:
            # Announced exactly like the real thing, settled as an error, and
            # nothing touches disk: the claim must not survive its own failure.
            call_id = f"fake-tool-{uuid.uuid4().hex[:8]}"
            yield AssistantMessage(
                [ToolUseBlock("Write", {"file_path": REFUSED_WRITE_TARGET_NAME}, call_id)]
            )
            yield UserMessage([ToolResultBlock(call_id, REFUSED_WRITE_ERROR, is_error=True)])
        if PERMISSION_TRIGGER in lowered:
            allowed = await self._bridge.ask_permission("Bash", {"command": PERMISSION_COMMAND})
            yield _delta(f"\n\npermission: {'allowed' if allowed else 'denied'}\n")
        if PLAN_TRIGGER in lowered:
            response = await self._bridge.present_plan(fake_plan())
            yield _delta(plan_echo(response))
        if VISUAL_TRIGGER in lowered:
            response = await self._bridge.present_plan(fake_visual_plan())
            yield _delta(plan_echo(response))
        if USAGE_TRIGGER in lowered:
            # Interleaved with the reply exactly as the real thing is: a
            # rate-limit transition arrives mid-stream, not at the end of a turn.
            for event in fake_rate_limit_events(self._session_id):
                yield event
        yield ResultMessage(self._session_id)

    def _read_a_file(self) -> list[Any]:
        """A tool-use note and its result, as two separate messages — the same
        two frames a real Read produces, so each chat row settles on its own."""
        call_id = f"fake-tool-{uuid.uuid4().hex[:8]}"
        target = first_workspace_file(self._folder)
        if target is None:
            return [
                AssistantMessage([ToolUseBlock("Read", {"file_path": "(none)"}, call_id)]),
                UserMessage([ToolResultBlock(call_id, "no readable file here", is_error=True)]),
            ]
        try:
            excerpt = target.read_text(encoding="utf-8", errors="replace")[:READ_EXCERPT_CHARS]
            failed = False
        except OSError as err:
            excerpt = f"cannot read: {err.strerror or err}"
            failed = True
        return [
            AssistantMessage([ToolUseBlock("Read", {"file_path": target.name}, call_id)]),
            UserMessage([ToolResultBlock(call_id, excerpt, is_error=failed)]),
        ]

    def _write_a_file(self) -> tuple[str, bool]:
        """Really write to disk — the whole point of the trigger is that the
        watcher sees a change nobody asked the *editor* for. Returns the tool
        result text and whether it failed."""
        self._writes += 1
        target = self._folder / WRITE_TARGET_NAME
        try:
            target.write_text(WRITE_BODY.format(revision=self._writes), encoding="utf-8")
        except OSError as err:
            return f"cannot write: {err.strerror or err}", True
        return f"wrote {WRITE_TARGET_NAME}", False

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True


def fake_client_factory() -> ClientFactory:
    """The factory ``main.py`` wires in instead of ``sdk_client_factory``."""

    def factory(folder: Path, resume_session_id: str | None, bridge: SessionBridge) -> SdkClient:
        log.info("agent.fake_client", folder=str(folder), resume=resume_session_id)
        return FakeAgentClient(folder, bridge)

    return factory
