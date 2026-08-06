"""The document-read contract — the COM bridge seam, read half only.

This is to reading the *live* hosted document what ``backend.py`` is to hosting
its window: the small, honest seam the risky native work plugs into. Above it —
the service methods, the ``office_read`` agent tool, the models — is plain Python
that runs and is tested on a machine with no Microsoft Office and no Rust,
against the in-process ``FakeDocumentBridge`` (``fake_document_bridge.py``).
Below it, in a later PR, will be the Win32/COM implementation that reads the same
Word/Excel instance ``shell_backend.py`` launched, off the single COM apartment
thread that backend already owns.

**The contract is read-only, deliberately.** There is no write, no edit, no
save: mutating the live document is a separate seam that arrives with PR 3, and
inventing it here would let the domain layer pass tests the native side cannot
satisfy — the same discipline ``backend.py`` keeps.

**Every method is bounded by the caller above it.** Like a
:class:`~workbench_server.services.office_host.backend.HostBackend`, an
implementation runs its blocking COM calls on a worker thread and the service
applies its own per-call ceiling, cancelling a read that never returns — a Word
thinking about a modal must not hang the request that started it.

Errors mirror ``backend.py``: one base carrying the
:data:`DocReason` the read settles with, so a caller maps failures without a
chain of ``isinstance`` checks and a new failure mode cannot arrive without
naming itself.
"""

from typing import Literal, Protocol

from workbench_server.models.office_bridge import CellWindow, DocStructure, WordText
from workbench_server.models.office_host import HostAppKind
from workbench_server.services.office_host.backend import HostHandle

#: Why a read refused. ``document_not_hosted`` means Workbench has no live host
#: for the path — the agent must open it first; ``document_not_readable`` is the
#: catch-all it can otherwise act on ("the window is not one we launched", "it is
#: still opening"); ``range_invalid`` names a bad sheet or A1 range the agent can
#: fix; ``document_gone`` means the instance closed underneath the read.
DocReason = Literal[
    "document_not_hosted", "document_not_readable", "range_invalid", "document_gone"
]


class DocumentBridgeError(Exception):
    """Base for the refusals a document read is allowed to report.

    Raised by the bridge itself and by the service method that guards it, so the
    ``office_read`` tool maps every read failure from one family and a new one
    cannot arrive without naming its :data:`DocReason`.
    """

    reason: DocReason = "document_not_readable"


class DocNotHostedError(DocumentBridgeError):
    """No live host for this document — it has to be opened before it is read."""

    reason: DocReason = "document_not_hosted"


class DocNotReadableError(DocumentBridgeError):
    """The document exists but cannot be read right now.

    The window is not the instance we launched, the document is still opening,
    or no native reader is available on this machine.
    """

    reason: DocReason = "document_not_readable"


class RangeInvalidError(DocumentBridgeError):
    """The sheet name or A1 range does not resolve — the agent can fix and retry."""

    reason: DocReason = "range_invalid"


class DocGoneError(DocumentBridgeError):
    """The instance closed while the read was in flight — nothing to read."""

    reason: DocReason = "document_gone"


class DocumentBridge(Protocol):
    """What the native document reader must provide, and nothing more."""

    def ready(self) -> bool:
        """Can this bridge read a hosted document right now?

        Like ``HostBackend.ready``, this changes while the server runs — the
        real bridge needs the COM apartment and a live instance to read — so it
        is asked, never inferred.
        """
        ...

    async def structure(self, handle: HostHandle, kind: HostAppKind) -> DocStructure:
        """The document's shape: Word paragraph count, or Excel sheet dimensions.

        Raises :class:`DocGoneError` if the instance has closed.
        """
        ...

    async def read_word(self, handle: HostHandle, start_paragraph: int, max_chars: int) -> WordText:
        """Read the Word body from ``start_paragraph``, up to ``max_chars``.

        Whole paragraphs, joined by blank lines; stops before ``max_chars`` is
        exceeded and reports what it did not reach. Raises
        :class:`RangeInvalidError` if ``start_paragraph`` is past the end of a
        non-empty document, :class:`DocGoneError` if the instance has closed.
        """
        ...

    async def read_excel(
        self, handle: HostHandle, sheet: str, a1_range: str | None, max_cells: int
    ) -> CellWindow:
        """Read a window of ``sheet``, at most ``max_cells`` cells.

        ``a1_range`` selects the corner or rectangle to read; ``None`` starts at
        A1. The window is trimmed to ``max_cells`` and reports the whole used
        range so the caller can ask for the rest. Raises
        :class:`RangeInvalidError` for an unknown sheet or a malformed range,
        :class:`DocGoneError` if the instance has closed.
        """
        ...
