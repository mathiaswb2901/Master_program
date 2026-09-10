"""The real document bridge: read and write the *live* docked Word/Excel over COM.

The implementation the :class:`~...document_bridge.DocumentBridge` Protocol was
written for, and the mirror of :class:`~...fake_document_bridge.FakeDocumentBridge`
one seam over. Where the fake mints content from a document's name and mutates an
in-memory overlay, this reaches the real in-memory instance the
:class:`~...shell_backend.ShellHostBackend` launched and is holding — the same
Word or Excel the user sees in the panel, including edits they have not saved.

**It owns no COM of its own.** A COM proxy belongs to the apartment that created
it, so every read and write runs on the shell backend's single apartment thread
(:meth:`ShellHostBackend.run_com`), against the instance that backend is holding
(:meth:`ShellHostBackend.instance_for`) — never a fresh ``DispatchEx``. That is
the same discipline the launch, the poll and the reap already follow, and the
reason this bridge is constructed *from* the backend rather than beside it.

**Off the event loop, and honest when it cannot answer.** Like the read half
(#65), the blocking COM work is pushed to that worker thread and bounded by the
service's per-call ceiling above; a missing instance, a dead COM object or a bad
address becomes the same typed :class:`~...document_bridge.DocumentBridgeError`
the fake and the service already raise, never a hang and never an unhandled COM
exception out of the tool.

**Fidelity.** A Word paragraph write replaces only that paragraph's range and
keeps its paragraph mark; a Word insert adds exactly one paragraph and refuses an
anchor whose mark is a table cell's structure; an Excel cell write sets only that
cell. None rewrites the file — the live instance changes and the user can undo it
(the CLAUDE.md rule). The windowing that shapes a read, the addressing that
resolves an insert and the refusals both raise are the *same* code the fake runs
(``document_window.py``), so the two produce the identical shape.
"""

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    LiveWorkbookStatus,
    SheetDim,
    SlideText,
    WordEdit,
    WordParagraphStyle,
    WordText,
)
from workbench_server.models.office_host import HostAppKind
from workbench_server.services.office_host import office_com
from workbench_server.services.office_host.a1 import cell_ref
from workbench_server.services.office_host.backend import HostHandle
from workbench_server.services.office_host.document_bridge import (
    DocGoneError,
    DocNotReadableError,
    DocumentBridgeError,
)
from workbench_server.services.office_host.document_window import (
    check_insert_text,
    check_paragraph,
    no_sheet_error,
    parse_write_cell,
    resolve_insert_index,
    slide_dims,
    used_dims,
    window_cells,
    window_slides,
    window_word,
)
from workbench_server.services.office_host.office_com import OfficeInstance

if TYPE_CHECKING:
    from workbench_server.services.office_host.shell_backend import ShellHostBackend

log = structlog.get_logger()

T = TypeVar("T")


