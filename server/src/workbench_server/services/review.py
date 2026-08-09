"""The adversarial review — M6 staged review, PR 2's ``ValidationCheck``.

PR 1 proved a change *builds*. This one asks whether it is **right**: it puts a
**fresh-context** agent session in front of the subject session's diff with one
instruction — *try to prove this is wrong* — and turns what it finds into
evidence. Everything downstream of a check is the #82 frame's and is untouched:
risk derivation, the gallery, the bus event, the replay, and the one mandatory
human approval.

**Evidence, never authority.** There is no call in this module that can record
an approval, and ``test_review.py`` asserts that two ways rather than leaving it
to review. An agent that could commission a reviewer *and* count its verdict as
approval has quietly become its own merge queue. For the same reason PR 2 ships
**no agent-facing tool that starts a review**: a review is started by a human, by
``POST /api/validation/run``, or by an objective's spec. A session that could
commission its own reviewer could also loop on it, and the money that pays for
that loop is the user's.

Four decisions carry this check, and each is code rather than a comment:

**1. The spawn seam, not ``fork_session``.** The SDK ships ``fork_session`` and
it is the wrong primitive here, for the feature's own reason: a fork starts from
the implementer's transcript, so the reviewer inherits the reasoning — including
the self-justifications and the claim that the tests pass — that it is supposed
to be checking. That is not a fresh context, it is the same context with a new
name. It is also invisible to ``WORKBENCH_FAKE_AGENT`` (an SDK/CLI-level call,
while fake mode swaps the *client factory*), so CI could not drive it. This
check uses ``SessionManager.create_at`` — the #63 seam, built for "a session in a
folder the **server** chose" — and lands inside the fleet's activity, usage,
permission and cap machinery for free. ``fork_session`` is re-filed rather than
discarded: it is right for "branch this conversation at message N", a different
feature.

**2. The diff includes untracked files.** ``git diff <base>`` cannot see a file
git has never heard of, so an agent that wrote three brand-new modules would be
reviewed **as having changed nothing** — a silent green of exactly the kind this
milestone exists to refuse. :func:`build_diff` therefore also reads
``git ls-files --others --exclude-standard`` and appends those files' contents.
``test_review.py`` has the regression test.

**3. The reviewer is told what it was not shown.** The diff is bounded, and the
truncation is stated *in the reviewer's own prompt* ("12 of 31 files shown …").
The AXI shapes are not only for tools: a reviewer that does not know it was
handed a slice will report absence as evidence of absence.

**4. A ceiling on every way this can cost or hang.** Turns and dollars ride
``ClaudeAgentOptions`` (``max_turns`` / ``max_budget_usd``, both previously
unused here), and a wall clock bounds the whole thing. Every one of them, when
it binds, produces a ``fail`` line **naming the setting that raises it** — never
an absence a reader could mistake for a clean review. A caller's own ceilings may
only *lower* the configured ones: starting a review takes no per-run human
approval, so honouring a spec that asked for the schema's maximum would let one
API call spend fifty times what the operator agreed to.

The isolation that makes "read-only" true — a toolset selected by kind, a
kind-gated auto-allow list, ``disallowed_tools``, and a deny-and-log answer on
*both* escalation paths — is not here. It is in ``services/sdk_factory.py`` and
``services/permission_broker.py``, because that is where the session is built,
and it is asserted in ``test_sdk_factory.py``.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import BaseModel, ValidationError

from workbench_server.models.gates import SlotRef
from workbench_server.models.review import (
    MAX_DIFF_BYTES,
    MAX_FILE_DIFF_BYTES,
    SEVERITY_OUTCOME,
    ReportFindingsRequest,
    ReviewReport,
    ReviewSpec,
)
from workbench_server.models.validation import CheckOutcome, EvidenceItem, EvidenceTruncation
from workbench_server.services.agent_sessions import (
    AgentSession,
    SessionManager,
    TooManySessionsError,
)
from workbench_server.services.validation import ValidationContext
from workbench_server.services.worktrees import GIT_TIMEOUT_S, GitError, GitRunner, run_git

log = structlog.get_logger()

#: Env the settings knobs name, quoted in every refusal that would fix them.
#: Spelled out rather than derived from the field names, the
#: ``services/orchestrator.py`` precedent: these are strings a person types into
#: a shell, and a derived one that drifted would be worse than no hint at all.
#: ``test_review.py`` asserts each names a real setting.
SETTING_TIMEOUT = "WORKBENCH_REVIEW_TIMEOUT_S"
SETTING_MAX_TURNS = "WORKBENCH_REVIEW_MAX_TURNS"
SETTING_MAX_BUDGET = "WORKBENCH_REVIEW_MAX_BUDGET_USD"
SETTING_MAX_SESSIONS = "WORKBENCH_MAX_CONCURRENT_SESSIONS"

#: How long one review may take, wall clock, before it is abandoned. Distinct
#: from the turn and dollar caps: those bound what the reviewer *does*, this
#: bounds how long the validation that started it will wait.
DEFAULT_REVIEW_TIMEOUT_S = 600.0

#: Default ceilings on the reviewer's own spend. Modest on purpose — this is the
#: second capability in the app that can spend money with no human answering each
#: step, and the out-of-the-box numbers should be ones a mistake survives.
DEFAULT_MAX_TURNS = 24
DEFAULT_MAX_BUDGET_USD = 2.0

#: How a per-file section is introduced when the file is untracked. ``git diff``
#: has no header for a file it does not know about, so we mint one that reads
#: like the rest of the document rather than pasting bare contents the reviewer
#: cannot attribute.
_UNTRACKED_HEADER = "diff --git a/{path} b/{path}\nnew file (untracked, not yet staged)\n--- /dev/null\n+++ b/{path}\n"  # noqa: E501


# ---- the seams ----------------------------------------------------------------


class SlotLocator(Protocol):
    """Which checkout is this session writing in? ``None`` when it holds none.

    The same seam ``services/gates.py`` takes, implemented by
    ``OrchestratorService.slot_of`` and injected so this module never imports the
    orchestrator. Read-only: the reviewer **takes no lease**, because "one writer
    per checkout" is intact when the second reader is not a writer.
    """

    def slot_of(self, session_id: str) -> SlotRef | None: ...


#: Turns and dollars a reviewer has spent, by session id. The ``UsageService``
#: narrowed to the one question this module asks, so the figures in a review
#: report are the same accumulator Mission Control's board renders rather than a
#: second one that could disagree with it.
SpendReader = Callable[[str], tuple[int, float]]


@dataclass
class _Pending:
    """One in-flight review, waiting for its reviewer to report.

    ``settled`` is the event the check waits on and ``receive_findings`` sets. A
    report that arrives twice keeps the **first**: a reviewer that calls the tool
    again mid-turn has changed its mind after the fact, and the check has already
    told it the report was taken.
    """

    settled: asyncio.Event
    report: ReportFindingsRequest | None = None
    turn_error: str | None = None


@dataclass
class _Diff:
    """The bounded slice of a change the reviewer is shown."""

    text: str
    files_shown: int
    files_total: int
    bytes_shown: int
    bytes_total: int
    head: str
    base: str
    truncated: EvidenceTruncation | None = None


@dataclass
class _Section:
    """One file's contribution to the diff, already per-file bounded."""

    path: str
    text: str
    clipped: bool = False


