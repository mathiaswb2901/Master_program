"""In-process fake document bridge (``WORKBENCH_OFFICE_FAKE=1``).

The counterpart of :class:`~workbench_server.services.office_host.fake_backend.FakeHostBackend`,
one seam over: a :class:`~workbench_server.services.office_host.document_bridge.DocumentBridge`
that returns deterministic, in-memory Word and Excel content and **never touches
Office**. It shares the fake backend so a read is answered for exactly the pids
that backend launched — the fake stand-in for a real COM read reaching into the
live instance a pid names.

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

from workbench_server.models.office_bridge import CellWindow, DocStructure, SheetDim, WordText
from workbench_server.models.office_host import HostAppKind
from workbench_server.services.office_host.a1 import cell_ref, parse_range
from workbench_server.services.office_host.backend import HostHandle
from workbench_server.services.office_host.document_bridge import (
    DocGoneError,
    DocNotReadableError,
    RangeInvalidError,
)
from workbench_server.services.office_host.fake_backend import FakeHostBackend

log = structlog.get_logger()

#: One worksheet as a sparse ``(row, col) -> text`` map, zero-based.
Grid = dict[tuple[int, int], str]


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


def _used_dims(grid: Grid) -> tuple[int, int]:
    if not grid:
        return 0, 0
    return max(row for row, _ in grid) + 1, max(col for _, col in grid) + 1


def _cap_cell(value: str, limit: int) -> str:
    """One cell's text, truncated to ``limit`` chars with an ellipsis if cut.

    Shared by the real bridge's contract: no single cell may dominate the window,
    so a lone long cell is trimmed here rather than left to overrun the byte
    budget the tool clamps at the far end.
    """
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)] + "…"


class FakeDocumentBridge:
    """A scripted document reader. Satisfies the ``DocumentBridge`` protocol."""

    def __init__(self, backend: FakeHostBackend) -> None:
        #: Shared with the host backend: the read is answered for the pids it
        #: launched, and dies with them.
        self._backend = backend

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

    async def structure(self, handle: HostHandle, kind: HostAppKind) -> DocStructure:
        name = self._name(handle)
        if kind == "word":
            return DocStructure(kind="word", paragraph_count=len(word_paragraphs(name)))
        if kind == "excel":
            sheets = [
                SheetDim(name=sheet, rows=rows, cols=cols)
                for sheet, grid in excel_sheets(name).items()
                for rows, cols in (_used_dims(grid),)
            ]
            return DocStructure(kind="excel", sheets=sheets)
        raise DocNotReadableError(f"{kind} documents cannot be read")

    async def read_word(self, handle: HostHandle, start_paragraph: int, max_chars: int) -> WordText:
        paragraphs = word_paragraphs(self._name(handle))
        total = len(paragraphs)
        if total == 0:
            return WordText(start_paragraph=0, returned_chars=0, total_paragraphs=0, text="")
        if start_paragraph >= total:
            raise RangeInvalidError(
                f"start_paragraph {start_paragraph} is past the last paragraph ({total - 1})"
            )
        chunks: list[str] = []
        length = 0
        separator = "\n\n"
        for paragraph in paragraphs[start_paragraph:]:
            added = (len(separator) if chunks else 0) + len(paragraph)
            if not chunks and len(paragraph) > max_chars:
                # A lone paragraph longer than the whole window: truncate it, so
                # a read is never empty when there is text to show.
                chunks.append(paragraph[:max_chars])
                break
            if chunks and length + added > max_chars:
                break
            chunks.append(paragraph)
            length += added
        text = separator.join(chunks)
        return WordText(
            start_paragraph=start_paragraph,
            returned_chars=len(text),
            total_paragraphs=total,
            text=text,
        )

    async def read_excel(
        self, handle: HostHandle, sheet: str, a1_range: str | None, max_cells: int, max_chars: int
    ) -> CellWindow:
        sheets = excel_sheets(self._name(handle))
        if sheet not in sheets:
            raise RangeInvalidError(
                f"no sheet named {sheet!r}; sheets are {', '.join(sheets) or '(none)'}"
            )
        grid = sheets[sheet]
        total_rows, total_cols = _used_dims(grid)
        if total_rows == 0:
            return CellWindow(
                sheet=sheet,
                a1_range="",
                rows=0,
                cols=0,
                total_rows=0,
                total_cols=0,
                cells=[],
            )
        try:
            row1, col1, row2, col2 = parse_range(a1_range) if a1_range else (0, 0, None, None)
        except ValueError as error:
            raise RangeInvalidError(f"bad range {a1_range!r}: {error}") from error
        if row1 >= total_rows or col1 >= total_cols:
            raise RangeInvalidError(
                f"range starts at {cell_ref(row1, col1)}, past the used range "
                f"{cell_ref(total_rows - 1, total_cols - 1)}"
            )
        last_row = total_rows - 1 if row2 is None else min(row2, total_rows - 1)
        last_col = total_cols - 1 if col2 is None else min(col2, total_cols - 1)
        ncols = last_col - col1 + 1
        # Trim rows so the window fits the cell-count budget; never below one row,
        # so a read always shows something and says how to widen it.
        max_rows = max(1, max_cells // ncols)
        last_row = min(last_row, row1 + max_rows - 1)
        # Bound the *text volume* too, not just the cell count: a count cap alone
        # does not stop a single long cell (a notes column, up to 32k chars in
        # Excel) from blowing the window past the tool's byte budget on its own.
        # Cap each cell so a full row of ``ncols`` cells stays within ``max_chars``,
        # then stop adding rows once the aggregate would exceed it — keeping at
        # least the first row so the read is never empty.
        per_cell = max(1, max_chars // ncols)
        cells: list[list[str]] = []
        used = 0
        for row in range(row1, last_row + 1):
            row_cells = [
                _cap_cell(grid.get((row, col), ""), per_cell) for col in range(col1, last_col + 1)
            ]
            row_chars = sum(len(cell) for cell in row_cells)
            if cells and used + row_chars > max_chars:
                break
            cells.append(row_cells)
            used += row_chars
        last_row = row1 + len(cells) - 1
        return CellWindow(
            sheet=sheet,
            a1_range=f"{cell_ref(row1, col1)}:{cell_ref(last_row, last_col)}",
            rows=len(cells),
            cols=ncols,
            total_rows=total_rows,
            total_cols=total_cols,
            cells=cells,
        )
