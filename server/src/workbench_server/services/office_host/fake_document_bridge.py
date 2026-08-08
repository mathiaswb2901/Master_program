"""In-process fake document bridge (``WORKBENCH_OFFICE_FAKE=1``).

The counterpart of :class:`~workbench_server.services.office_host.fake_backend.FakeHostBackend`,
one seam over: a :class:`~workbench_server.services.office_host.document_bridge.DocumentBridge`
that returns deterministic, in-memory Word and Excel content and **never touches
Office**. It shares the fake backend so a read is answered for exactly the pids
that backend launched — the fake stand-in for a real COM read reaching into the
live instance a pid names.

Content starts minted from the *name* of the document, but a write **mutates the
in-memory copy in place**, keyed by the pid, so a subsequent read reflects the
edit exactly as a COM write into the live instance would — the whole point of the
write seam. The mint is the seed the first access materialises; every read and
write after that goes through the mutable overlay, and it dies with the pid.

Content is minted from the *name* of the document, so every read branch is
reachable in CI and, later, drivable from a test that just opens the right file
name (the ``FAILURE_TRIGGERS`` precedent):

* Word, ordinary -> a short multi-paragraph memo.
* ``…empty…`` (Word) -> no paragraphs, so a read says "none".
* Excel, ordinary -> a small ``Budget`` sheet that fits in one window and a
  2000-row ``Forecast`` sheet that does not, so the same document exercises both
  the whole-range read and the windowed one.
* ``…empty…`` / ``…blank…`` (Excel) -> a single sheet with no used range.
* ``…notes…`` (Excel) -> a ``Notes`` sheet of few but very long non-ASCII cells,
  so a read exercises the aggregate-text bound, not just the cell-count cap.
* an instance that has been killed -> :class:`DocGoneError`, the read racing a
  close.
"""

from pathlib import Path

import structlog

from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    SheetDim,
    WordEdit,
    WordText,
)
from workbench_server.models.office_host import HostAppKind
from workbench_server.services.office_host.a1 import cell_ref
from workbench_server.services.office_host.backend import HostHandle
from workbench_server.services.office_host.document_bridge import (
    DocGoneError,
    DocNotReadableError,
)
from workbench_server.services.office_host.document_window import (
    Grid,
    check_paragraph,
    no_sheet_error,
    parse_write_cell,
    used_dims,
    window_cells,
    window_word,
)
from workbench_server.services.office_host.fake_backend import FakeHostBackend

log = structlog.get_logger()


def _titleize(stem: str) -> str:
    words = stem.replace("_", " ").replace("-", " ").split()
    return " ".join(word.capitalize() for word in words) or "Untitled"


def word_paragraphs(name: str) -> list[str]:
    """The body a Word document of this name would have."""
    if "empty" in name.lower():
        return []
    stem = Path(name).stem
    return [
        _titleize(stem),
        f"This memo, {stem}, is the live document docked in a Workbench panel; "
        "reading it here returns exactly what is on screen, including edits the "
        "user has not yet saved to disk.",
        "The first finding concerns delivery-hour normalisation across the autumn "
        "DST boundary, where the 25-hour day must not be folded into 24.",
        "The second finding is unit consistency: figures quoted in MWh sat beside "
        "a table in MW, and the two have now been reconciled.",
        "In closing, the recommended change is small, reversible, and covered by "
        "the regression that the reproduction became.",
    ]


def excel_sheets(name: str) -> dict[str, Grid]:
    """The worksheets a workbook of this name would have."""
    low = name.lower()
    if "empty" in low or "blank" in low:
        return {"Sheet1": {}}
    if "notes" in low:
        # A sheet whose used range is tiny by *cell count* but huge by text: a
        # notes column of very long cells. It exercises the aggregate-text bound
        # a cell-count cap alone cannot enforce — one such cell can hold Excel's
        # 32,767-char maximum.
        notes: Grid = {(0, 0): "Item", (0, 1): "Note"}
        for row in range(1, 6):
            notes[(row, 0)] = f"row {row}"
            notes[(row, 1)] = "Åsen 2 " * 5_000  # ~35k chars of non-ASCII prose
        return {"Notes": notes}
    budget: Grid = {}
    for col, header in enumerate(("Month", "Revenue", "Cost", "Margin")):
        budget[(0, col)] = header
    for row in range(1, 6):
        budget[(row, 0)] = f"M{row}"
        budget[(row, 1)] = str(1000 * row)
        budget[(row, 2)] = str(400 * row)
        budget[(row, 3)] = str(600 * row)
    forecast: Grid = {}
    for col, header in enumerate(("Hour", "SE1", "SE2", "SE3", "SE4", "Load", "Wind", "Solar")):
        forecast[(0, col)] = header
    for row in range(1, 2000):
        forecast[(row, 0)] = str(row)
        for col in range(1, 8):
            forecast[(row, col)] = f"{row}.{col}"
    return {"Budget": budget, "Forecast": forecast}