# ---- the diff -----------------------------------------------------------------


def split_sections(diff_text: str) -> list[_Section]:
    """Split ``git diff`` output into one section per file.

    Split on ``diff --git`` rather than parsed: this text is handed to a *model*,
    not applied as a patch, so the only structure that matters is where one file
    ends and the next begins. Anything before the first header (there is nothing
    in practice) is kept as its own section rather than dropped, because silently
    losing bytes is the failure mode this whole module is about.
    """
    sections: list[_Section] = []
    current: list[str] = []
    path = ""
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                sections.append(_Section(path=path or "(preamble)", text="".join(current)))
            current = [line]
            path = _path_of(line)
            continue
        current.append(line)
    if current:
        sections.append(_Section(path=path or "(preamble)", text="".join(current)))
    return sections


def _path_of(header: str) -> str:
    """``b/server/x.py`` out of ``diff --git a/server/x.py b/server/x.py``."""
    parts = header.split()
    if len(parts) < 4:
        return "(unknown)"
    candidate = parts[3]
    return candidate[2:] if candidate.startswith("b/") else candidate


def _clip_section(section: _Section, cap: int, *, unread_bytes: int = 0) -> _Section:
    """One file's diff, cut to ``cap`` bytes with the cut named in the text.

    Marked inline and not only in the report's ``truncated``, for the reason the
    gate's log marker exists: a reader must never have to know that two adjacent
    lines were never adjacent.

    ``unread_bytes`` is for the one caller that stops reading before it has the
    whole file (:func:`_read_untracked`): the bytes it never loaded are added to
    the count so the sentence states the file's *real* remainder rather than the
    one byte the overshoot happened to see. Saying "1 more byte" of a 2 GB
    artifact is AXI shape 1 violated with a number attached.
    """
    encoded = section.text.encode()
    if len(encoded) <= cap and not unread_bytes:
        return section
    withheld = max(len(encoded) - cap, 0) + unread_bytes
    kept = encoded[:cap].decode(errors="ignore")
    return _Section(
        path=section.path,
        text=f"{kept}\n… {withheld:,} more bytes of this file were not shown …\n",
        clipped=True,
    )


