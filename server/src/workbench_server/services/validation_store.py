"""The disk source behind the validation frame: proof that survives a restart.

The #82 frame publishes every result on the bus and replays what it holds on
``GET /api/validation`` — and holds it in an LRU that a restart empties. This
module is the third source in that same pattern: a **file** the frame reads on
boot, so the endpoint a client already calls on reconnect simply stops being
empty. Nothing downstream of the service changes; no client learns a new call.

The layout, under the workspace's own ``.workbench/``::

    .workbench/validation/
      results-2026-08.jsonl      one StoredValidation per line, append-only
      approvals.jsonl            one StoredApproval per line, append-only
      payloads/<kind>/<ref>.json one EvidencePayload envelope, byte-budgeted
      exports/<validation_id>.md the rendered report (services/evidence_export.py)

Four decisions, each of which is a refusal of an easier one:

* **Append-only, never rewritten.** An approval is a *new line* in a second
  file rather than an edit to the result's line, because the alternative is
  rewriting the file that is the record of what somebody signed off on. Replay
  applies the last approval line for an id on top of the result.
* **A bad line costs a line.** A truncated or unparseable trailing line — the
  shape a power cut leaves — is skipped with a ``structlog`` warning and every
  other line in the file is kept (``services/layouts.py``'s posture). Losing one
  reading costs a reading; guessing at it costs a wrong verdict.
* **A payload over budget is written short, never dropped**, carrying an
  ``EvidenceTruncation`` that says how much was cut (AXI shape 1). What is on
  disk is then honest about being partial, which blankness never is.
* **Retention sweeps whole months.** A file is deleted only once *every* line in
  it is past the window, so the configured window is a floor and never a
  ceiling — an append-only file cannot have rows removed from its middle, and
  keeping evidence a few days too long is the safe direction of that trade.

Nothing here raises into a caller. A write that fails costs the record and never
the run; a read that fails costs the replay and never the boot.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from workbench_server.models.evidence import EvidencePayload
from workbench_server.models.gates import GateLog
from workbench_server.models.reconciliation import ReconciliationReport
from workbench_server.models.review import ReviewReport
from workbench_server.models.validation import (
    EvidenceKind,
    EvidenceTruncation,
    ValidationApproval,
    ValidationResult,
)
from workbench_server.models.validation_store import (
    MAX_LINE_BYTES,
    MAX_PAYLOAD_BYTES,
    STORE_VERSION,
    RetentionPolicy,
    StoredApproval,
    StoredPayload,
    StoredValidation,
    as_utc,
)

log = structlog.get_logger()

#: Where the record lives, relative to the workspace root. Inside the project on
#: purpose — a verdict about *these* files belongs beside them, and a switch to
#: another workspace must find that project's evidence and not this one's.
VALIDATION_DIR = ".workbench/validation"

#: Sub-paths under it.
PAYLOAD_DIR = "payloads"
EXPORT_DIR = "exports"
APPROVALS_FILE = "approvals.jsonl"

#: ``results-YYYY-MM.jsonl``. One file per month is what makes an append-only log
#: prunable at all: whole files age out, and nothing is ever rewritten.
_RESULTS_GLOB = "results-*.jsonl"
_RESULTS_MONTH = re.compile(r"^results-(\d{4})-(\d{2})\.jsonl$")

#: A ``payload_ref`` is server-minted (``kind_<hex>``), but it reaches this
#: module from a file on disk as well as from memory — so it is checked before it
#: becomes a filename. A ref that is not this shape is refused, never sanitised:
#: quietly rewriting a caller's key would put the payload somewhere it can never
#: be looked up again.
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


# ---- the payload envelope, one narrowing shared by disk and the router --------


def payload_envelope(kind: EvidenceKind, ref: str, payload: BaseModel) -> EvidencePayload | None:
    """Wrap one detail payload in the shape ``GET …/payload/{kind}/{ref}`` returns.

    ``None`` for a payload shape this build has no field for — which the router
    turns into a 404 and this module turns into a warning and a skipped file.
    One narrowing, used by both, because two copies of it drift the day a fourth
    payload kind lands and only one of them is visited.
    """
    if isinstance(payload, ReconciliationReport):
        return EvidencePayload(kind=kind, ref=ref, reconciliation=payload)
    if isinstance(payload, GateLog):
        return EvidencePayload(kind=kind, ref=ref, gate_log=payload)
    if isinstance(payload, ReviewReport):
        return EvidencePayload(kind=kind, ref=ref, review=payload)
    return None


def payload_of(envelope: EvidencePayload) -> BaseModel | None:
    """The one payload an envelope carries, or ``None`` if it carries none."""
    return envelope.reconciliation or envelope.gate_log or envelope.review


# ---- the byte budget ---------------------------------------------------------


#: Bytes held back from the budget for the ``EvidenceTruncation`` a trim adds.
#: The sentence plus its keys is ~200 bytes; 256 is that with a margin.
_TRUNCATION_HEADROOM = 256


def _encoded_size(data: dict[str, Any]) -> int:
    return len(json.dumps(data, separators=(",", ":")).encode("utf-8"))


def _longest_list(data: dict[str, Any]) -> str | None:
    best, size = None, 0
    for key, value in data.items():
        if isinstance(value, list) and len(value) > size:
            best, size = key, len(value)
    return best


def _longest_str(data: dict[str, Any]) -> str | None:
    best, size = None, 0
    for key, value in data.items():
        if isinstance(value, str) and len(value) > size:
            best, size = key, len(value)
    return best


def _largest_fitting(data: dict[str, Any], key: str, whole: list[Any], budget: int) -> int:
    """How many leading rows of ``whole`` fit in ``budget``. Binary search, so a
    9,000-row table costs ~14 serialisations rather than 9,000."""
    low, high = 0, len(whole)
    while low < high:
        mid = (low + high + 1) // 2
        data[key] = whole[:mid]
        if _encoded_size(data) <= budget:
            low = mid
        else:
            high = mid - 1
    data[key] = whole[:low]
    return low


def fit_payload(
    payload: BaseModel, budget: int = MAX_PAYLOAD_BYTES
) -> tuple[BaseModel, EvidenceTruncation | None]:
    """The payload as it will be written, and what was cut to make it fit.

    Shape-agnostic on purpose: it trims the model's **longest list field** (the
    comparison table, the findings, the gate list) and, if that is still not
    enough, its longest string (a captured log), then stamps the model's own
    ``truncated`` field if it has one. Every payload shape in the tree has
    exactly one of each, so this needs no table of kinds to keep up to date.

    A trim that no longer validates as the original model is abandoned and the
    **whole** payload is returned instead — the budget is a budget, and a proof
    that lost a payload because the trimmer was clever is worse than a fat file.
    """
    data = payload.model_dump(mode="json")
    if _encoded_size(data) <= budget:
        return payload, None

    # Room for the record of the cut itself. Trimming to exactly the budget and
    # *then* stamping a truncation puts the file back over it — the bug this
    # constant exists to have already made once.
    room = max(budget - _TRUNCATION_HEADROOM, budget // 2)
    truncation: EvidenceTruncation | None = None
    previous = data.get("truncated")
    already = previous.get("total") if isinstance(previous, dict) else None

    list_key = _longest_list(data)
    if list_key is not None:
        whole = list(data[list_key])
        kept = _largest_fitting(data, list_key, whole, room)
        if _encoded_size(data) > room:
            # Cutting this list did not get us there, so it was never the cost —
            # a `GateLog`'s longest list is its two-word argv, and emptying that
            # loses the one line that says *what ran* to save forty bytes.
            data[list_key] = whole
        elif kept < len(whole):
            total = already if isinstance(already, int) and already > len(whole) else len(whole)
            truncation = EvidenceTruncation(
                shown=kept,
                total=total,
                detail=(
                    f"stored {kept} of {total} {list_key}: this payload is over the "
                    f"{budget // 1024} KB on-disk budget. Re-run the validation to see it whole."
                ),
            )

    if _encoded_size(data) > room:
        text_key = _longest_str(data)
        if text_key is not None:
            whole_text: str = data[text_key]
            low, high = 0, len(whole_text)
            while low < high:
                mid = (low + high + 1) // 2
                data[text_key] = whole_text[:mid]
                if _encoded_size(data) <= room:
                    low = mid
                else:
                    high = mid - 1
            data[text_key] = whole_text[:low]
            if low < len(whole_text):
                truncation = EvidenceTruncation(
                    shown=low,
                    total=len(whole_text),
                    detail=(
                        f"stored the first {low} of {len(whole_text)} characters of "
                        f"{text_key}: over the {budget // 1024} KB on-disk budget."
                    ),
                )

    if truncation is not None and "truncated" in data:
        data["truncated"] = truncation.model_dump(mode="json")
    try:
        return type(payload).model_validate(data), truncation
    except ValidationError:
        log.warning("validation_store.payload_untrimmable", shape=type(payload).__name__)
        return payload, None


# ---- what a replay hands back ------------------------------------------------


@dataclass
class StoredEvidence:
    """Everything a boot (or a workspace switch) found on disk.

    ``results`` is oldest-first, the order ``GET /api/validation`` answers in and
    the order the LRU has to be filled in. ``payloads`` is keyed by
    ``(kind, ref)`` so the frame can put each back behind the ref its evidence
    already names.
    """

    results: list[ValidationResult] = field(default_factory=list)
    payloads: dict[tuple[EvidenceKind, str], BaseModel] = field(default_factory=dict)
    #: How many lines were skipped as unreadable, across every file. Reported
    #: rather than swallowed: a boot that silently dropped half the record would
    #: look exactly like a quiet quarter.
    skipped: int = 0


class ValidationStore:
    """Reads and appends ``<workspace>/.workbench/validation/``.

    Stateless between calls — it holds a path and nothing else — so re-rooting is
    a new instance, and there is no cache that can go on answering about the
    project the user left.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._dir = workspace_root.resolve() / Path(VALIDATION_DIR)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def exports_dir(self) -> Path:
        return self._dir / EXPORT_DIR

    # ---- writing ------------------------------------------------------------

    def append_result(
        self,
        result: ValidationResult,
        payloads: Sequence[tuple[EvidenceKind, str, BaseModel]],
        *,
        now: datetime,
    ) -> None:
        """Write one result and its detail payloads. Never raises.

        Payloads first, so a line on disk never names a file that is not there.
        The reverse order is the one that produces a result whose evidence points
        at nothing — which reads to a user as "the log was evicted" and is in
        fact "the write half-happened".
        """
        stored: list[StoredPayload] = []
        for kind, ref, payload in payloads:
            written = self._write_payload(kind, ref, payload)
            if written is not None:
                stored.append(written)
        line = StoredValidation(written_at=as_utc(now), result=result, payloads=stored)
        self._append(self._results_path(result.created_at), line)

    def append_approval(
        self, validation_id: str, approval: ValidationApproval, *, now: datetime
    ) -> None:
        """Write the human decision as its own line. Never raises."""
        self._append(
            self._dir / APPROVALS_FILE,
            StoredApproval(written_at=as_utc(now), validation_id=validation_id, approval=approval),
        )

    def _append(self, path: Path, line: BaseModel) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line.model_dump_json())
                handle.write("\n")
        except OSError as err:
            # The record is lost, the run is not. Warned rather than raised: a
            # read-only or full disk must not turn a green validation into a 500.
            log.warning("validation_store.append_failed", path=str(path), error=str(err))

    def _write_payload(
        self, kind: EvidenceKind, ref: str, payload: BaseModel
    ) -> StoredPayload | None:
        if not _SAFE_REF.match(ref):
            log.warning("validation_store.payload_ref_refused", ref=ref)
            return None
        trimmed, truncation = fit_payload(payload)
        envelope = payload_envelope(kind, ref, trimmed)
        if envelope is None:
            # A shape this build cannot describe. The router refuses to render
            # one too, so there is nothing on the other end that could read it.
            log.warning(
                "validation_store.payload_unstorable", kind=kind, shape=type(payload).__name__
            )
            return None
        target = self._dir / PAYLOAD_DIR / kind / f"{ref}.json"
        data = envelope.model_dump_json().encode("utf-8")
        try:
            _write_atomic(target, data)
        except OSError as err:
            log.warning("validation_store.payload_failed", path=str(target), error=str(err))
            return None
        return StoredPayload(kind=kind, ref=ref, bytes=len(data), truncated=truncation)

    def write_export(self, filename: str, markdown: str) -> Path:
        """Write one rendered report and answer with where it landed.

        Raises ``OSError`` — unlike the append paths. An export is something a
        user asked for and is waiting on, so a failure is a message they get
        rather than a file that quietly is not there.

        The name is checked, not trusted. It is built from a ``validation_id``,
        and a *replayed* id came off a file on disk: a hand-edited (or hostile)
        ``results-*.jsonl`` naming ``../../evil`` would otherwise be a write
        outside the workspace, through the one method here that is allowed to
        raise. Same rule and same reason as :data:`_SAFE_REF` on a payload ref.
        """
        if not _SAFE_REF.match(filename):
            raise OSError(f"refusing to write an export named {filename!r}")
        target = self.exports_dir / filename
        _write_atomic(target, markdown.encode("utf-8"))
        return target

    # ---- reading ------------------------------------------------------------

    def load(self, limit: int, *, payload_cap: int) -> StoredEvidence:
        """The newest ``limit`` results on disk, oldest-first, with their payloads.

        Newest-first across files and within each file, so a workspace with years
        of evidence costs one bounded read rather than a full scan. Never raises:
        an unreadable directory answers "nothing", which is the same answer a
        fresh workspace gives and is exactly as true.
        """
        found = StoredEvidence()
        if limit <= 0:
            return found
        newest: deque[StoredValidation] = deque(maxlen=limit)
        for path in self._results_files():
            if len(newest) >= limit:
                break
            # `maxlen` on the *remaining* budget: a file's own newest lines are
            # the ones that survive, and older files only fill what is left.
            tail: deque[StoredValidation] = deque(maxlen=limit - len(newest))
            for line in self._lines(path):
                parsed = _parse(line, StoredValidation)
                if parsed is None:
                    found.skipped += 1
                    continue
                tail.append(parsed)
            newest.extendleft(reversed(tail))
        if found.skipped:
            log.warning("validation_store.lines_skipped", count=found.skipped, dir=str(self._dir))

        by_id = {entry.result.validation_id: entry for entry in newest}
        approvals = self._approvals()
        ordered = sorted(by_id.values(), key=lambda entry: as_utc(entry.result.created_at))
        for entry in ordered:
            approval = approvals.get(entry.result.validation_id)
            found.results.append(
                entry.result
                if approval is None
                else entry.result.model_copy(update={"approval": approval})
            )

        # Payloads for the newest results first, so a cap that bites drops the
        # detail behind the *oldest* evidence rather than a random half.
        per_kind: dict[EvidenceKind, int] = {}
        for entry in reversed(ordered):
            for stored in entry.payloads:
                if per_kind.get(stored.kind, 0) >= payload_cap:
                    continue
                payload = self._read_payload(stored.kind, stored.ref)
                if payload is None:
                    continue
                found.payloads[(stored.kind, stored.ref)] = payload
                per_kind[stored.kind] = per_kind.get(stored.kind, 0) + 1
        return found

    def _approvals(self) -> dict[str, ValidationApproval]:
        """The last decision recorded for each id. Later lines win, which is what
        makes a re-approval an append rather than an edit."""
        latest: dict[str, ValidationApproval] = {}
        for line in self._lines(self._dir / APPROVALS_FILE):
            parsed = _parse(line, StoredApproval)
            if parsed is not None:
                latest[parsed.validation_id] = parsed.approval
        return latest

    def _read_payload(self, kind: EvidenceKind, ref: str) -> BaseModel | None:
        if not _SAFE_REF.match(ref):
            return None
        path = self._dir / PAYLOAD_DIR / kind / f"{ref}.json"
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError:
            return None  # swept, or never written — the same answer the LRU gives
        try:
            return payload_of(EvidencePayload.model_validate_json(raw))
        except ValidationError:
            log.warning("validation_store.payload_unreadable", path=str(path))
            return None

    def _lines(self, path: Path) -> Iterator[str]:
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    if len(line) > MAX_LINE_BYTES:
                        log.warning("validation_store.line_too_long", path=str(path))
                        continue
                    stripped = line.strip()
                    if stripped:
                        yield stripped
        except OSError:
            return  # no file yet, or an unreadable one: "nothing", not a crash

    # ---- retention -----------------------------------------------------------

    def prune(self, policy: RetentionPolicy, *, now: datetime) -> int:
        """Sweep whole month files that are entirely past the window.

        Returns how many files were removed. A month is only swept once its
        **last possible instant** is older than the cutoff, so the configured
        window is a floor: evidence lives at least that long and at most a month
        longer. That asymmetry is deliberate — an append-only file cannot lose a
        row from its middle, and the direction that errs is the one that keeps
        proof around too long rather than throwing it away too early.
        """
        cutoff = policy.cutoff(now)
        if cutoff is None:
            return 0
        removed = 0
        for path in self._results_files():
            end = _month_end(path.name)
            if end is None or end >= cutoff:
                continue
            for line in self._lines(path):
                parsed = _parse(line, StoredValidation)
                if parsed is None:
                    continue
                for stored in parsed.payloads:
                    if _SAFE_REF.match(stored.ref):
                        (self._dir / PAYLOAD_DIR / stored.kind / f"{stored.ref}.json").unlink(
                            missing_ok=True
                        )
            try:
                path.unlink()
            except OSError as err:
                log.warning("validation_store.prune_failed", path=str(path), error=str(err))
                continue
            removed += 1
            log.info("validation_store.pruned", path=str(path), policy=policy.detail())
        return removed

    # ---- paths ---------------------------------------------------------------

    def _results_files(self) -> list[Path]:
        """Month files, newest first. An unreadable directory is an empty list."""
        try:
            files = [p for p in self._dir.glob(_RESULTS_GLOB) if _RESULTS_MONTH.match(p.name)]
        except OSError:
            return []
        return sorted(files, key=lambda p: p.name, reverse=True)

    def _results_path(self, created_at: datetime) -> Path:
        stamp = as_utc(created_at)
        return self._dir / f"results-{stamp.year:04d}-{stamp.month:02d}.jsonl"


def _write_atomic(target: Path, data: bytes) -> None:
    """tmp + ``os.replace``, the discipline every other document write here uses."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


_Line = TypeVar("_Line", bound=BaseModel)


def _parse(line: str, model: type[_Line]) -> _Line | None:
    """One line, or ``None`` with a warning. The half-written trailing line a
    power cut leaves is exactly this case, and it must cost one line."""
    try:
        parsed = model.model_validate_json(line)
    except ValidationError:
        log.warning("validation_store.line_unreadable", shape=model.__name__)
        return None
    version = getattr(parsed, "version", STORE_VERSION)
    if version != STORE_VERSION:
        log.warning("validation_store.line_version", shape=model.__name__, version=version)
        return None
    return parsed


def _month_end(name: str) -> datetime | None:
    """The first instant *after* the month a results file covers."""
    match = _RESULTS_MONTH.match(name)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
