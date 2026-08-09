"""Running a validation, and holding its result.

**A validation is a registry of checks, not a hardwired pipeline** — the same
"registry, not a pipeline" instinct as the tool registry. A :class:`ValidationCheck`
is a small typed protocol (an ``id`` and an async ``run``); the service dispatches
to the registered checks a spec names, assembles their evidence into one
:class:`ValidationResult`, and **derives the risk** from that evidence through an
explicit table — the caller never asserts a risk.

The first real check is M6's reconciliation gate (a later PR); this module ships
the *frame*, provable end-to-end by a fake check that returns canned evidence.

Two bounded stores, both mirroring provenance's 500-path posture:

* **Results**, an LRU keyed by ``validation_id``. A long-running server cannot
  grow it without limit; an evicted result is gone from ``GET /api/validation``
  too, which is the honest answer (a client that reconnects will not see it).
* **Payloads**, per evidence ``kind``. An :class:`EvidenceItem` references its
  detail payload by id rather than inlining it, so a large reconciliation table
  never rides the result or the ``/ws/events`` frame.

Every result is published as a :class:`ValidationEvent` on the shared bus, the
usage/activity precedent, so a window that never issued the run still tracks it.

**Both stores are backed by a disk source** (``services/validation_store.py``).
The bounded maps above are still what serves a request — nothing downstream of
this module changed — but they are now *filled* from
``<workspace>/.workbench/validation/`` on :meth:`ValidationService.start`, and
re-filled from the new root on a workspace switch. That is the whole shape of
this feature: a third source in the #82 event-and-replay pattern, so the endpoint
a client already calls on reconnect stops being empty after a restart. A write
that fails costs the record and never the run.
"""

import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel

from workbench_server.models.validation import (
    CheckOutcome,
    EvidenceItem,
    EvidenceKind,
    EvidenceTruncation,
    RiskLevel,
    ValidationApproval,
    ValidationEvent,
    ValidationResult,
    ValidationResults,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.models.validation_store import EvidenceExport, RetentionPolicy
from workbench_server.services.event_bus import EventBus
from workbench_server.services.evidence_export import render_evidence
from workbench_server.services.validation_store import ValidationStore

log = structlog.get_logger()

#: LRU cap on held results — mirrors provenance's ``MAX_TRACKED_PATHS``. A long
#: session cannot grow the map without limit; the oldest result is evicted.
MAX_RESULTS = 500

#: LRU cap on stored payloads, per evidence ``kind``. Detail payloads are
#: referenced by id, and this bounds how many are kept behind those ids.
MAX_PAYLOADS_PER_KIND = 500

#: Cap on the evidence a single result carries. A defensive bound: a pathological
#: check cannot make one result unbounded. When it bites, ``truncated`` says so.
MAX_EVIDENCE = 100

#: outcome → the risk that outcome contributes. The whole result's risk is the
#: max severity over its evidence through this table.
#:
#: An empty evidence list is *also* ``blocked`` (see :func:`derive_risk`) — that
#: is the absence of judgeable evidence rather than any one outcome — but a check
#: may now say ``blocked`` on the record instead, which is strictly better: the
#: refusal arrives with the sentence that names the remedy. Both routes land on
#: the same badge, and the badge is the point: ``low`` is something a reader
#: scrolls past, ``blocked`` is something that stops them.
_OUTCOME_RISK: dict[CheckOutcome, RiskLevel] = {
    "pass": "pass",
    "skipped": "low",
    "warn": "medium",
    "fail": "high",
    "blocked": "blocked",
}

#: Least-to-most severe. ``blocked`` sorts highest: a validation that could not
#: judge at all is worse than one that ran and failed a check.
_RISK_SEVERITY: dict[RiskLevel, int] = {
    "pass": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "blocked": 4,
}


def derive_risk(evidence: list[EvidenceItem]) -> RiskLevel:
    """The badge value for a set of evidence.

    No evidence is **blocked** — a validation that produced nothing judged
    nothing, and reporting that as a green ``pass`` is exactly the silent-green
    failure this gate exists to refuse. Otherwise the risk is the most severe
    contribution over the evidence, through :data:`_OUTCOME_RISK`.
    """
    if not evidence:
        return "blocked"
    return max(
        (_OUTCOME_RISK[item.outcome] for item in evidence),
        key=lambda risk: _RISK_SEVERITY[risk],
    )


def _summarize(risk: RiskLevel, evidence: list[EvidenceItem]) -> str:
    """One sentence for the badge tooltip and the card."""
    if not evidence:
        return "Blocked: no checks produced evidence — nothing was validated."
    counts: dict[CheckOutcome, int] = {}
    for item in evidence:
        counts[item.outcome] = counts.get(item.outcome, 0) + 1
    # Most severe first, so the sentence opens with what stops the reader.
    # ``blocked`` leads: a check that refused to judge outranks one that judged
    # and disagreed (_OUTCOME_RISK, and RiskLevel's own ordering).
    parts = [
        f"{counts[o]} {o}" for o in ("blocked", "fail", "warn", "skipped", "pass") if counts.get(o)
    ]
    return f"{risk}: {len(evidence)} checks ({', '.join(parts)})."


class PayloadStore:
    """A bounded, per-kind LRU of evidence detail payloads.

    A check stashes a payload and gets back a ``payload_ref`` to put on its
    :class:`EvidenceItem`; the payload never rides the result. Bounded per kind
    so one chatty kind cannot starve the others.
    """

    def __init__(self, cap: int = MAX_PAYLOADS_PER_KIND) -> None:
        self._cap = cap
        self._by_kind: dict[EvidenceKind, OrderedDict[str, BaseModel]] = {}

    def put(self, kind: EvidenceKind, payload: BaseModel) -> str:
        ref = f"{kind}_{uuid.uuid4().hex[:16]}"
        self.put_ref(kind, ref, payload)
        return ref

    def put_ref(self, kind: EvidenceKind, ref: str, payload: BaseModel) -> None:
        """Put a payload back behind a ref that already exists.

        The replay half of :meth:`put`: a payload read off disk is already named
        by the ``payload_ref`` on a stored result's evidence, so minting a fresh
        id for it would leave that reference pointing at nothing — the dead
        handle the payload route was added to close.
        """
        store = self._by_kind.setdefault(kind, OrderedDict())
        store[ref] = payload
        store.move_to_end(ref)
        while len(store) > self._cap:
            store.popitem(last=False)

    def get(self, kind: EvidenceKind, ref: str) -> BaseModel | None:
        """The payload behind a ref, or None once the LRU has dropped it."""
        return self._by_kind.get(kind, OrderedDict()).get(ref)


@dataclass
class ValidationContext:
    """What a check is handed. Read-only into the workspace, plus a place to
    stash detail payloads.

    ``params`` is the spec's per-check input, as *data* — never code. ``payloads``
    is the shared store; a check calls :meth:`store_payload` and puts the returned
    ref on its evidence.
    """

    subject: ValidationSubject
    root: Path
    params: dict[str, Any] = field(default_factory=dict)
    payloads: PayloadStore = field(default_factory=PayloadStore)

    def store_payload(self, kind: EvidenceKind, payload: BaseModel) -> str:
        return self.payloads.put(kind, payload)


@runtime_checkable
class ValidationCheck(Protocol):
    """One registered check. The pipeline is the set of these, not a sequence."""

    id: str

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]: ...


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ValidationService:
    """Runs validations against registered checks and holds their results.

    Loop-affine for publishing: :meth:`run` is awaited from the async router, so
    the ``ValidationEvent`` is put on subscriber queues from the event loop. No
    background task and nothing subscribed — this service is *called*, not fed.
    """

    def __init__(
        self,
        root: Path,
        bus: EventBus,
        clock: Callable[[], datetime] = _now_utc,
        id_factory: Callable[[], str] | None = None,
        max_results: int = MAX_RESULTS,
        retention: Callable[[], RetentionPolicy] | None = None,
    ) -> None:
        self._root = root.resolve()
        self._bus = bus
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"val_{uuid.uuid4().hex[:16]}")
        self._max_results = max_results
        self._checks: dict[str, ValidationCheck] = {}
        self._results: OrderedDict[str, ValidationResult] = OrderedDict()
        self._payloads = PayloadStore()
        # How long the record is kept, asked for rather than captured: it lives
        # in the *machine's* settings document, which a workspace switch does not
        # touch and a user may change while the server runs.
        self._retention = retention or (lambda: RetentionPolicy())
        self._disk = ValidationStore(self._root)

    def start(self) -> None:
        """Sweep what is past the window, then fill the maps from disk.

        Called once from the lifespan, before anything can ask. Prune first so a
        replay never rehydrates results it is about to delete, and never raises:
        a workspace whose ``.workbench/`` cannot be read starts empty, which is
        the same answer a fresh one gives and is exactly as true.
        """
        self._replay()

    def set_workspace_root(self, root: Path) -> None:
        """Re-root, and **re-read the new project's evidence**.

        A result for ``subject.kind == "file"`` names a workspace-relative path,
        so carrying one across a switch would attach a verdict about a file in
        the project the user left to a same-named file in the one they opened —
        which is why this used to clear both maps and stop. Clearing was standing
        in for the right behaviour: the record lives *in* the workspace, so the
        honest answer after a switch is the other project's own evidence, not
        emptiness. Nothing is published — the client re-reads
        ``GET /api/validation`` as part of adopting the new workspace.
        """
        self._root = root.resolve()
        self._results.clear()
        self._payloads = PayloadStore()
        self._disk = ValidationStore(self._root)
        self._replay()

    def _replay(self) -> None:
        """Prune, then rehydrate the bounded maps from the disk source."""
        policy = self._policy()
        try:
            self._disk.prune(policy, now=self._clock())
            found = self._disk.load(self._max_results, payload_cap=MAX_PAYLOADS_PER_KIND)
        except Exception:  # a replay may never be the reason a server will not start
            log.exception("validation.replay_failed", root=str(self._root))
            return
        for result in found.results:
            self._results[result.validation_id] = result
        for (kind, ref), payload in found.payloads.items():
            self._payloads.put_ref(kind, ref, payload)
        log.info(
            "validation.replayed",
            results=len(found.results),
            payloads=len(found.payloads),
            skipped=found.skipped,
            retention=policy.detail(),
        )

    def _policy(self) -> RetentionPolicy:
        try:
            return self._retention()
        except Exception:
            log.exception("validation.retention_unreadable")
            return RetentionPolicy()

    def register(self, check: ValidationCheck) -> None:
        """Add a check to the registry. Last registration for an id wins."""
        self._checks[check.id] = check

    async def run(self, spec: ValidationSpec) -> ValidationResult:
        """Dispatch to the checks the spec names, assemble one result, store and
        publish it.

        A named check that is not registered, or one that raises, becomes a
        ``gate``/``fail`` evidence line — never a silent skip. A run that
        produces no evidence at all derives ``blocked`` (see :func:`derive_risk`).
        """
        created_at = self._clock()
        ctx = ValidationContext(subject=spec.subject, root=self._root, params=spec.params)
        ctx.payloads = self._payloads
        selected = spec.checks or list(self._checks)
        evidence: list[EvidenceItem] = []
        for check_id in selected:
            check = self._checks.get(check_id)
            if check is None:
                evidence.append(
                    EvidenceItem(
                        kind="gate",
                        label=check_id,
                        outcome="fail",
                        detail=f"check '{check_id}' is not registered",
                    )
                )
                continue
            try:
                evidence.extend(await check.run(ctx))
            except Exception as exc:  # a check that throws is a gate that could not run
                log.exception("validation.check_failed", check=check_id)
                evidence.append(
                    EvidenceItem(
                        kind="gate",
                        label=check_id,
                        outcome="fail",
                        detail=f"check '{check_id}' could not run: {exc}",
                    )
                )

        # Risk is derived over the *full* evidence the checks produced, before any
        # display cap. Truncating first would let a fail/warn past index MAX_EVIDENCE
        # be silently dropped from the risk — the same silent-green failure this gate
        # exists to refuse. Only the stored/returned list is capped.
        risk = derive_risk(evidence)

        truncated: EvidenceTruncation | None = None
        if len(evidence) > MAX_EVIDENCE:
            total = len(evidence)
            evidence = evidence[:MAX_EVIDENCE]
            truncated = EvidenceTruncation(
                shown=MAX_EVIDENCE,
                total=total,
                detail=(
                    f"showing {MAX_EVIDENCE} of {total} evidence items; "
                    "run fewer checks to see the rest"
                ),
            )
        result = ValidationResult(
            validation_id=self._id_factory(),
            subject=spec.subject,
            risk=risk,
            evidence=evidence,
            summary=_summarize(risk, evidence),
            created_at=created_at,
            completed_at=self._clock(),
            truncated=truncated,
        )
        self._store(result)
        self._write(result)
        self._bus.publish(ValidationEvent(result=result))
        log.info(
            "validation.completed",
            validation_id=result.validation_id,
            subject=spec.subject.ref,
            risk=risk,
            evidence=len(evidence),
        )
        return result

    def approve(
        self, validation_id: str, approver: str, note: str | None = None
    ) -> ValidationResult | None:
        """Record the human decision on a result, and republish it.

        The timestamp is minted here from the service's own clock — never taken
        from the caller — so it is one the test can drive and the wire cannot
        forge. None means the id is unknown — evicted by the LRU or never minted:
        the router turns that into a 404 rather than a 200 a caller could misread
        as an approval that landed. A known result is updated in place, touched
        to most-recently-used, and re-broadcast so every window's map catches up.
        """
        result = self._results.get(validation_id)
        if result is None:
            return None
        approval = ValidationApproval(approver=approver, timestamp=self._clock(), note=note)
        updated = result.model_copy(update={"approval": approval})
        self._results[validation_id] = updated
        self._results.move_to_end(validation_id)
        # Its own append-only line, never an edit to the result's — which is what
        # lets the results log stay append-only at all (services/validation_store.py).
        self._disk.append_approval(validation_id, approval, now=self._clock())
        self._bus.publish(ValidationEvent(result=updated))
        return updated

    def export(self, validation_id: str) -> EvidenceExport | None:
        """Render one result as a one-page report and write it into the workspace.

        ``None`` means the id is unknown — the same answer :meth:`approve` gives
        an evicted handle, and the router turns it into a 404 rather than an
        empty document a reader could mistake for a result with nothing in it.
        Raises ``OSError`` if the file cannot be written: an export is something
        a person is waiting on, so a failure is a message and not a silence.
        """
        result = self._results.get(validation_id)
        if result is None:
            return None
        generated_at = self._clock()
        markdown = render_evidence(
            result,
            payloads=self._payloads,
            workspace_root=self._root,
            generated_at=generated_at,
        )
        filename = f"{validation_id}.md"
        written = self._disk.write_export(filename, markdown)
        relative = written.relative_to(self._root).as_posix()
        log.info("validation.exported", validation_id=validation_id, path=relative)
        return EvidenceExport(
            validation_id=validation_id,
            path=relative,
            filename=filename,
            markdown=markdown,
            bytes=len(markdown.encode("utf-8")),
            generated_at=generated_at,
        )

    def _write(self, result: ValidationResult) -> None:
        """Append the result and the detail payloads its evidence names."""
        payloads: list[tuple[EvidenceKind, str, BaseModel]] = []
        for item in result.evidence:
            if item.payload_ref is None:
                continue
            payload = self._payloads.get(item.kind, item.payload_ref)
            if payload is not None:
                payloads.append((item.kind, item.payload_ref, payload))
        self._disk.append_result(result, payloads, now=self._clock())

    def get(self, validation_id: str) -> ValidationResult | None:
        return self._results.get(validation_id)

    def payload(self, kind: EvidenceKind, ref: str) -> BaseModel | None:
        """The detail payload behind an ``EvidenceItem.payload_ref``, or None
        once the per-kind LRU has dropped it."""
        return self._payloads.get(kind, ref)

    def snapshot(self) -> ValidationResults:
        """Every result currently held, oldest first — initial load and
        reconnect, the usage/activity precedent."""
        return ValidationResults(results=list(self._results.values()))

    def _store(self, result: ValidationResult) -> None:
        self._results[result.validation_id] = result
        self._results.move_to_end(result.validation_id)
        while len(self._results) > self._max_results:
            evicted, _ = self._results.popitem(last=False)
            log.debug("validation.evicted", validation_id=evicted)