async def build_diff(
    git: GitRunner,
    path: Path,
    base: str,
    *,
    max_bytes: int = MAX_DIFF_BYTES,
    per_file_bytes: int = MAX_FILE_DIFF_BYTES,
) -> _Diff | None:
    """Everything this session changed, bounded, or ``None`` when git will not say.

    Two reads, and the second is the one that makes this correct rather than
    plausible:

    * ``git diff <base>`` — everything committed and uncommitted against the
      commit the slot was leased at, in one read;
    * ``git ls-files --others --exclude-standard`` — every **untracked** file,
      whose contents are appended with a minted header. Without this an agent
      that wrote three brand-new modules reviews as having changed nothing.
      ``.gitignore`` is honoured (``--exclude-standard``), so a slot's own
      ``.venv`` or ``node_modules`` never enters the window.

    ``None`` is not "clean" and a caller must not read it as one — the gate's
    ``_fingerprint`` rule, one module over: a checkout whose state git will not
    report is one no verdict may be attributed to.
    """
    try:
        head_result = await git(path, ("rev-parse", "HEAD"), GIT_TIMEOUT_S)
        # An empty ``base`` means the pool could not say what the slot was leased
        # at. ``HEAD`` is the honest fallback — it reviews the uncommitted work,
        # which is strictly less than the lease's diff and never more, and the
        # report says which commit it used.
        against = base or "HEAD"
        tracked = await git(
            path, ("--no-optional-locks", "diff", against, "--find-renames"), GIT_TIMEOUT_S
        )
        untracked = await git(
            path,
            ("--no-optional-locks", "ls-files", "--others", "--exclude-standard", "-z"),
            GIT_TIMEOUT_S,
        )
    except GitError as err:
        log.warning("review.diff_failed", path=str(path), detail=str(err))
        return None
    refused = next((result for result in (head_result, tracked, untracked) if not result.ok), None)
    if refused is not None or not head_result.out:
        log.warning(
            "review.diff_failed",
            path=str(path),
            detail=(refused or head_result).first_error_line(),
        )
        return None
    head = head_result.out.splitlines()[0].strip()

    sections = [_clip_section(s, per_file_bytes) for s in split_sections(tracked.out)]
    names = [name for name in untracked.out.split("\0") if name]
    new_sections = await asyncio.to_thread(_read_untracked, path, names, per_file_bytes)
    sections += new_sections

    # Selection: source order, spending the budget on breadth. The per-file cap
    # above is what stops one generated file crowding the rest out, so there is
    # nothing left for a reordering to buy — and a stable order is what lets two
    # runs over the same tree be compared.
    kept: list[_Section] = []
    used = 0
    total = 0
    for section in sections:
        size = len(section.text.encode())
        total += size
        if used + size <= max_bytes:
            kept.append(section)
            used += size
    truncated: EvidenceTruncation | None = None
    if len(kept) < len(sections):
        truncated = EvidenceTruncation(
            shown=len(kept),
            total=len(sections),
            detail=(
                f"{len(kept)} of {len(sections)} changed files shown "
                f"({used:,} of {total:,} bytes); the rest were withheld to bound "
                "the review's context"
            ),
        )
    return _Diff(
        text="".join(section.text for section in kept),
        files_shown=len(kept),
        files_total=len(sections),
        bytes_shown=used,
        bytes_total=total,
        head=head,
        base=base or head,
        truncated=truncated,
    )


