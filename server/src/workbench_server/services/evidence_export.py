"""The proof, rendered as something a person can hand to someone else.

A :class:`~workbench_server.models.validation.ValidationResult` is a wire type:
ids, enums, refs into a payload store. Handing that to a colleague is handing
them a JSON blob and a request to trust it. This module turns one result into a
**one-page Markdown document** that answers, in the order somebody reads them:
what was validated, against which file, what each check found, what the risk is,
and who signed it off.

Markdown rather than PDF, and that is a decision rather than an omission: it is
readable, diffable, greppable and pastes into a ticket, an email or a chat
without a renderer, fonts or a page model. A PDF is a formatting job on top of a
finished document and it makes no claim in it truer.

**Two sections say "none" out loud** (AXI shape 2, applied to a human reader).
A result that came from no reconciliation spec prints ``not run from a spec``
and a result nobody has approved prints ``not approved``, because a reader who
cannot tell "there was no spec" from "the spec section is missing from this
build" does not have proof — they have a document with a hole in it.

Nothing here is computed twice. Every line comes from a field the result (or its
payload) already carries; the only thing this module reads that the result does
not carry is the *workbook's digest*, and that is stamped with when it was taken
so it can never be misread as the bytes the check saw.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import BaseModel

from workbench_server.models.reconciliation import ReconciliationReport
from workbench_server.models.validation import (
    CheckOutcome,
    EvidenceKind,
    ValidationResult,
)

log = structlog.get_logger()

#: Never digest a file larger than this. A workbook is measured in megabytes;
#: anything past this is not one, and a report must not take a minute to render
#: because somebody validated a subject that names a 40 GB export.
MAX_DIGEST_BYTES = 256 * 1024 * 1024

#: Read size for the digest. One buffer, reused — the file is never held whole.
_DIGEST_CHUNK = 1024 * 1024

#: How many evidence lines the report prints before it says it stopped. A
#: one-page document that is forty pages long is not the thing this makes.
MAX_REPORT_EVIDENCE = 40


class PayloadLookup(Protocol):
    """Just enough of the payload store to read a detail payload back.

    A Protocol rather than the class, so this module does not import
    ``services/validation.py`` — which imports *it*.
    """

    def get(self, kind: EvidenceKind, ref: str) -> BaseModel | None: ...


def _stamp(value: datetime) -> str:
    """A timestamp as a person reads one: local wall clock plus its UTC offset.

    Local rather than UTC because every other time in this product is local wall
    clock (the reconciliation gate's whole contract), and offset-bearing rather
    than bare because a report that crosses a time zone with no label is a report
    about an hour nobody can identify — which in a market with a 25-hour day is
    not a pedantic distinction.

    The **offset** rather than ``tzname()``: on Windows that is the localised
    long name ("Vest-Europa (sommertid)"), which is neither short nor portable —
    read on the rendered output, which is why this is not the obvious version.
    """
    local = value.astimezone()
    offset = local.strftime("%z")
    return (
        f"{local:%Y-%m-%d %H:%M:%S} {offset[:3]}:{offset[3:]}"
        if offset
        else f"{local:%Y-%m-%d %H:%M:%S}"
    )


def _counts(result: ValidationResult) -> str:
    tally: dict[CheckOutcome, int] = {}
    for item in result.evidence:
        tally[item.outcome] = tally.get(item.outcome, 0) + 1
    parts = [f"{tally[o]} {o}" for o in ("fail", "warn", "skipped", "pass") if tally.get(o)]
    if not parts:
        return "no evidence — nothing was judged"
    return f"{len(result.evidence)} evidence line(s): {', '.join(parts)}"


def _workbook(result: ValidationResult, payloads: PayloadLookup) -> str | None:
    """The workspace-relative file this result is *about*, if it is about one.

    Preferred from the reconciliation payload, which names the workbook the check
    actually opened; falls back to the subject when it is a file. A session's
    output or an objective names no file, and the report says so rather than
    inventing one.
    """
    for item in result.evidence:
        if item.kind != "numeric" or item.payload_ref is None:
            continue
        payload = payloads.get(item.kind, item.payload_ref)
        if isinstance(payload, ReconciliationReport) and payload.workbook:
            return payload.workbook
    if result.subject.kind == "file" and result.subject.ref:
        return result.subject.ref
    return None


def _digest(root: Path, relative: str) -> str:
    """``sha256 <hex>, N bytes, modified <stamp>`` for a file under the root.

    Jailed: a subject that names a path outside the workspace is reported as
    such and never opened. Every failure resolves to a sentence — a report must
    render for a workbook that has since been deleted, because *that* is a fact
    the reader wants.
    """
    try:
        target = (root / relative).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        return "outside this workspace — not read"
    try:
        stat = target.stat()
    except OSError:
        return "no longer on disk when this report was written"
    if stat.st_size > MAX_DIGEST_BYTES:
        return f"{stat.st_size:,} bytes — too large to digest here"
    digest = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            while chunk := handle.read(_DIGEST_CHUNK):
                digest.update(chunk)
    except OSError as err:
        return f"unreadable when this report was written ({err.strerror or err})"
    modified = datetime.fromtimestamp(stat.st_mtime).astimezone()
    return f"sha256 {digest.hexdigest()}, {stat.st_size:,} bytes, modified {_stamp(modified)}"


def render_evidence(
    result: ValidationResult,
    *,
    payloads: PayloadLookup,
    workspace_root: Path,
    generated_at: datetime,
) -> str:
    """One result as a one-page Markdown report."""
    root = workspace_root.resolve()
    lines: list[str] = [
        f"# Validation evidence — {result.subject.label}",
        "",
        f"**Result**   `{result.validation_id}`",
        "",
        f"**Risk**     **{result.risk}** — {result.summary}",
        "",
        f"**Subject**  {result.subject.kind} · `{result.subject.ref}`",
        "",
        f"**Ran**      {_stamp(result.created_at)}"
        + (f", finished {_stamp(result.completed_at)}" if result.completed_at else ""),
        "",
    ]

    workbook = _workbook(result, payloads)
    if workbook is None:
        lines += [
            "**Workbook** — this result is about a "
            f"{result.subject.kind.replace('_', ' ')}, not a file.",
            "",
        ]
    else:
        lines += [
            f"**Workbook** `{workbook}`",
            "",
            f"> {_digest(root, workbook)}",
            "> ",
            "> Read when this report was written, not when the check ran — so a file "
            "that changed in between will not match what was judged.",
            "",
        ]

    # The two sections that have to say "none" rather than go missing.
    lines += [
        "**Source**   not recorded — this result carries no read-source provenance, "
        "so the numbers were read from the file as it stood at the time of the run.",
        "",
        "**Spec**     — not run from a spec, so there is no approved spec digest and no "
        "list of the code it covered.",
        "",
        f"**Checks**   {_counts(result)}",
        "",
    ]

    shown = result.evidence[:MAX_REPORT_EVIDENCE]
    for item in shown:
        lines.append(f"- **{item.outcome}** · {item.label} — {item.detail}")
    if not shown:
        lines.append("- (none — see the risk line above for why nothing was judged)")
    if len(result.evidence) > len(shown):
        lines.append(
            f"- …and {len(result.evidence) - len(shown)} more evidence line(s), not printed here. "
            "Open the result in the Review panel to read them all."
        )
    if result.truncated is not None:
        lines.append(f"- {result.truncated.detail}")
    lines.append("")

    if result.approval is None:
        lines += [
            "**Approval** — not approved. "
            + (
                "This result is medium-or-worse, so it is still awaiting a human decision."
                if result.risk in ("medium", "high", "blocked")
                else "None was required: a pass or low-risk result needs no sign-off."
            ),
            "",
        ]
    else:
        note = result.approval.note
        lines += [
            f"**Approval** {result.approval.approver}, {_stamp(result.approval.timestamp)}"
            + (f" — “{note}”" if note else ""),
            "",
        ]

    lines += [
        "---",
        "",
        f"Rendered by Workbench on {_stamp(generated_at)} from "
        f"`{result.validation_id}`. Times are this machine's local wall clock with "
        "its UTC offset. Everything above comes from the record this machine kept "
        "of the run; nothing in it was recomputed.",
        "",
    ]
    return "\n".join(lines)