class ShellDocumentBridge:
    """A COM-backed document reader/writer. Satisfies the ``DocumentBridge`` protocol."""

    def __init__(self, backend: "ShellHostBackend") -> None:
        #: Shared with the host backend: reads are answered for exactly the pids
        #: it launched and run on the one thread that owns their COM proxies.
        self._backend = backend

    def ready(self) -> bool:
        """COM is only reachable on Windows; the bridge is only built there, so
        this is the honest floor rather than a formality the service reads."""
        return office_com.WINDOWS

    async def structure(self, handle: HostHandle, kind: HostAppKind) -> DocStructure:
        def work(instance: OfficeInstance) -> DocStructure:
            if kind == "word":
                return DocStructure(
                    kind="word", paragraph_count=office_com.word_paragraph_count(instance)
                )
            if kind == "excel":
                sheets: list[SheetDim] = []
                for name in office_com.excel_sheet_names(instance):
                    worksheet = office_com.excel_worksheet(instance, name)
                    rows, cols = used_dims(office_com.excel_grid(worksheet))
                    sheets.append(SheetDim(name=name, rows=rows, cols=cols))
                return DocStructure(kind="excel", sheets=sheets)
            return DocStructure(
                kind="powerpoint", slides=slide_dims(office_com.powerpoint_slides(instance))
            )

        return await self._com(handle, work)

    async def read_word(self, handle: HostHandle, start_paragraph: int, max_chars: int) -> WordText:
        def work(instance: OfficeInstance) -> WordText:
            return window_word(
                office_com.word_paragraph_texts(instance), start_paragraph, max_chars
            )

        return await self._com(handle, work)

    async def read_excel(
        self, handle: HostHandle, sheet: str, a1_range: str | None, max_cells: int, max_chars: int
    ) -> CellWindow:
        def work(instance: OfficeInstance) -> CellWindow:
            worksheet = office_com.excel_worksheet(instance, sheet)
            if worksheet is None:
                raise no_sheet_error(sheet, office_com.excel_sheet_names(instance))
            return window_cells(
                sheet, office_com.excel_grid(worksheet), a1_range, max_cells, max_chars
            )

        return await self._com(handle, work)

    async def read_powerpoint(
        self, handle: HostHandle, start_slide: int, max_chars: int
    ) -> SlideText:
        def work(instance: OfficeInstance) -> SlideText:
            return window_slides(office_com.powerpoint_slides(instance), start_slide, max_chars)

        return await self._com(handle, work)

    async def write_word(self, handle: HostHandle, paragraph: int, text: str) -> WordEdit:
        def work(instance: OfficeInstance) -> WordEdit:
            total = office_com.word_paragraph_count(instance)
            check_paragraph(paragraph, total)
            # Scoped to the one paragraph; a replace never changes the count.
            office_com.set_word_paragraph_text(instance, paragraph, text)
            return WordEdit(paragraph=paragraph, written_chars=len(text), total_paragraphs=total)

        return await self._com(handle, work)

    async def insert_word(
        self,
        handle: HostHandle,
        after_paragraph: int | None,
        text: str,
        style: WordParagraphStyle | None,
    ) -> WordEdit:
        def work(instance: OfficeInstance) -> WordEdit:
            total = office_com.word_paragraph_count(instance)
            index = resolve_insert_index(after_paragraph, total)
            check_insert_text(text)
            applied = office_com.insert_word_paragraph(instance, index, text, style)
            # The count is *re-read*, never computed as ``total + 1``. Word is the
            # authority on what the document now contains, and a number this seam
            # invented is exactly the kind that stays plausible while being wrong
            # — which the caller would then address from.
            return WordEdit(
                paragraph=index,
                written_chars=len(text),
                total_paragraphs=office_com.word_paragraph_count(instance),
                op="insert",
                style=applied,
            )

        return await self._com(handle, work)

    async def write_excel(self, handle: HostHandle, sheet: str, cell: str, value: str) -> CellEdit:
        def work(instance: OfficeInstance) -> CellEdit:
            worksheet = office_com.excel_worksheet(instance, sheet)
            if worksheet is None:
                raise no_sheet_error(sheet, office_com.excel_sheet_names(instance))
            row, col = parse_write_cell(cell)
            office_com.set_excel_cell(worksheet, row, col, value)
            return CellEdit(sheet=sheet, a1_cell=cell_ref(row, col), written_chars=len(value))

        return await self._com(handle, work)

    # ---- the value-typed read (the reconciliation seam) ---------------------

    async def workbook_status(self, handle: HostHandle) -> LiveWorkbookStatus:
        def work(instance: OfficeInstance) -> LiveWorkbookStatus:
            return LiveWorkbookStatus(
                saved=office_com.workbook_saved(instance),
                calculation=office_com.calculation_state(instance),
            )

        return await self._com(handle, work)

    async def read_cells(
        self, handle: HostHandle, sheet: str | None, cells: Sequence[str]
    ) -> list[Any]:
        def work(instance: OfficeInstance) -> list[Any]:
            worksheet = self._worksheet(instance, sheet)
            values: list[Any] = []
            for address in cells:
                # One `Range` per address rather than one call for the union:
                # the addresses are arbitrary and unordered, and a union range's
                # `Value` collapses to its first area, which would silently
                # score every expectation against the wrong cell.
                rectangle = office_com.excel_range_values(worksheet, address)
                values.append(rectangle[0][0] if rectangle and rectangle[0] else None)
            return values

        return await self._com(handle, work)

    async def read_columns(
        self,
        handle: HostHandle,
        sheet: str | None,
        ts_column: str,
        value_column: str,
        start_row: int,
        max_rows: int,
    ) -> list[tuple[Any, Any]]:
        def work(instance: OfficeInstance) -> list[tuple[Any, Any]]:
            worksheet = self._worksheet(instance, sheet)
            last = office_com.excel_used_rows(worksheet)
            end = min(last, start_row + max_rows - 1)
            if end < start_row:
                return []
            stamps = office_com.excel_range_values(
                worksheet, f"{ts_column}{start_row}:{ts_column}{end}"
            )
            values = office_com.excel_range_values(
                worksheet, f"{value_column}{start_row}:{value_column}{end}"
            )
            # The window, verbatim — blank rows included. Where the *data* ends
            # is not this layer's call: the reconciliation seam applies one rule
            # (`column_data_rows`) to whichever bridge answered, so a live read
            # and a read of the same file on disk cover the same rows. A bridge
            # that trimmed on its own would be a second copy of that rule, and
            # two copies is what let a live read keep rows disk had dropped.
            return [
                (
                    stamps[row][0] if row < len(stamps) and stamps[row] else None,
                    values[row][0] if row < len(values) and values[row] else None,
                )
                for row in range(end - start_row + 1)
            ]

        return await self._com(handle, work)

    def _worksheet(self, instance: OfficeInstance, sheet: str | None) -> Any:
        """The named worksheet, or the active one when ``sheet`` is None — the
        disk reader's own convention, so the two implementations of
        ``WorkbookReader`` address the same cells for the same spec."""
        if sheet is None:
            return instance.document.ActiveSheet
        worksheet = office_com.excel_worksheet(instance, sheet)
        if worksheet is None:
            raise no_sheet_error(sheet, office_com.excel_sheet_names(instance))
        return worksheet

    async def _com(self, handle: HostHandle, work: Callable[[OfficeInstance], T]) -> T:
        """Run one COM operation on the backend's apartment thread, mapping every
        failure to the typed refusal the seam owes.

        The instance lookup is the first gate: a pid the backend no longer holds
        is a document that has been closed and reaped, which is ``document_gone``
        — never a proxy reached for and found dead. Anything the COM work itself
        raises is either an already-typed refusal (a bad address), an object that
        died mid-call (``document_gone``), or an otherwise-failed read the agent
        can still act on (``document_not_readable``); none escapes as a raw COM
        exception."""
        instance = self._backend.instance_for(handle.pid)
        if instance is None:
            raise DocGoneError(f"the instance for pid {handle.pid} has closed")

        def run() -> T:
            try:
                return work(instance)
            except DocumentBridgeError:
                raise
            except Exception as error:
                if office_com.is_object_gone(error):
                    raise DocGoneError(
                        f"the document closed while it was being accessed: {error}"
                    ) from error
                raise DocNotReadableError(
                    f"the live document could not be reached over COM: {error}"
                ) from error

        return await self._backend.run_com(run)
