"""The adversarial review's typed inputs and outputs — M6 staged review, PR 2.

PR 1 proved that a change *builds*: ruff, mypy, pytest, npm-test, each one an
:class:`~workbench_server.models.validation.EvidenceItem` with its captured log.
A green toolchain is necessary and nowhere near sufficient — it says the code
runs, not that it is right. This check closes that half: a **fresh-context**
agent session reads the subject session's diff with one instruction, *try to
prove this is wrong*, and what it finds becomes evidence a human approves.

Three properties live in the schema rather than in a comment, because each is a
promise a later edit could quietly break:

* **A finding must name the input that breaks the change.**
  :attr:`ReviewFinding.refutation` is a *required, non-empty* field, not an
  optional elaboration on :attr:`~ReviewFinding.claim`. That is the whole
  refute-first posture expressed where it binds: a reviewer that cannot say
  which input or state fails has an opinion, and an opinion is a ``nit``. Making
  it required means a model that tries to file a bare assertion as ``must_fix``
  gets a validation error it can read and fix, rather than a `must_fix` line a
  human has to go and disprove.
* **Prompt text is not argv.** :attr:`ReviewSpec.focus` is bounded free text
  appended to a *fixed, server-owned* brief and handed to a session with no way
  to write or execute anything. ``m6-proof.md``'s "no shell in a JSON body"
  refusal binds PR 1's gate catalog absolutely — a caller names a gate, never a
  command — and the distinction here is exactly that: this is a sentence for a
  read-only reader, not a string that reaches a process.
* **Nothing in this module can record an approval.** There is no approval field,
  no verdict field and no "ok to merge" flag anywhere below. A review produces
  findings; the human approval gate stays the sole decider, and
  ``test_review.py`` asserts that rather than leaving it to review.

Only :class:`ReviewReport` is a wire body, and only through the payload envelope
in ``models/evidence.py``. :class:`ReviewSpec` travels *inside*
``ValidationSpec.params`` (a ``dict`` on the wire) and
:class:`ReportFindingsRequest` is the ``report_findings`` tool's argument shape.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from workbench_server.models.validation import CheckOutcome, EvidenceTruncation

#: How much of a diff the reviewer is shown, in bytes.
#:
#: Sized against what it costs rather than against what a diff could be: 200 KB
#: of unified diff is a large PR and roughly 50k tokens of turn-one context. Past
#: that the marginal file buys less than it costs, and the honest answer is to
#: say a slice was shown (which the prompt does) rather than to quietly send
#: half a megabyte.
MAX_DIFF_BYTES = 200_000

#: Ceiling on any **one** file's contribution to that budget.
#:
#: The reason is not size, it is *crowding*: without a per-file cap a single
#: regenerated lockfile or checked-in bundle consumes the whole window and the
#: twelve source files that actually changed are never shown — a reviewer that
#: reports "nothing wrong" having been handed only `package-lock.json`. The cap
#: is per file so the budget is spent on breadth first.
MAX_FILE_DIFF_BYTES = 40_000

#: Ceiling on how many findings one report may carry. A reviewer that files
#: eighty findings has written a second opinion of the codebase, not a review of
#: a diff; the cap is stated back to it (AXI shape 1) rather than silently
#: dropping the tail.
MAX_FINDINGS = 40

#: Ceiling on the caller's ``focus`` sentence. Bounded because it is appended to
#: a prompt that is otherwise entirely the server's.
MAX_FOCUS_CHARS = 500

#: Per-field clips inside one finding, so ``MAX_FINDINGS`` bounds the report in
#: bytes and not only in rows.
MAX_CLAIM_CHARS = 300
MAX_REFUTATION_CHARS = 700
MAX_FINDING_PATH_CHARS = 260

#: How wrong a finding says the change is.
#:
#: ``nit`` is deliberately the floor rather than an absence: a reviewer that
#: found only style has still *reviewed*, and the difference between "nothing to
#: fix" and "nothing ran" is the difference this whole milestone exists to keep
#: visible.
ReviewSeverity = Literal["must_fix", "should_fix", "nit"]

#: How sure the reviewer is. Carried on the finding rather than folded into
#: severity because they are independent: a ``possible`` ``must_fix`` (a data-loss
#: path the reviewer could not fully trace) is worth a human's minute, and a
#: ``certain`` ``nit`` is not.
ReviewConfidence = Literal["certain", "likely", "possible"]

#: Severity → the ``EvidenceItem`` outcome one grouped review line reports. The
#: worst severity found wins; nits and an empty list are both a ``pass``, which
#: is why the detail line says which of the two it was.
#:
#: Typed as the frame's own ``CheckOutcome`` so this table cannot drift into an
#: outcome the validation frame does not have — ``derive_risk`` would raise on
#: one, and it would raise inside a check, which the service reports as the check
#: failing rather than as the typo it is.
SEVERITY_OUTCOME: dict[ReviewSeverity, CheckOutcome] = {
    "must_fix": "fail",
    "should_fix": "warn",
    "nit": "pass",
}

#: Worst-first. Used to pick the grouped line's outcome and to order a report.
SEVERITY_ORDER: tuple[ReviewSeverity, ...] = ("must_fix", "should_fix", "nit")


class ReviewFinding(BaseModel):
    """One thing the reviewer claims is wrong, and why it believes it.

    The pair of fields is the point. :attr:`claim` is what a reader scans;
    :attr:`refutation` is what makes the claim checkable — the input, the state
    or the sequence under which the change misbehaves. A finding whose
    refutation is empty is refused at validation, which is how "no failure path
    = nit" stops being a guideline and becomes a shape.
    """

    severity: ReviewSeverity
    #: Repository-relative path the finding is about; ``None`` for a claim about
    #: the change as a whole (a missing migration, an absent test).
    file: str | None = Field(default=None, max_length=MAX_FINDING_PATH_CHARS)
    #: 1-based line in ``file``. ``None`` when the finding is about the file, or
    #: about the absence of something, rather than about a line.
    line: int | None = Field(default=None, ge=1)
    #: What is wrong, in one line.
    claim: str = Field(min_length=1, max_length=MAX_CLAIM_CHARS)
    #: The input or state that breaks it — the reason to believe the claim.
    #: Required and non-empty: see the class docstring and the module's.
    refutation: str = Field(min_length=1, max_length=MAX_REFUTATION_CHARS)
    confidence: ReviewConfidence = "likely"

    @field_validator("claim", "refutation")
    @classmethod
    def _no_blank(cls, value: str) -> str:
        """Whitespace is not a refutation.

        ``min_length`` alone accepts ``" "``, and a model that has nothing to say
        will reach for exactly that when a field is required. Stripped and
        re-checked so the requirement means what it reads as.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class ReviewReport(BaseModel):
    """The payload behind a ``diff`` :class:`EvidenceItem` — one whole review.

    Grouped rather than one item per finding, which is the opposite of what the
    toolchain gate does and right for the opposite reason: four gates are four
    independent questions a reader wants side by side, whereas a review is **one**
    question ("does this hold up") with supporting detail, and the detail belongs
    in the payload where the gallery's expander redeems it.
    """

    #: The commit the subject's slot was leased at — the diff's other end.
    base: str
    #: The commit the reviewed tree was on. Equal to ``base`` for work that is
    #: entirely uncommitted, which is the common case mid-task.
    head: str
    files_reviewed: int
    diff_bytes: int
    findings: list[ReviewFinding] = Field(default_factory=list)
    #: Set when the diff or the finding list was capped (AXI shape 1). The same
    #: sentence is put in the reviewer's *own* prompt: a reviewer that does not
    #: know it was shown a slice will report absence as evidence of absence.
    truncated: EvidenceTruncation | None = None
    #: The session that produced this. Named so a reviewer's own row in the
    #: activity feed can be found from the evidence it produced.
    reviewer_session_id: str
    turns: int = 0
    cost_usd: float = 0.0

    def worst(self) -> ReviewSeverity | None:
        """The most severe finding's severity, or ``None`` for a clean review."""
        for severity in SEVERITY_ORDER:
            if any(finding.severity == severity for finding in self.findings):
                return severity
        return None

    def counts(self) -> dict[ReviewSeverity, int]:
        """Findings per severity, worst first, omitting the zeroes."""
        tally: dict[ReviewSeverity, int] = {}
        for severity in SEVERITY_ORDER:
            found = sum(1 for finding in self.findings if finding.severity == severity)
            if found:
                tally[severity] = found
        return tally