class FakeDocumentBridge:
    """A scripted document reader. Satisfies the ``DocumentBridge`` protocol."""

    def __init__(self, backend: FakeHostBackend) -> None:
        #: Shared with the host backend: the read is answered for the pids it
        #: launched, and dies with them.
        self._backend = backend
        #: The mutable in-memory copy a write edits, materialised lazily from the
        #: name-derived mint on first access and keyed by pid, so a read after a
        #: write sees the edit — the fake stand-in for the live COM instance.
        self._word_docs: dict[int, list[str]] = {}
        self._excel_docs: dict[int, dict[str, Grid]] = {}

    def ready(self) -> bool:
        return True

    def _name(self, handle: HostHandle) -> str:
        """The document this pid opened, or :class:`DocGoneError` if it is gone."""
        if not self._backend.is_alive(handle.pid):
            raise DocGoneError(f"the instance for pid {handle.pid} has closed")
        name = self._backend.launched_paths.get(handle.pid)
        if name is None:  # pragma: no cover - is_alive already excludes unknown pids
            raise DocGoneError(f"no document is open for pid {handle.pid}")
        return name

    def _word_body(self, handle: HostHandle) -> list[str]:
        """The live paragraph list for this pid — minted once, mutated thereafter."""
        name = self._name(handle)
        if handle.pid not in self._word_docs:
            self._word_docs[handle.pid] = word_paragraphs(name)
        return self._word_docs[handle.pid]

    def _excel_book(self, handle: HostHandle) -> dict[str, Grid]:
        """The live worksheets for this pid — minted once, mutated thereafter."""
        name = self._name(handle)
        if handle.pid not in self._excel_docs:
            self._excel_docs[handle.pid] = excel_sheets(name)
        return self._excel_docs[handle.pid]

    async def structure(self, handle: HostHandle, kind: HostAppKind) -> DocStructure:
        if kind == "word":
            return DocStructure(kind="word", paragraph_count=len(self._word_body(handle)))
        if kind == "excel":
            sheets = [
                SheetDim(name=sheet, rows=rows, cols=cols)
                for sheet, grid in self._excel_book(handle).items()
                for rows, cols in (used_dims(grid),)
            ]
            return DocStructure(kind="excel", sheets=sheets)
        raise DocNotReadableError(f"{kind} documents cannot be read")

    async def read_word(self, handle: HostHandle, start_paragraph: int, max_chars: int) -> WordText:
        return window_word(self._word_body(handle), start_paragraph, max_chars)

    async def read_excel(
        self, handle: HostHandle, sheet: str, a1_range: str | None, max_cells: int, max_chars: int
    ) -> CellWindow:
        sheets = self._excel_book(handle)
        if sheet not in sheets:
            raise no_sheet_error(sheet, list(sheets))
        return window_cells(sheet, sheets[sheet], a1_range, max_cells, max_chars)

    async def write_word(self, handle: HostHandle, paragraph: int, text: str) -> WordEdit:
        paragraphs = self._word_body(handle)
        check_paragraph(paragraph, len(paragraphs))
        # Replace exactly the one addressed paragraph; every other paragraph, and
        # the document's shape, is left untouched — the fidelity the seam owes.
        paragraphs[paragraph] = text
        return WordEdit(
            paragraph=paragraph, written_chars=len(text), total_paragraphs=len(paragraphs)
        )

    async def write_excel(self, handle: HostHandle, sheet: str, cell: str, value: str) -> CellEdit:
        sheets = self._excel_book(handle)
        if sheet not in sheets:
            raise no_sheet_error(sheet, list(sheets))
        row, col = parse_write_cell(cell)
        grid = sheets[sheet]
        # Set exactly the one addressed cell; empty value clears it (mirroring a
        # COM ``Range.Value = ""``), so the used range can shrink but no other
        # cell is disturbed.
        if value == "":
            grid.pop((row, col), None)
        else:
            grid[(row, col)] = value
        return CellEdit(sheet=sheet, a1_cell=cell_ref(row, col), written_chars=len(value))