def _decode_untracked(raw: bytes, *, complete: bool) -> str | None:
    """``raw`` as text, or ``None`` when these bytes are not text at all.

    Two different questions used to be answered by one strict ``bytes.decode``,
    and conflating them cost real source files their review. A file read up to a
    **byte** ceiling is almost never cut on a character boundary, so any new
    module whose 40,000th byte landed inside a multi-byte character — an em dash
    is three of them, and this codebase is full of them — raised
    ``UnicodeDecodeError`` and was filed as ``(new binary file, not shown)``. The
    reviewer then saw not one byte of an ordinary Python file, and the report
    called the omission *binary* rather than *truncation*: a silent green with a
    plausible-looking label on it, arriving through the check written to stop
    exactly that.

    So the two questions are now asked separately:

    * **Is this binary?** A NUL byte anywhere in what was read — git's own
      heuristic (``buffer_is_binary``), and one that does not care where the read
      stopped.
    * **Where does the text end?** An incremental decoder, ``final=False`` when
      the read was cut short, which *holds back* an incomplete trailing sequence
      instead of raising on it. Genuinely invalid bytes still raise, and still
      mean binary — the classification survives, it just stops firing on a cut.
    """
    if b"\0" in raw:
        return None
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        return decoder.decode(raw, final=complete)
    except UnicodeDecodeError:
        return None


def _read_untracked(root: Path, names: Sequence[str], cap: int) -> list[_Section]:
    """The untracked files' contents, bounded, as diff-shaped sections.

    Blocking IO throughout, so callers run it in a thread. A file that is not
    text is named and **not** included: handing a model a megabyte of
    replacement characters buys nothing and spends the window a source file
    needed. A file that cannot be read at all is named too — "there is a new file
    here I could not show you" is a fact a reviewer should have. A file that is
    text but longer than the cap is *truncated and said to be truncated*, which
    is a third outcome and must never be reported as either of the first two.
    """
    sections: list[_Section] = []
    for name in names:
        target = root / name
        header = _UNTRACKED_HEADER.format(path=name)
        try:
            # ``stat`` before the read, so the truncation sentence can state the
            # true remainder; the read itself stops one byte past the cap, which
            # is all it takes to know the cap bit and is what keeps a checked-in
            # 2 GB artifact costing the cap in memory rather than 2 GB.
            size = target.stat().st_size
            with target.open("rb") as handle:
                raw = handle.read(cap + 1)
        except OSError as err:
            sections.append(
                _Section(path=name, text=f"{header}(new file, unreadable: {err.strerror or err})\n")
            )
            continue
        complete = len(raw) <= cap
        if not complete:
            raw = raw[:cap]
        text = _decode_untracked(raw, complete=complete)
        if text is None:
            sections.append(
                _Section(
                    path=name,
                    text=f"{header}(new binary file, {size:,} bytes, not shown)\n",
                )
            )
            continue
        body = "".join(f"+{line}\n" for line in text.splitlines())
        sections.append(
            _clip_section(
                _Section(path=name, text=header + body),
                cap,
                unread_bytes=0 if complete else max(size - cap, 0),
            )
        )
    return sections


# ---- the brief ----------------------------------------------------------------

#: The reviewer's instruction. **Fixed and server-owned**: a caller may append a
#: bounded ``focus`` sentence and may not replace any of this.
#:
#: Refute-first is stated three times in three ways on purpose — as the job, as
#: the shape of a finding, and as the rule that demotes an unsupported claim —
#: because "review this" reliably produces a summary of the change, which is the
#: single least useful thing a second model can do with a diff.
REVIEW_BRIEF = """\
You are reviewing a change someone else just wrote. You have never seen it \
before and you have no stake in it being correct.

Your job is to REFUTE this change, not to summarise it. Do not describe what it \
does. Do not compliment it. Look for the input, the state, the ordering or the \
concurrency under which it does the wrong thing.

Rules for a finding:
- It must name the concrete thing that breaks it: an input value, a sequence of \
calls, a boundary, an empty collection, a timezone or DST transition, a unit \
mismatch, a retry, a partial failure. "This looks fragile" is not a finding.
- If you cannot name that concrete failure path, the severity is "nit". \
must_fix and should_fix are for things you can show going wrong.
- Prefer few, sharp, checkable findings over many vague ones.

You are read-only. You cannot edit, write or run anything, and no human is \
watching this session, so asking for permission will only waste a turn. You may \
read files in this checkout to check a claim before you make it.

When you are done, call report_findings exactly once. If the change holds up, \
call it with an empty findings list — that is a real answer and it is recorded \
as one.
"""


