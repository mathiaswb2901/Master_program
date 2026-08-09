"""The document read/write contract — the COM bridge seam.

This is to reading and editing the *live* hosted document what ``backend.py`` is
to hosting its window: the small, honest seam the risky native work plugs into.
Above it — the service methods, the ``office_read`` and ``office_write`` agent
tools, the models — is plain Python that runs and is tested on a machine with no
Microsoft Office and no Rust, against the in-process ``FakeDocumentBridge``
(``fake_document_bridge.py``). Below it, in a later PR, will be the Win32/COM
implementation that reaches into the same Word/Excel instance
``shell_backend.py`` launched, off the single COM apartment thread that backend
already owns.

**The read half (PR 1) came before the write half (PR 2).** The two writes below
— :meth:`DocumentBridge.write_word` and :meth:`DocumentBridge.write_excel` — are
the mirror of the reads: each is a *targeted* edit of one addressed paragraph or
one cell that leaves the rest of the document untouched, applied to the live
in-memory instance (the user can undo it) rather than rewriting the file. They
are still bounded by the seam the same way — inventing anything the native side
cannot satisfy is the mistake ``backend.py`` exists to prevent.

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

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    LiveWorkbookStatus,
    SlideText,
    WordEdit,
    WordText,
)
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
        self, handle: HostHandle, sheet: str, a1_range: str | None, max_cells: int, max_chars: int
    ) -> CellWindow:
        """Read a window of ``sheet``, at most ``max_cells`` cells.

        ``a1_range`` selects the corner or rectangle to read; ``None`` starts at
        A1. The window is trimmed to ``max_cells`` *and* to ``max_chars`` of
        aggregate cell text — the count cap alone does not bound a sheet whose
        cells hold long text (a notes column, a 32k-char cell), so both bound the
        result and each cell is truncated so one long cell cannot fill the window
        by itself. Reports the whole used range so the caller can ask for the
        rest. Raises :class:`RangeInvalidError` for an unknown sheet or a
        malformed range, :class:`DocGoneError` if the instance has closed.
        """
        ...

    async def read_powerpoint(
        self, handle: HostHandle, start_slide: int, max_chars: int
    ) -> SlideText:
        """Read the deck's text from ``start_slide`` (one-based), up to ``max_chars``.

        Whole slides, joined by blank lines, each headed by its number and title;
        stops before ``max_chars`` is exceeded and reports what it did not reach.
        Raises :class:`RangeInvalidError` if ``start_slide`` is outside a
        non-empty deck, :class:`DocGoneError` if the instance has closed.

        There is no ``write_powerpoint`` beside this one, deliberately: see
        ``models/office_bridge.py`` for why a slide has no addressable write
        target yet.
        """
        ...

    async def write_word(self, handle: HostHandle, paragraph: int, text: str) -> WordEdit:
        """Replace the text of one addressed Word paragraph, nothing else.

        A *targeted* edit — the real COM side sets ``Range.Text`` on the one
        paragraph and leaves every other paragraph, and the file, untouched;
        empty ``text`` empties the paragraph without removing it. Raises
        :class:`RangeInvalidError` when ``paragraph`` is out of range (including
        an empty document, which has no paragraph to replace), and
        :class:`DocGoneError` if the instance has closed.
        """
        ...

    async def write_excel(self, handle: HostHandle, sheet: str, cell: str, value: str) -> CellEdit:
        """Set the text of one addressed Excel cell, nothing else.

        A *targeted* edit — the real COM side assigns ``Range(cell).Value`` and
        leaves every other cell, and the file, untouched; empty ``value`` clears
        the cell. Raises :class:`RangeInvalidError` for an unknown sheet or a
        malformed cell address, and :class:`DocGoneError` if the instance has
        closed.
        """
        ...

    # ---- the value-typed read (the reconciliation seam) ---------------------
    #
    # Three methods that exist for one caller: the workbook↔code reconciliation
    # gate (``services/reconciliation.py``), which needs *numbers* rather than
    # the text the three reads above return. They are deliberately separate from
    # ``read_excel``: that one renders a window a person looks at, and its
    # ``Grid`` is ``str`` all the way down. Reconciling parsed-back strings would
    # throw away the precision a tolerance band is about.
    #
    # ``workbook_status`` is cheap and answered first, on purpose. It is two
    # property reads with no range in them, so a *bulk* read that fails — a
    # modal, a timeout, a range the sheet does not have — can still be told
    # apart from "the workbook is clean and the file will do". That distinction
    # is the whole front gate: on a dirty workbook a failed live read must be a
    # refusal, never a quiet fall back to disk.

    async def workbook_status(self, handle: HostHandle) -> LiveWorkbookStatus:
        """Whether the live workbook has unsaved edits, and whether Excel has
        finished calculating.

        Raises :class:`DocNotReadableError` when this bridge cannot answer at
        all, :class:`DocGoneError` if the instance has closed. It never guesses:
        a status nobody could read is a refusal, because the caller's only other
        option is to trust a file that may be stale.
        """
        ...

    async def read_cells(
        self, handle: HostHandle, sheet: str | None, cells: Sequence[str]
    ) -> list[Any]:
        """The **typed** values of the named A1 cells, in the order asked.

        ``sheet`` is ``None`` for the active sheet, matching the disk reader's
        own convention. A blank cell is ``None``; a number stays a number and a
        date stays a naive ``datetime`` (the COM side drops the offset it
        attaches — ``office_com.naive_local``). Raises
        :class:`RangeInvalidError` for an unknown sheet or a malformed address.
        """
        ...

    async def read_columns(
        self,
        handle: HostHandle,
        sheet: str | None,
        ts_column: str,
        value_column: str,
        start_row: int,
        max_rows: int,
    ) -> list[tuple[Any, Any]]:
        """Two whole columns as ``(timestamp, value)`` pairs, typed, from
        ``start_row`` down to the sheet's last used row.

        **The window verbatim, blank rows included.** A bridge is a transport
        here and decides nothing about where the column's data ends — that is one
        rule, applied once, by the reconciliation reader seam
        (``services/reconciliation.py``'s ``column_data_rows``) to whichever
        bridge answered. It has to be one rule because the disk reader applies it
        too: a bridge that trimmed on its own would make the same workbook
        reconcile a different set of rows docked than undocked, which is the
        provenance promise ``ReadSource`` exists to make.

        ``max_rows`` is a hard ceiling on the rows read; a taller column is cut
        there, which can only ever surface as an unmatched expectation (an
        alignment gap) and never as a pass — truncation must not be able to
        manufacture agreement.
        """
        ...
