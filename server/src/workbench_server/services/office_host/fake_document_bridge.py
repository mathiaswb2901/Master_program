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

The same mechanism scripts the three **live-workbook** states the reconciliation
front gate branches on, so every row of its table is reachable in CI with no
Office and no window:

* ``…dirty…``       -> ``Workbook.Saved = False``: the live instance holds edits
  the file on disk does not have.
* ``…calculating…`` -> ``CalculationState = calculating``: Excel has not finished
  working out what the formulas say.
* ``…nolive…``      -> the *bulk* value reads refuse
  (:class:`DocNotReadableError`) while ``workbook_status`` still answers. On real
  COM those two travel together; splitting them here is what makes the
  **blocked** row — docked, dirty, and no live read — reachable without Office.
  It is not only a test affordance: a status read is two properties and a column
  read is 8,760 cells, so a modal or a timeout really can take the second while
  the first succeeds, and on a dirty workbook that must be a refusal rather than
  a quiet fall back to the file.

**Values, not text.** The workbook is minted once as *typed* values
(:func:`excel_value_sheets`) and the text grid a window read renders is derived
from it — one store, exactly as a real Excel has one. That is what lets the
value-typed reconciliation read return ``1234.5`` as a float and a date as a
``datetime`` while ``office_read`` keeps returning the same strings it always
did.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    LiveWorkbookStatus,
    SheetDim,
    WordEdit,
    WordText,
)
from workbench_server.models.office_host import HostAppKind
from workbench_server.services.office_host.a1 import cell_ref, column_index, parse_cell
from workbench_server.services.office_host.backend import HostHandle
from workbench_server.services.office_host.document_bridge import (
    DocGoneError,
    DocNotReadableError,
    RangeInvalidError,
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


#: One worksheet as the *typed* values a live instance really holds: numbers as
#: numbers, timestamps as naive ``datetime``s, text as text. :data:`Grid` — the
#: ``str`` map ``office_read``'s window is built from — is derived from this.
ValueGrid = dict[tuple[int, int], object]

#: The naive local wall clock the ``Hours`` sheet is indexed by: the Nordic
#: fall-back day, whose 02:00 occurs twice. The reconciliation gate's DST rules
#: are the reason this fixture is that date and not an ordinary one.
_FALL_BACK_DAY = datetime(2024, 10, 27)


def _text_of(value: object) -> str:
    """One typed cell as the string a window read renders — the fake's mirror of
    ``office_com._cell_text``, so both bridges stringify identically."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def excel_value_sheets(name: str) -> dict[str, ValueGrid]:
    """The worksheets a workbook of this name would have, as **typed** values.

    The single mint. :func:`excel_sheets` renders this to text, and the bridge
    mutates this — so a write is visible to both reads, exactly as one edit in a
    real Excel is visible to a person looking at the cell and to a program
    reading ``Range.Value``.
    """
    low = name.lower()
    if "empty" in low or "blank" in low:
        return {"Sheet1": {}}
    if "notes" in low:
        # A sheet whose used range is tiny by *cell count* but huge by text: a
        # notes column of very long cells. It exercises the aggregate-text bound
        # a cell-count cap alone cannot enforce — one such cell can hold Excel's
        # 32,767-char maximum.
        notes: ValueGrid = {(0, 0): "Item", (0, 1): "Note"}
        for row in range(1, 6):
            notes[(row, 0)] = f"row {row}"
            notes[(row, 1)] = "Åsen 2 " * 5_000  # ~35k chars of non-ASCII prose
        return {"Notes": notes}
    budget: ValueGrid = {}
    for col, header in enumerate(("Month", "Revenue", "Cost", "Margin")):
        budget[(0, col)] = header
    for row in range(1, 6):
        budget[(row, 0)] = f"M{row}"
        # Floats, not their strings: this is the sheet the reconciliation gate
        # reads, and a tolerance band compared against `"1000"` is not a
        # comparison. They render to exactly the same text as before.
        budget[(row, 1)] = float(1000 * row)
        budget[(row, 2)] = float(400 * row)
        budget[(row, 3)] = float(600 * row)
    forecast: ValueGrid = {}
    for col, header in enumerate(("Hour", "SE1", "SE2", "SE3", "SE4", "Load", "Wind", "Solar")):
        forecast[(0, col)] = header
    for row in range(1, 2000):
        forecast[(row, 0)] = str(row)
        for col in range(1, 8):
            forecast[(row, col)] = f"{row}.{col}"
    sheets: dict[str, ValueGrid] = {"Budget": budget, "Forecast": forecast}
    if "hours" in low:
        # A time-indexed sheet whose timestamps are real datetimes — the shape
        # the value-typed read exists for, and the one a text grid destroys.
        # 25 rows: the fall-back day writes 02:00 twice.
        hours: ValueGrid = {(0, 0): "Hour", (0, 1): "MWh"}
        stamps = [_FALL_BACK_DAY.replace(hour=hour) for hour in range(24)]
        stamps.insert(3, _FALL_BACK_DAY.replace(hour=2))  # the repeated 02:00
        for row, stamp in enumerate(stamps, start=1):
            hours[(row, 0)] = stamp
            hours[(row, 1)] = float(row) * 1.5
        sheets["Hours"] = hours
    return sheets


def excel_sheets(name: str) -> dict[str, Grid]:
    """The worksheets a workbook of this name would have, as the text a window
    read renders — :func:`excel_value_sheets` stringified, blanks dropped."""
    return {
        sheet: {key: text for key, value in grid.items() if (text := _text_of(value))}
        for sheet, grid in excel_value_sheets(name).items()
    }


def coerce_written(value: str) -> object:
    """What a cell holds after a string is typed into it.

    Excel does this, not us: type ``9999`` into a cell and the cell holds the
    *number* 9999, which is why a write followed by a value-typed read must come
    back as a float rather than as the text that was sent. Anything that does
    not parse as a number stays text, exactly as it would in Excel.
    """
    try:
        return float(value)
    except ValueError:
        return value


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
        self._excel_docs: dict[int, dict[str, ValueGrid]] = {}
        #: Pids whose workbook has been written since it was opened. The stand-in
        #: for ``Workbook.Saved``: a write dirties the live instance and nothing
        #: here ever writes the file, so it stays dirty — which is exactly the
        #: state the reconciliation front gate has to refuse to read around.
        self._dirty: set[int] = set()

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

    def _excel_book(self, handle: HostHandle) -> dict[str, ValueGrid]:
        """The live worksheets for this pid — minted once, mutated thereafter."""
        name = self._name(handle)
        if handle.pid not in self._excel_docs:
            self._excel_docs[handle.pid] = excel_value_sheets(name)
        return self._excel_docs[handle.pid]

    def _excel_text(self, handle: HostHandle, sheet: str) -> Grid:
        """One sheet as the text a window read renders. Derived, never stored:
        the typed values are the workbook, text is how it looks."""
        grid = self._excel_book(handle)[sheet]
        return {key: text for key, value in grid.items() if (text := _text_of(value))}

    async def structure(self, handle: HostHandle, kind: HostAppKind) -> DocStructure:
        if kind == "word":
            return DocStructure(kind="word", paragraph_count=len(self._word_body(handle)))
        if kind == "excel":
            sheets = [
                SheetDim(name=sheet, rows=rows, cols=cols)
                for sheet in self._excel_book(handle)
                for rows, cols in (used_dims(self._excel_text(handle, sheet)),)
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
        return window_cells(sheet, self._excel_text(handle, sheet), a1_range, max_cells, max_chars)

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
            grid[(row, col)] = coerce_written(value)
        # The live instance now differs from the file — and nothing here writes
        # the file, so it stays that way. This is the fake's whole contribution
        # to the front gate: an edit really does make disk stale.
        self._dirty.add(handle.pid)
        return CellEdit(sheet=sheet, a1_cell=cell_ref(row, col), written_chars=len(value))

    # ---- the value-typed read (the reconciliation seam) ---------------------

    async def workbook_status(self, handle: HostHandle) -> LiveWorkbookStatus:
        name = self._name(handle).lower()
        return LiveWorkbookStatus(
            saved=not ("dirty" in name or handle.pid in self._dirty),
            calculation="calculating" if "calculating" in name else "done",
        )

    async def read_cells(
        self, handle: HostHandle, sheet: str | None, cells: Sequence[str]
    ) -> list[Any]:
        grid = self._values(handle, sheet)
        out: list[Any] = []
        for address in cells:
            try:
                row, col = parse_cell(address)
            except ValueError as error:
                raise RangeInvalidError(f"bad cell {address!r}: {error}") from error
            out.append(grid.get((row, col)))
        return out

    async def read_columns(
        self,
        handle: HostHandle,
        sheet: str | None,
        ts_column: str,
        value_column: str,
        start_row: int,
        max_rows: int,
    ) -> list[tuple[Any, Any]]:
        grid = self._values(handle, sheet)
        try:
            ts_col = column_index(ts_column)
            value_col = column_index(value_column)
        except ValueError as error:
            raise RangeInvalidError(f"bad column: {error}") from error
        rows, _ = used_dims({key: "x" for key in grid})
        last = min(rows, start_row - 1 + max_rows)
        # The window, verbatim — blank rows included, exactly as the real bridge
        # returns it. Where the data *ends* is the reconciliation seam's one
        # rule, not something each bridge decides for itself.
        return [
            (grid.get((row, ts_col)), grid.get((row, value_col)))
            for row in range(start_row - 1, last)
        ]

    def _values(self, handle: HostHandle, sheet: str | None) -> ValueGrid:
        """The typed grid for a sheet — or the refusal a bridge that cannot do a
        bulk read owes.

        ``nolive`` in the name is what makes the **blocked** row of the front
        gate's table reachable in CI: the status read above still answers (so
        the gate learns the workbook is dirty) and this one refuses (so there is
        no live number to judge). On real COM the two travel together; the split
        is honest all the same, because a bulk read really can fail — a modal, a
        per-call timeout — where two property reads succeeded.
        """
        name = self._name(handle)
        if "nolive" in name.lower():
            raise DocNotReadableError(
                "reading the live document is not available here (it needs the desktop shell)"
            )
        sheets = self._excel_book(handle)
        if sheet is None:
            # The active sheet: the first tab, which is what a freshly opened
            # workbook shows and what the disk reader's `None` means too.
            return next(iter(sheets.values()), {})
        if sheet not in sheets:
            raise no_sheet_error(sheet, list(sheets))
        return sheets[sheet]