#: Where the server's instructions stop and the change begins in
#: :func:`review_prompt`. Exported because a reader of the prompt has to be able
#: to tell the two apart: ``services/fake_agent.py``'s scripted reviewer keys its
#: script off the caller's ``focus``, and scanning the whole prompt for a trigger
#: would let the *diff under review* decide what the reviewer reports.
DIFF_MARKER = "\n--- the change ---\n"


def review_prompt(diff: _Diff, focus: str) -> str:
    """The brief, what was withheld, the caller's focus, and the diff.

    The truncation sentence is inside the *prompt*, not only in the report: a
    reviewer that does not know it was shown 12 of 31 files will report the
    absence of a problem in the other 19 as evidence there is none.
    """
    parts = [REVIEW_BRIEF, ""]
    scope = (
        f"Scope: {diff.files_shown} changed file(s), {diff.bytes_shown:,} bytes of diff, "
        f"against {diff.base[:12] or 'the working tree'}."
    )
    parts.append(scope)
    if diff.truncated is not None:
        parts.append(
            f"NOTE — you are being shown a slice: {diff.truncated.detail}. "
            "Do not treat a file you were not shown as unchanged, and say so in a "
            "finding if the part you were shown cannot be judged without it."
        )
    if focus:
        parts.append(f"The person who asked for this review added: {focus}")
    parts.append(DIFF_MARKER)
    parts.append(diff.text)
    return "\n".join(parts)


# ---- the check ----------------------------------------------------------------