class ReviewSpec(BaseModel):
    """``ValidationSpec.params`` for check id ``"review"``.

    Every field is a *bound* or a sentence — there is no path, no cwd, no command
    and no session id here. The subject names the session; the slot is resolved
    from it server-side, exactly as the gate's is, so a caller cannot point a
    reviewer at a checkout that is not the subject's.
    """

    #: Appended to the fixed server-side brief. Bounded free text handed to a
    #: read-only session: a sentence, not an argv (see the module docstring).
    focus: str = Field(default="", max_length=MAX_FOCUS_CHARS)
    #: Ceiling on the reviewer's turns, passed to the SDK as
    #: ``ClaudeAgentOptions.max_turns``. ``None`` takes the server's configured
    #: value; a check that spends without a ceiling is one nobody leaves on.
    #:
    #: **This can only lower the server's ceiling, never raise it.** The bound
    #: here is the schema's outer limit, not a licence: ``WORKBENCH_REVIEW_MAX_TURNS``
    #: is what the operator agreed to, a review starts with no per-run human
    #: approval, and ``services/review.py`` clamps to the smaller of the two and
    #: says so on the evidence line when it did.
    max_turns: int | None = Field(default=None, ge=1, le=100)
    #: Ceiling on the reviewer's spend, passed as ``max_budget_usd``. Clamped to
    #: ``WORKBENCH_REVIEW_MAX_BUDGET_USD`` for the reason above — the difference
    #: between the two bounds is $98 of someone else's money.
    max_budget_usd: float | None = Field(default=None, gt=0.0, le=100.0)

    @field_validator("focus")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return value.strip()


class ReportFindingsRequest(BaseModel):
    """The ``report_findings`` tool's arguments — a reviewer's whole output.

    Not prose to be parsed. ``present_plan`` established this shape in the repo
    (an agent delivers a *typed artifact* by calling a tool), and it is what
    makes a review a record rather than a paragraph someone has to interpret.

    An **empty list is meaningful and allowed**: it is how a reviewer says "I
    found nothing", which must be distinguishable from a reviewer that never
    answered at all (AXI shape 2). The check reports those two differently — a
    clean ``pass`` line versus a ``fail`` naming the ceiling that stopped it.
    """

    findings: list[ReviewFinding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    #: One sentence the reviewer may add about coverage — what it could not
    #: check, and why. Optional, bounded, and never a verdict.
    note: str = Field(default="", max_length=MAX_CLAIM_CHARS)
