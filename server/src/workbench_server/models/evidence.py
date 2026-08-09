"""The evidence-payload envelope: what ``payload_ref`` actually resolves to.

The #82 frame stores a check's detail payload in a bounded per-kind LRU and puts
only a *reference* on the :class:`~workbench_server.models.validation.EvidenceItem`
— so a big reconciliation table never rides the result or the ``/ws/events``
frame. It shipped no route to redeem that reference, which was survivable for
reconciliation (the grouped line carries the counts) and is not for a toolchain
gate: the entire value of a failing gate is its captured output. PR 1 closes the
gap, and this envelope is what ``GET /api/validation/payload/{kind}/{ref}``
returns.

**Why this is its own module and not an append to ``models/validation.py``.**
``models/reconciliation.py`` already imports ``EvidenceTruncation`` *from*
``models/validation.py``, and ``models/gates.py`` does the same. An envelope that
names both payload types has to sit **downstream** of both, or the imports cycle.

**Why one optional field per kind and not a discriminated union.** A union needs
a literal discriminator field added to every existing payload model, which
changes a shipped wire shape for no gain the UI can feel. Adding a kind here is
one optional field plus one narrowing branch in the router — the same one-line
append ``ValidationEvent`` took to the bus.
"""

from __future__ import annotations

from pydantic import BaseModel

from workbench_server.models.gates import GateLog
from workbench_server.models.reconciliation import ReconciliationReport
from workbench_server.models.review import ReviewReport
from workbench_server.models.validation import EvidenceKind


class EvidencePayload(BaseModel):
    """``GET /api/validation/payload/{kind}/{ref}`` — the detail behind one
    ``EvidenceItem.payload_ref``.

    **404 once the LRU has dropped it.** The store is bounded and honest about
    it: the Review panel renders the 404 as "this log has been evicted", never as
    a spinner that never resolves.

    Exactly one of the payload fields is set, chosen by ``kind``. Both ``None``
    would mean the server holds a shape this version cannot describe, which the
    router refuses rather than returning as an emptiness a client must interpret.
    """

    #: Which per-kind store the ref was redeemed from — echoed so a client that
    #: holds several refs can key on the whole answer.
    kind: EvidenceKind
    ref: str
    #: Set for ``kind == "numeric"``: the workbook↔code comparison table.
    reconciliation: ReconciliationReport | None = None
    #: Set for ``kind == "gate"``: one gate's bounded head+tail log.
    gate_log: GateLog | None = None
    #: Set for ``kind == "diff"``: one adversarial review's findings (PR 2).
    #: The one field PR 2 said it would append, appended — the envelope's whole
    #: design claim, which was that adding a kind costs one optional field here
    #: and one narrowing branch in the router.
    review: ReviewReport | None = None