class AdversarialReviewCheck:
    """The registered check. ``id`` is the handle a ``ValidationSpec`` names.

    Also the :class:`~workbench_server.services.agent_tools.FindingsReceiver` a
    reviewer's ``report_findings`` call lands on — one object, so the review that
    commissioned a session is the only thing its report can settle.
    """

    id = "review"

    def __init__(
        self,
        locator: SlotLocator,
        *,
        git: GitRunner = run_git,
        timeout_s: float = DEFAULT_REVIEW_TIMEOUT_S,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
        spend: SpendReader | None = None,
    ) -> None:
        self._locator = locator
        self._git = git
        self._timeout_s = timeout_s
        self._max_turns = max_turns
        self._max_budget_usd = max_budget_usd
        self._spend = spend
        self._sessions: SessionManager | None = None
        self._pending: dict[str, _Pending] = {}
        # The ceilings the *next* reviewer session is built with, parked here for
        # the client factory to read. A spec may name its own, so this cannot be
        # captured once at construction — see ``sdk_client_factory``.
        self._caps: tuple[int, float] = (max_turns, max_budget_usd)
        # One review at a time per check instance. Not a throughput decision: the
        # caps above are per-session, and two concurrent reviews would each see
        # the other's parked ceilings. The lock makes "parked just before the
        # spawn" true rather than merely likely.
        self._lock = asyncio.Lock()

    def bind(self, sessions: SessionManager) -> None:
        """The other half of the construction cycle in ``main.py``.

        The client factory closes over this check (it is the ``FindingsReceiver``)
        and the session manager is built *from* that factory, so the manager
        cannot be a constructor argument. The orchestrator's ``bind`` solves the
        identical cycle for the identical reason.
        """
        self._sessions = sessions

    def reviewer_caps(self) -> tuple[int, float]:
        """Turns and dollars for the reviewer session about to be created."""
        return self._caps

    # ---- FindingsReceiver ---------------------------------------------------

    def receive_findings(self, session_id: str, report: ReportFindingsRequest) -> str | None:
        """A reviewer's report, landing on the review that commissioned it.

        ``None`` when nothing is waiting on this session — which the tool turns
        into an error the model reads, rather than an acknowledgement of a report
        that went nowhere. That is the one failure mode of ``report_findings``
        that could otherwise look like a clean review.
        """
        pending = self._pending.get(session_id)
        if pending is None:
            log.warning("review.findings_unclaimed", session=session_id)
            return None
        if pending.report is not None:
            # Taken already. Said plainly rather than overwritten: a second call
            # is a reviewer revising after the fact, and the first answer is the
            # one the check is going to use.
            return "Your findings were already recorded; this second report was not taken."
        pending.report = report
        pending.settled.set()
        log.info("review.findings_received", session=session_id, findings=len(report.findings))
        return (
            f"Recorded {len(report.findings)} finding(s). A human reviews them and decides; "
            "nothing is approved by this call. You are done — stop here."
        )

    # ---- the check ----------------------------------------------------------

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        try:
            spec = ReviewSpec.model_validate(ctx.params)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "params"
            return [_refusal(f"invalid review spec: {where} — {first['msg']}.")]

        if ctx.subject.kind != "session_output":
            return [
                _refusal(
                    "a review reads a session's own diff, so the subject must be a session "
                    f"output — this one is {ctx.subject.kind!r}."
                )
            ]
        session_id = ctx.subject.ref
        slot = self._locator.slot_of(session_id)
        if slot is None:
            return [
                _refusal(
                    "this session holds no worktree slot, so there is no diff to review. A "
                    "review reads the checkout the session writes in, never the live "
                    "workspace root — that would review the user's unsaved edits as though "
                    "the agent had made them. Start the work as a Mission Control worker "
                    "and review that session."
                )
            ]
        path = Path(slot.path)
        if not await asyncio.to_thread(path.is_dir):
            return [
                _refusal(
                    f"the checkout this session was working in is gone ({slot.path}) — its "
                    "slot was released or pruned. Re-run once the session has one again."
                )
            ]
        if self._sessions is None:
            return [_refusal("the review service is not wired up in this server")]

        diff = await build_diff(self._git, path, slot.base)
        if diff is None:
            return [
                _refusal(
                    f"git could not read {slot.slot or slot.path} — a checkout whose state "
                    "cannot be vouched for is not one a review may be attributed to."
                )
            ]
        if diff.files_total == 0:
            # Explicitly "none", never blankness (AXI shape 2). ``skipped`` and
            # not ``pass``: nothing was judged, and a green line here would be
            # the silent green in its purest form.
            return [
                EvidenceItem(
                    kind="diff",
                    label="adversarial review",
                    outcome="skipped",
                    detail=(
                        f"nothing to review — this session's checkout is identical to "
                        f"{diff.base[:12]}, with no uncommitted and no untracked changes."
                    ),
                )
            ]

        async with self._lock:
            return await self._review(ctx, spec, slot, path, diff)

    async def _review(
        self,
        ctx: ValidationContext,
        spec: ReviewSpec,
        slot: SlotRef,
        path: Path,
        diff: _Diff,
    ) -> list[EvidenceItem]:
        """Spawn the reviewer, wait for it, and turn the answer into evidence."""
        sessions = self._sessions
        assert sessions is not None  # noqa: S101 — guarded by the caller
        # **The configured ceilings are ceilings, not defaults.** A spec may ask
        # for less; asking for more is clamped, because starting a review takes
        # no per-run human approval and ``ReviewSpec``'s own schema bound is 100
        # turns / $100 — fifty times what an operator who set $2 "so a mistake
        # survives" agreed to spend. Clamped rather than refused so an over-ask
        # still gets a review, and said out loud rather than clamped quietly:
        # a caller who does not learn its number was lowered will read a
        # truncated review as a complete one.
        asked_turns = spec.max_turns or self._max_turns
        asked_budget = spec.max_budget_usd or self._max_budget_usd
        turns_cap = min(asked_turns, self._max_turns)
        budget_cap = min(asked_budget, self._max_budget_usd)
        caps_note = ""
        if asked_turns > turns_cap or asked_budget > budget_cap:
            log.warning(
                "review.caps_clamped",
                asked_turns=asked_turns,
                asked_budget_usd=asked_budget,
                turns=turns_cap,
                budget_usd=budget_cap,
            )
            caps_note = (
                f"The request asked for {asked_turns} turns / ${asked_budget:.2f}; this "
                f"server allows at most {turns_cap} / ${budget_cap:.2f} and the review ran "
                f"under those — raise {SETTING_MAX_TURNS} or {SETTING_MAX_BUDGET} to spend more."
            )
        self._caps = (turns_cap, budget_cap)

        label = f"review of {slot.slot or path.name}"
        try:
            session = sessions.create_at(path, label, kind="reviewer")
        except TooManySessionsError:
            # A cap that is hit renders as the cap and the way out, never a dead
            # button — the ``SpawnRefusal`` idiom, and the reason this is
            # ``skipped`` rather than ``fail``: nothing was judged.
            log.info("review.session_cap", limit=sessions.max_concurrent)
            return [
                _refusal(
                    f"no session slot was free to run the reviewer — "
                    f"{sessions.active_count()} of {sessions.max_concurrent} are working. "
                    f"Raise {SETTING_MAX_SESSIONS}, or re-run when the fleet is quieter."
                )
            ]

        reviewer_id = session.session_id
        pending = _Pending(settled=asyncio.Event())
        self._pending[reviewer_id] = pending
        # **Subscribed before the prompt is sent, synchronously.** Not a style
        # choice: ``asyncio.create_task`` only *queues* the watcher, so a task
        # that did its own ``subscribe()`` would not have run yet when
        # ``send_user_message`` starts the turn — and ``AgentSession._emit``
        # delivers to the listeners that exist *at that moment*. A reviewer whose
        # whole turn landed in that gap would leave this check waiting out its
        # full wall clock for frames that were already dropped, and report a
        # timeout for a review that actually finished. Today's ordering makes
        # that unlikely rather than impossible; taking the queue here makes it
        # impossible. (Found by ``test_a_reviewer_that_errored_names_the_ceilings``,
        # which is why that test asserts on the *error text* and not merely on
        # the outcome.)
        queue = session.subscribe()
        watcher = asyncio.create_task(self._watch(reviewer_id, queue, pending))
        timed_out = False
        try:
            session.send_user_message(review_prompt(diff, spec.focus))
            async with asyncio.timeout(self._timeout_s):
                await pending.settled.wait()
        except TimeoutError:
            timed_out = True
            log.warning("review.timed_out", session=reviewer_id, timeout_s=self._timeout_s)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            session.unsubscribe(queue)
            self._pending.pop(reviewer_id, None)
            await self._retire(sessions, session)

        turns, cost = self._spend(reviewer_id) if self._spend is not None else (0, 0.0)
        if pending.report is None:
            return [
                _with_note(
                    _failed_review(
                        diff,
                        reviewer_id,
                        timed_out=timed_out,
                        turn_error=pending.turn_error,
                        timeout_s=self._timeout_s,
                        turns_cap=turns_cap,
                        budget_cap=budget_cap,
                    ),
                    caps_note,
                )
            ]

        report = ReviewReport(
            base=diff.base,
            head=diff.head,
            files_reviewed=diff.files_shown,
            diff_bytes=diff.bytes_shown,
            findings=list(pending.report.findings),
            truncated=diff.truncated,
            reviewer_session_id=reviewer_id,
            turns=turns,
            cost_usd=cost,
        )
        log.info(
            "review.completed",
            session=reviewer_id,
            subject=ctx.subject.ref,
            findings=len(report.findings),
            files=report.files_reviewed,
        )
        return [_with_note(_evidence(report, ctx, note=pending.report.note), caps_note)]

    @staticmethod
    async def _watch(reviewer_id: str, queue: asyncio.Queue[BaseModel], pending: _Pending) -> None:
        """Drain the reviewer's own frames until its turn ends.

        A reviewer has no WebSocket — nobody opened a chat for it — so this is
        the only reader of its frames, the ``_pump`` shape from
        ``services/orchestrator.py``. It exists for the case that matters most:
        a turn that ends **without** ``report_findings`` having been called.
        ``AgentSession._run_turn`` turns an exception into ``agent_error`` plus
        ``turn_done``, so from out here a reviewer that died looks exactly like
        one that finished until these frames are read — and a check that waited
        on the event alone would hang until its wall clock instead of saying what
        went wrong.

        The queue is handed in already subscribed; see the caller for why that
        is not a refactor but the fix to a dropped-frame race.
        """
        try:
            while True:
                event = await queue.get()
                kind = getattr(event, "type", "")
                if kind == "agent_error":
                    pending.turn_error = str(getattr(event, "message", "the turn failed"))
                elif kind == "turn_done":
                    if pending.report is None and pending.turn_error is None:
                        pending.turn_error = (
                            "the reviewer's turn ended without calling report_findings"
                        )
                    # Wakes the check whether or not a report arrived: a turn
                    # that is over is over, and waiting out the wall clock for a
                    # reviewer that has stopped talking is a ten-minute stall
                    # with a known answer.
                    pending.settled.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # a reader that dies must not take the review with it
            log.exception("review.watch_failed", session=reviewer_id)

    @staticmethod
    async def _retire(sessions: SessionManager, session: AgentSession) -> None:
        """Close the reviewer and take it out of the fleet.

        A reviewer is a *disposable* session: it exists for one turn against one
        diff, and leaving it in the manager would hold a slot against
        ``WORKBENCH_MAX_CONCURRENT_SESSIONS`` for a conversation nobody can open.
        Interrupt first so a call parked on anything unwinds, then close — the
        orchestrator's ``_retire`` order, and for the same reason.
        """
        with contextlib.suppress(Exception):
            await session.interrupt()
        with contextlib.suppress(Exception):
            await session.close()
        sessions.sessions.pop(session.session_id, None)


