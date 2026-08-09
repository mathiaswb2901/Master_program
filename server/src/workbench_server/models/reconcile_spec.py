"""Spec-from-code: the typed shapes behind ``.workbench/reconcile/<name>.toml``.

A :class:`~workbench_server.models.reconciliation.ReconciliationSpec` takes its
``expected`` values **as data**, on purpose — ``models/reconciliation.py`` is
explicit that executing user code out of a JSON body is what the never-execute
doctrine forbids. That decision stands and nothing here weakens it. What it left
open is that *somebody* has to produce the data every time, which is why the gate
was fired by hand and therefore fired rarely.

This module is the other end: a **checked-in file** names the analyst's own
callable, the server owns the argv, and a **one-time content-hash approval** is
what turns a file in a folder into something that may run.

Three properties are in the schema rather than in a comment, because each is a
promise a later edit could quietly break:

* **Nothing in here can express a command.** A :class:`SpecCheck` names a
  ``module:function`` *within the workspace* and nothing else — no argv, no cwd,
  no interpreter. The argv is fixed and server-owned
  (``services/reconcile_spec.py``), and the spec travels to it on **stdin**, so
  ``models/gates.py``'s rule ("there is no field anywhere in this module through
  which a JSON body can reach an argv, a cwd or a path") holds here too. The
  posture that *is* wider than the gates' — this runs in the live workspace root,
  which ``ToolchainGateCheck`` refuses — is paid for by the approval below.

* **The approval is keyed to the code, not merely to the file that names it.**
  A :class:`SpecApproval` carries a composite digest over a list of
  :class:`CoveredSource` — the spec's own bytes, the module each ``callable``
  resolves to, and the workspace-local import closure a previous run *actually*
  used. Hashing the ``.toml`` alone would be a hole with a name: approve once,
  then rewrite the body of ``annual_revenue``, and the spec file — and therefore
  the approval — is untouched while the watcher keeps running whatever that
  function now contains, unattended, forever. An approval that authorises code it
  never hashed is not a trust prompt; it is a trust prompt's shadow.

* **What is covered is a list a person can read.** :attr:`SpecApproval.covered`
  is rendered by the panel verbatim, so "this spec, running exactly this code, on
  this machine, until either changes" is a claim with a receipt rather than one
  taken on faith.

Only :class:`SpecStates`, :class:`SpecState`, :class:`SpecApprovalRequest` and
:class:`SpecEvent` are wire bodies of their own. :class:`SpecEntryRequest` /
:class:`SpecEntryResult` are the **stdin/stdout envelope** of the subprocess and
never reach a browser; they are typed all the same, because the boundary they
cross is the one that matters most.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench_server.models.reconciliation import Tolerance

#: Where specs live, relative to the workspace root. A *folder* rather than one
#: file because a workspace routinely proves more than one workbook, and because
#: the approval is per spec: one bad edit revokes one spec, not the set.
SPEC_DIR = Path(".workbench") / "reconcile"

#: Suffix a spec file must carry. TOML because it is a file a person edits.
SPEC_SUFFIX = ".toml"

#: Ceiling on one spec file, in bytes. A spec is a page of declarations; anything
#: past this is not a spec that got long, it is a file that wandered into the
#: folder. Refused with the size, never silently half-read.
MAX_SPEC_BYTES = 256 * 1024

#: Ceiling on how many specs one workspace's folder contributes. Bounded because
#: every one of them is re-digested on every listing.
MAX_SPECS = 64

#: Ceiling on the checks in one spec. Each check is one callable in one
#: subprocess run, so this bounds the work a single save can buy.
MAX_CHECKS_PER_SPEC = 64

#: Ceiling on the ``(timestamp, value)`` pairs one ``range`` check may return.
#: A full hourly year is 8,760; this leaves room for a decade of them and still
#: bounds a callable that decided to return its whole dataframe.
MAX_PAIRS_PER_CHECK = 100_000

#: Ceiling on the bytes of the subprocess's **stdout** — the JSON envelope. The
#: envelope is bounded by the two caps above; this is the backstop for a child
#: that writes to the real stdout anyway. Chatty output goes to *stderr* (the
#: entry module redirects the callable's own ``print``), where it is bounded by
#: the gates' ``_BoundedCapture``.
MAX_SPEC_STDOUT_BYTES = 8 * 1024 * 1024

#: Bytes of the child's stderr kept for the evidence line. Head + ring tail, the
#: gates' window, for the same reason: a callable that prints a 500 MB dataframe
#: costs the window, not the memory.
MAX_SPEC_LOG_BYTES = 8_192

#: Env knobs, quoted in every refusal that they would fix.
SETTING_TIMEOUT = "WORKBENCH_RECONCILE_TIMEOUT_S"
SETTING_FAKE = "WORKBENCH_RECONCILE_FAKE"


# ---- the file ----------------------------------------------------------------


class SpecCheck(BaseModel):
    """One check: a workbook address, and the callable that produces its expected
    value.

    Exactly one of :attr:`cell` and :attr:`range` — a check that addressed both
    would have two answers about what shape the callable must return, and a check
    that addressed neither is a typo the reader should hear about at load time.
    """

    model_config = ConfigDict(populate_by_name=True)

    #: A1 address, sheet-qualified (``Summary!D14``) or bare (``D14`` → active
    #: sheet). The callable must return a scalar.
    cell: str | None = None
    #: A two-column A1 range, ``Hours!A2:B8761`` — timestamp column then value
    #: column. The callable must return an iterable of
    #: ``(naive local datetime, float)`` pairs, which is exactly ``TimeIndexSpec``'s
    #: vocabulary, so a spec compiles into a ``ReconciliationSpec`` and nothing
    #: downstream of the check changes.
    range: str | None = None
    #: ``module:function`` **within the workspace**. Field name is ``call``
    #: because ``callable`` is a builtin; the alias is what the TOML and the wire
    #: both use, so a person never types the compromise.
    call: str = Field(alias="callable")
    #: Unit of the value the *callable* returns.
    unit: str = ""
    #: Declared unit of the *workbook* side. ``None`` means "same as
    #: :attr:`unit`". A differing, compatible pair is a **named** conversion in
    #: the evidence — the x1000 this domain hides bugs in is never silent.
    value_unit: str | None = None
    label: str | None = None
    #: Per-check override of the spec's ``default_tolerance``.
    tolerance: Tolerance | None = None

    @model_validator(mode="after")
    def _exactly_one_address(self) -> SpecCheck:
        if (self.cell is None) == (self.range is None):
            raise ValueError("a check names exactly one of `cell` or `range`")
        if ":" not in self.call:
            raise ValueError(f"`callable` must be module:function, got {self.call!r}")
        module, _, function = self.call.partition(":")
        if not module or not function:
            raise ValueError(f"`callable` must be module:function, got {self.call!r}")
        return self

    def address(self) -> str:
        """The address this check names, whichever field carries it."""
        return self.cell if self.cell is not None else str(self.range)


class ReconcileSpecFile(BaseModel):
    """One ``.workbench/reconcile/<name>.toml``, parsed.

    Deliberately *not* a wire body: it is read from disk, never posted. The
    browser sees :class:`SpecState`, which is this file plus what the server
    knows about it.
    """

    model_config = ConfigDict(populate_by_name=True)

    #: Workspace-relative path to the ``.xlsx``, jailed against the root by the
    #: service before anything opens it.
    workbook: str
    #: IANA zone for ``range`` checks, e.g. ``Europe/Oslo``. Required when any
    #: check names a range: a time-indexed comparison without a zone cannot tell
    #: a fall-back day's two 02:00s apart, which is the whole reason the aligner
    #: exists.
    timezone: str | None = None
    default_tolerance: Tolerance
    #: ``[[check]]`` in the TOML — singular there because that is what a person
    #: writing one at a time types.
    checks: list[SpecCheck] = Field(
        default_factory=list, alias="check", max_length=MAX_CHECKS_PER_SPEC
    )

    @model_validator(mode="after")
    def _zone_when_time_indexed(self) -> ReconcileSpecFile:
        if any(check.range is not None for check in self.checks) and self.timezone is None:
            raise ValueError("a `range` check needs a top-level `timezone`")
        return self


# ---- what an approval covers -------------------------------------------------

#: How a covered file entered the approval. ``spec`` is the ``.toml`` itself,
#: ``callable`` is a module a ``callable`` entry resolved to at approval time,
#: and ``imported`` is a workspace-local module a *previous run* actually used
#: (the ``sys.modules`` closure — a fact, not a guess at the import graph).
CoveredOrigin = Literal["spec", "callable", "imported"]


class CoveredSource(BaseModel):
    """One file an approval stands for, and the bytes it stood for."""

    #: Workspace-relative, POSIX separators. Relative because an approval that
    #: moved with the folder would be an approval nobody re-made.
    path: str
    #: ``blake2b`` of the file's bytes, or :data:`ABSENT_DIGEST` when it is gone.
    digest: str
    origin: CoveredOrigin


#: The digest recorded for a covered path that cannot be read. A distinct value
#: rather than an omission: "the file is gone now" is a state that has to be
#: tellable apart from "nothing changed" (``services/gates.py::content_digest``,
#: same rule one level up).
ABSENT_DIGEST = "absent"


class SpecApproval(BaseModel):
    """The one-time trust decision, recorded.

    Stored in the machine's app-data dir and **never** in ``.workbench/`` — the
    reason ``RecentsStore`` spells out, sharpened by what this one authorises: a
    trust record inside the folder it authorises is a trust record an attacker
    can write.
    """

    #: Spec file stem, e.g. ``dispatch`` for ``dispatch.toml``.
    name: str
    #: The composite digest over :attr:`covered`, in the order that list carries.
    #: Re-computed before every run and compared; a mismatch is a refusal, never
    #: a silent re-run and never a silent skip.
    digest: str
    approver: str
    #: Server-minted, never taken from the caller.
    approved_at: datetime
    #: Every file this approval stands for, rendered verbatim by the panel.
    covered: list[CoveredSource] = Field(default_factory=list)


# ---- what a run produced -----------------------------------------------------

#: A spec's own verdict. ``blocked`` is the digest-mismatch state — the code
#: changed and nobody re-approved it — and it is deliberately its own word rather
#: than a ``skipped``: the difference between them is the difference between a
#: badge someone scrolls past and a badge that stops them.
#:
#: Note for the reader coming from the plan: ``CheckOutcome`` (``models/
#: validation.py``) does not carry ``"blocked"`` yet — that row is PR-A's, in
#: PR-A's files. Until it lands, a blocked *spec* still produces a ``fail``
#: evidence line carrying the same sentence, so the risk is ``high`` and nothing
#: is ever waved through; only the word on the pill is the compromise.
SpecOutcome = Literal["pass", "warn", "fail", "blocked", "skipped"]

#: Whether a spec may run right now, and why not when it may not.
SpecStatus = Literal["unapproved", "approved", "stale", "invalid"]


class SpecRunReport(BaseModel):
    """One whole run of one spec — what it judged and how long it took."""

    name: str
    outcome: SpecOutcome
    #: The ``ValidationResult`` this run produced, when it produced one. ``None``
    #: for a run that never reached the gate (unapproved, stale, invalid).
    validation_id: str | None = None
    #: What started it — the loop's whole claim is that ``watcher`` happens
    #: without anybody clicking, so the two are told apart on the record.
    trigger: Literal["watcher", "manual"] = "manual"
    ran_at: datetime
    duration_ms: int
    #: How many expected values the callables produced (scalars + pairs).
    values: int = 0
    #: One sentence a human reads. Never blank: an empty result says it is empty
    #: (AXI shape 2) and a refusal names the way to fix it (shape 3).
    detail: str


class SpecState(BaseModel):
    """One row of the panel: a spec, what it points at, and whether it may run."""

    name: str
    #: Workspace-relative path of the ``.toml``.
    path: str
    #: Workspace-relative path of the workbook it proves. Empty when the file
    #: could not be parsed at all.
    workbook: str = ""
    status: SpecStatus
    #: The composite digest **as it is right now**. This is the value the client
    #: echoes back on approve, so a spec that changed between the render and the
    #: click is refused rather than approved on the strength of what was on
    #: screen.
    digest: str
    checks: int = 0
    approval: SpecApproval | None = None
    #: The last run this server observed, or ``None``. In memory: a restart
    #: forgets it, and the approval — which is the part that matters — does not.
    last_run: SpecRunReport | None = None
    #: One sentence: why it cannot run, or what it is proving.
    detail: str


class SpecStates(BaseModel):
    """``GET /api/reconcile/specs`` — every spec in the workspace's folder.

    An empty list is a real and common answer (a workspace with no specs), and
    :attr:`detail` says so rather than leaving blankness to interpret.
    """

    specs: list[SpecState] = Field(default_factory=list)
    #: One sentence about the set — "no specs" names the folder to create.
    detail: str


class SpecApprovalRequest(BaseModel):
    """``POST /api/reconcile/specs/{name}/approve``.

    The caller echoes the digest it was shown. A digest that no longer matches is
    **409**, not a 200: approving a spec whose bytes moved under the dialog would
    approve something nobody read.
    """

    approver: str
    digest: str


class SpecEvent(BaseModel):
    """Broadcast on ``/ws/events`` whenever a spec's state changes.

    Carries the whole state, not a delta — the client holds its own map keyed by
    ``name`` and replaces the entry, exactly as it does for a ``ValidationEvent``.
    """

    type: Literal["reconcile_spec"] = "reconcile_spec"
    state: SpecState


# ---- the subprocess envelope -------------------------------------------------


class SpecEntryRequest(BaseModel):
    """What travels to the child on **stdin**. Never an argv.

    The child is started as a fixed, server-owned
    ``uv run python -m workbench_server.spec_entry`` with the workspace root as
    its cwd; this document is the only variable input, and every field in it is a
    *selection* the spec file already made.
    """

    #: ``module:function`` strings, in the spec's own order. The child imports
    #: each and calls it with no arguments.
    callables: list[str] = Field(default_factory=list, max_length=MAX_CHECKS_PER_SPEC)
    #: Ceiling on the pairs one callable may return, so a dataframe cannot
    #: become an unbounded stdout.
    max_pairs: int = MAX_PAIRS_PER_CHECK


class SpecValue(BaseModel):
    """What one callable answered."""

    #: The ``module:function`` this answers for, echoed so the parent never has
    #: to trust list order.
    call: str
    ok: bool
    #: A scalar answer, for a ``cell`` check.
    scalar: float | None = None
    #: ``(naive local ISO timestamp, value)`` pairs, for a ``range`` check.
    pairs: list[tuple[str, float]] = Field(default_factory=list)
    #: Set when the pair list was cut at ``max_pairs`` (AXI shape 1).
    total_pairs: int | None = None
    #: Why it failed, when it did. One line, already bounded by the child.
    error: str | None = None


class SpecEntryResult(BaseModel):
    """What comes back on the child's **stdout** — one compact JSON line.

    :attr:`modules` is a list of **paths only**, deliberately. The child names
    the files it imported; the *parent* hashes them, because a digest supplied by
    the process being trusted is not evidence of anything.
    """

    ok: bool
    values: list[SpecValue] = Field(default_factory=list)
    #: Absolute paths of every module in ``sys.modules`` whose ``__file__``
    #: resolves under the workspace root after the callables returned. A fact
    #: about what ran, not a static guess at an import graph that may be
    #: conditional or built with ``importlib``.
    modules: list[str] = Field(default_factory=list)
    #: Set when the child could not even start the work (a malformed request).
    error: str | None = None
