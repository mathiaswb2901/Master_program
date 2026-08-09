"""The toolchain gate's typed inputs and outputs — M6 staged review, PR 1.

A gate is the second kind of proof this milestone ships. Reconciliation proves
*numbers*; a gate proves the rest of what an agent hands over: that the change it
wrote lints, type-checks and passes its own tests. Both are
:class:`~workbench_server.services.validation.ValidationCheck` implementations,
so everything downstream of a check — risk derivation, the gallery, the bus
event, the approval — is the frame's and is untouched here.

Two properties are in the schema rather than in a comment, because each is a
promise a later edit could quietly break:

* **A caller names a gate, never a command line.** :class:`GateCommand` is
  *server-owned data*: its ``argv`` is fixed in the catalog
  (``services/gates.py``) and a :class:`GateSpec` selects from that catalog **by
  id**. There is no field anywhere in this module through which a JSON body can
  reach an argv, a cwd or a path — that is the ``m6-proof.md`` "no shell in a
  JSON body" refusal, sharpened, because this check really does run processes.
* **A captured log is bounded while it is read, not after.** :class:`GateLog`
  carries at most :data:`MAX_GATE_LOG_BYTES` of output as a head plus a tail, and
  ``truncated`` states how much was withheld (AXI shape 1, reusing the frame's
  :class:`~workbench_server.models.validation.EvidenceTruncation`).

Only :class:`GateLog` is a wire body of its own, and only through the payload
envelope in ``models/evidence.py``: a :class:`GateSpec` travels *inside*
``ValidationSpec.params`` (a ``dict`` on the wire) and :class:`GateRunReport` is
the internal aggregate the check and the ``run_gates`` tool are built from.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from workbench_server.models.validation import EvidenceTruncation

#: Ceiling on one gate's captured output, in bytes, and also the **default**
#: window: a run that names no ``log_bytes`` captures exactly this much.
#:
#: Split as a 2 KiB head plus a 6 KiB tail (see :data:`GATE_LOG_HEAD_FRACTION`).
#: The tail because that is where ``pytest`` and ``mypy`` put their summary; the
#: head because that is where a "command not found" lands. Bounded *while the
#: pipes drain*, so a gate that prints 500 MB costs 8 KiB of memory rather than
#: 500 MB — the runner keeps a head buffer and a ring tail, never an unbounded
#: buffer it truncates afterwards.
MAX_GATE_LOG_BYTES = 8_192

#: Floor on a caller-narrowed window. Below this a log stops being readable and
#: starts being a rumour of one.
MIN_GATE_LOG_BYTES = 256

#: How much of a window is head. ``1/4`` of 8,192 is 2,048 — the plan's 2 KiB
#: head and 6 KiB tail, expressed once so a narrowed window keeps the same shape.
GATE_LOG_HEAD_FRACTION = 4

#: Ceiling on how many gate ids one :class:`GateSpec` may name.
#:
#: A cap rather than trust, because :attr:`GateSpec.gates` is the one field a
#: caller fills and every id in it costs a *whole toolchain run*: unbounded,
#: ``{"gates": ["pytest"] * 50}`` is fifty serial ``pytest`` invocations — hours
#: — inside one request, holding the session's slot for every one of them. That
#: is reachable both from the ``run_gates`` tool and from a plain
#: ``POST /api/validation/run``, so the bound belongs here, on the shape both
#: share, rather than in either caller.
#:
#: Repeats fold away first (:meth:`GateSpec._fold_repeats`), so this only ever
#: bites a caller naming more *distinct* ids than any catalog could hold;
#: ``test_gates.py`` asserts the shipped catalog fits inside it.
MAX_GATES_PER_RUN = 8


def head_bytes(window: int) -> int:
    """Bytes of the head half of a captured window (the rest is tail)."""
    return max(1, window // GATE_LOG_HEAD_FRACTION)


class GateCommand(BaseModel):
    """One runnable gate, as **server-owned data**.

    ``argv`` is never assembled from a request. The catalog in
    ``services/gates.py`` owns every one of these, an operator may choose *which*
    of them run (``WORKBENCH_GATES``) and how long they may take
    (``WORKBENCH_GATE_TIMEOUT_S``), and a caller — REST or agent — may only name
    an :attr:`id`. Adding a fifth shape is one row in the catalog and one line in
    its test.
    """

    #: The handle a :class:`GateSpec` names, e.g. ``"ruff"``.
    id: str
    #: The exact process, already split. Run with
    #: ``asyncio.create_subprocess_exec`` — never ``shell=True``, so no part of it
    #: is ever interpreted by a shell.
    argv: tuple[str, ...]
    #: What the evidence line is called, e.g. ``"ruff check ."``.
    label: str
    #: Hard ceiling on this gate, in seconds. A gate that hangs would hang the
    #: request that started it and, through it, the lifespan shutdown.
    timeout_s: float
    #: Exit codes that mean "this gate is satisfied". Almost always ``(0,)``;
    #: named so a tool that reports "no tests ran" as 5 can be admitted without a
    #: special case in the runner.
    pass_codes: tuple[int, ...] = (0,)


class GateSpec(BaseModel):
    """``ValidationSpec.params`` for check id ``"gates"``.

    Both fields are *selections*, not instructions: one names configured gates by
    id, the other sizes the captured window. Neither can express a command.
    """

    #: Ids from the catalog; empty runs the configured default set. An id that is
    #: not in the catalog becomes a ``fail`` evidence line naming what is
    #: available — never a silent skip (the frame's unregistered-check
    #: precedent). At most :data:`MAX_GATES_PER_RUN` of them, because each one is
    #: a whole toolchain run and a list is otherwise a way to ask for hours of
    #: them in a single request.
    gates: list[str] = Field(default_factory=list, max_length=MAX_GATES_PER_RUN)
    #: Bytes of each gate's output to capture. ``None`` is
    #: :data:`MAX_GATE_LOG_BYTES`, which is also the ceiling; a smaller value
    #: narrows the stored payload for a caller that wants a cheaper one. Clamped
    #: into ``[MIN_GATE_LOG_BYTES, MAX_GATE_LOG_BYTES]`` rather than rejected, so
    #: a typo costs a smaller log rather than a failed run.
    log_bytes: int | None = None

    @field_validator("gates", mode="before")
    @classmethod
    def _fold_repeats(cls, value: object) -> object:
        """Name a gate twice and it runs once — folded *before* the cap applies.

        A repeated id can only mean one thing, and honouring the repeat is pure
        cost: the second ``pytest`` judges the byte-for-byte tree the first one
        did. Folding here rather than in the check covers the REST body and the
        ``run_gates`` tool in one place, and keeps :data:`MAX_GATES_PER_RUN` a
        bound on *work* rather than on how verbosely the work was asked for.
        Order is preserved, so the caller still gets its gates in the sequence it
        named them. Anything that is not a list of strings is handed on
        untouched, so the ordinary type error is the one the caller sees.
        """
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return value
        return list(dict.fromkeys(value))

    def window(self) -> int:
        """The captured window this spec asks for, clamped into the legal band."""
        if self.log_bytes is None:
            return MAX_GATE_LOG_BYTES
        return max(MIN_GATE_LOG_BYTES, min(self.log_bytes, MAX_GATE_LOG_BYTES))


class GateLog(BaseModel):
    """The payload behind one ``gate`` :class:`EvidenceItem`.

    Reachable over HTTP as ``GET /api/validation/payload/gate/{ref}`` (through
    :class:`~workbench_server.models.evidence.EvidencePayload`), which is what
    makes a failing gate readable by the human who has to approve it. A gate
    whose log a human cannot read is a gate they have to take on faith.
    """

    #: The catalog id that produced this log.
    gate: str
    #: The exact process that ran, echoed so the evidence is self-describing.
    argv: list[str]
    #: ``None`` means the gate timed out or could not start — distinct from a
    #: non-zero code, and the detail line says which.
    exit_code: int | None
    duration_ms: int
    #: Head + tail capture of stdout and stderr interleaved, byte-bounded.
    text: str
    #: Set when the window bit: how many bytes were withheld and that
    #: ``log_bytes`` widens it (AXI shape 1). ``None`` means the log is whole.
    truncated: EvidenceTruncation | None = None


class SlotRef(BaseModel):
    """Which checkout a session is writing in.

    Produced by ``OrchestratorService.slot_of`` and consumed by the gate check
    through the :class:`~workbench_server.services.gates.SlotLocator` protocol,
    so ``services/gates.py`` never imports the orchestrator.
    """

    #: Pool slot name, ``None`` for a session working outside the pool.
    slot: str | None
    #: The checkout the gate runs in. Absolute, server-resolved: it is read from
    #: the roster, never from a request.
    path: str
    #: The commit the slot was leased at — the diff's other end (PR 2 reads it).
    #: Empty when the pool could not say.
    base: str = ""


class GateRunReport(BaseModel):
    """One whole gate run: where it ran, what it judged, and every log.

    Internal rather than a wire body — the evidence lines and their per-gate
    payloads are what travel — but typed all the same, because it is the value
    the check turns into evidence and the ``run_gates`` tool summarises.
    """

    path: str
    slot: str | None
    #: The commit judged. The same value on both sides of the run, or the run is
    #: reported as ``skipped`` rather than attributed to a tree that moved.
    head: str
    gates: list[GateLog] = Field(default_factory=list)
    started_at: datetime
    duration_ms: int