# ---- evidence -----------------------------------------------------------------


def _evidence(report: ReviewReport, ctx: ValidationContext, note: str = "") -> EvidenceItem:
    """**One grouped** ``diff`` line, outcome = the worst severity found.

    Grouped, which is the opposite of what the toolchain gate does and right for
    the opposite reason: four gates are four independent questions a reader wants
    side by side, whereas a review is one question with supporting detail — and
    the detail belongs in the payload the gallery's expander redeems.

    A review that found nothing and a review that never ran must never look
    alike (AXI shape 2), so the clean line says what was reviewed rather than
    saying nothing.
    """
    worst = report.worst()
    outcome: CheckOutcome = "pass" if worst is None else SEVERITY_OUTCOME[worst]
    ref = ctx.store_payload("diff", report)
    kb = report.diff_bytes / 1024
    scope = f"{report.files_reviewed} file(s), {kb:.1f} KB of diff"
    if worst is None:
        detail = f"No findings — {scope} reviewed against {report.base[:12]}."
    else:
        counts = ", ".join(f"{count} {name}" for name, count in report.counts().items())
        detail = f"{len(report.findings)} finding(s) ({counts}) over {scope}."
    if report.truncated is not None:
        detail = f"{detail} {report.truncated.detail}."
    if note:
        detail = f"{detail} Reviewer noted: {note}"
    return EvidenceItem(
        kind="diff",
        label="adversarial review",
        outcome=outcome,
        detail=detail,
        payload_ref=ref,
    )


def _failed_review(
    diff: _Diff,
    reviewer_id: str,
    *,
    timed_out: bool,
    turn_error: str | None,
    timeout_s: float,
    turns_cap: int,
    budget_cap: float,
) -> EvidenceItem:
    """A review that produced no findings **because it could not**.

    ``fail``, never ``pass`` and never silence: a reviewer that timed out, blew
    its budget or died is an absence, and an absence read as approval is the one
    outcome this milestone exists to make impossible. Every ceiling that could
    have caused it is named with the setting that raises it.
    """
    if timed_out:
        why = (
            f"the reviewer did not finish within {timeout_s:.0f}s and was stopped — "
            f"raise {SETTING_TIMEOUT}"
        )
    elif turn_error:
        why = (
            f"the reviewer stopped without reporting: {turn_error}. If it ran out of room, "
            f"raise {SETTING_MAX_TURNS} (now {turns_cap} turns) or {SETTING_MAX_BUDGET} "
            f"(now ${budget_cap:.2f})"
        )
    else:
        why = "the reviewer produced no report"
    return EvidenceItem(
        kind="diff",
        label="adversarial review",
        outcome="fail",
        detail=(
            f"the change was NOT reviewed — {why}. "
            f"{diff.files_shown} file(s) were prepared for review; no findings were "
            "collected, and this is recorded as a failure rather than as a clean "
            f"review. Reviewer session {reviewer_id}."
        ),
    )


def _with_note(item: EvidenceItem, note: str) -> EvidenceItem:
    """The same line with a *server-side* caveat appended, or the line unchanged.

    Separate from the reviewer's own ``note`` on purpose: that field is what the
    model said about its coverage, and folding the server's clamp message into it
    would attribute the server's sentence to the reviewer. The outcome is never
    touched — a clamped ceiling changes what a review cost, not what it found.
    """
    if not note:
        return item
    return item.model_copy(update={"detail": f"{item.detail} {note}"})


def _refusal(detail: str) -> EvidenceItem:
    """One ``skipped`` line. Refusing is the feature — a fallback would be the
    bug — and a refusal that says why and how to fix it is not an absence."""
    return EvidenceItem(kind="diff", label="adversarial review", outcome="skipped", detail=detail)
