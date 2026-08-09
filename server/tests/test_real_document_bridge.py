"""The real COM document bridge, without Microsoft Office.

The mirror of ``test_office_host_shell.py`` one seam over: what is under test is
never "did COM return the paragraph" — a real Word proves that only by being run,
which the owner does on a machine with Office (see the PR body) — but *which COM
members the bridge reaches for* and *what it decides when they answer or fail*.
Getting those wrong rewrites the user's document or hangs the tool, so they are
pinned here against a stand-in that answers the way Word and Excel do:
``Document.Paragraphs(i).Range.Text`` get and set, ``Worksheet.Cells(r, c).Value``
get and set, ``UsedRange`` for the sheet's shape.

The bridge runs its COM work on the real ``ShellHostBackend`` apartment thread
(``run_com``) against the instance that backend is holding (``instance_for``), so
these drive the whole path a session takes — the thread hop included — with the
live document replaced by objects, not the reader replaced by a mock.
"""

from typing import Any

import pytest

from workbench_server.models.office_bridge import CellEdit, CellWindow, DocStructure, WordEdit
from workbench_server.services.office_host import office_com
from workbench_server.services.office_host.a1 import column_letter, parse_range
from workbench_server.services.office_host.backend import HostHandle
from workbench_server.services.office_host.document_bridge import (
    DocGoneError,
    DocNotReadableError,
    RangeInvalidError,
)
from workbench_server.services.office_host.fake_backend import FakeHostBackend
from workbench_server.services.office_host.fake_document_bridge import FakeDocumentBridge
from workbench_server.services.office_host.office_com import OfficeInstance
from workbench_server.services.office_host.real_document_bridge import ShellDocumentBridge
from workbench_server.services.office_host.service import build_bridge
from workbench_server.services.office_host.shell_backend import ShellHostBackend, _Bound
from workbench_server.services.office_host.shell_channel import ShellChannel

PID = 4321
HANDLE = HostHandle(pid=PID, window_id=0xABCD)

# ---- COM stand-ins ----------------------------------------------------------
#
# Each object offers exactly the members the bridge uses and nothing else, so a
# reach for the wrong one is an AttributeError the test surfaces — the #83
# discipline, one seam over.


class FakeRange:
    """A Word ``Range``: its ``Text`` carries the trailing paragraph mark, the
    way Word's does, so the bridge is what strips it on read and re-adds it on
    write."""

    def __init__(self, text: str) -> None:
        self.Text = text


class FakeParagraph:
    def __init__(self, range_: FakeRange) -> None:
        self.Range = range_


class FakeParagraphs:
    def __init__(self, texts: list[str], *, dead_hresult: int | None = None) -> None:
        # Stored with the paragraph mark Word keeps; the same FakeRange instance
        # is handed back by Item, so a write mutates what a later read sees.
        self._ranges = [FakeRange(text + "\r") for text in texts]
        self._dead = dead_hresult

    @property
    def Count(self) -> int:
        if self._dead is not None:
            raise Exception(self._dead)  # a COM object that died under the call
        return len(self._ranges)

    def Item(self, index: int) -> FakeParagraph:  # 1-based, the COM convention
        return FakeParagraph(self._ranges[index - 1])


class FakeWordDoc:
    def __init__(self, texts: list[str], *, dead_hresult: int | None = None) -> None:
        self.Paragraphs = FakeParagraphs(texts, dead_hresult=dead_hresult)


class FakeCell:
    def __init__(self, sheet: "FakeWorksheet", row: int, col: int) -> None:
        self._sheet = sheet
        self._row = row
        self._col = col

    @property
    def Value(self) -> Any:
        return self._sheet.raw(self._row, self._col)

    @Value.setter
    def Value(self, value: Any) -> None:
        self._sheet.put(self._row, self._col, value)


class FakeCells:
    def __init__(self, sheet: "FakeWorksheet") -> None:
        self._sheet = sheet

    def Item(self, row: int, col: int) -> FakeCell:  # 1-based Cells(r, c)
        return FakeCell(self._sheet, row, col)


class FakeCellRange:
    """A COM ``Range`` reduced to the one member a read touches: ``Value``.

    Lazy on purpose — the sheet is only asked for the rectangle when the value is
    actually pulled, which is what makes ``cells_marshalled`` a count of the work
    a read really did rather than of the objects it made.
    """

    def __init__(self, sheet: "FakeWorksheet", box: tuple[int, int, int, int]) -> None:
        self._sheet = sheet
        self._box = box

    @property
    def Value(self) -> Any:
        return self._sheet.rectangle(*self._box)


class FakeUsedRange(FakeCellRange):
    """``UsedRange``: a rectangle that also knows where it starts.

    ``Address`` is spelled the way Excel spells it — ``$A$1:$C$3``, absolute and
    collapsed to a single cell when it is one — because that string is what the
    dimension read parses instead of paying six round trips for the corners.
    """

    def __init__(self, sheet: "FakeWorksheet", box: tuple[int, int, int, int]) -> None:
        super().__init__(sheet, box)
        row1, col1, row2, col2 = box
        self.Row = row1 + 1
        self.Column = col1 + 1
        start = f"${column_letter(col1)}${row1 + 1}"
        end = f"${column_letter(col2)}${row2 + 1}"
        self.Address = start if start == end else f"{start}:{end}"


class FakeWorksheet:
    """A worksheet backed by a zero-based ``(row, col) -> value`` map, with a
    ``UsedRange`` anchored at the first used cell — so the bridge's A1-offset
    arithmetic is exercised, not bypassed.

    ``claims`` is Excel's least convenient habit, made reproducible: a real
    ``UsedRange`` extends to any cell that was ever *formatted* or written and
    cleared, so it routinely reports rows and columns that hold nothing. It is
    the reason the dimension read cannot simply trust the used range's corners.

    ``cells_marshalled`` counts every cell handed across the boundary. It is the
    budget the perf test asserts, and it cannot flake: a whole-sheet read is
    rows x cols on any machine, and two edge strips are rows + cols on any
    machine.
    """

    def __init__(
        self,
        name: str,
        grid: dict[tuple[int, int], Any],
        claims: tuple[int, int] | None = None,
    ) -> None:
        self.Name = name
        self._grid = dict(grid)
        self._claims = claims
        self.Cells = FakeCells(self)
        self.cells_marshalled = 0

    def raw(self, row1: int, col1: int) -> Any:
        return self._grid.get((row1 - 1, col1 - 1))

    def put(self, row1: int, col1: int, value: Any) -> None:
        key = (row1 - 1, col1 - 1)
        if value is None:
            self._grid.pop(key, None)
        else:
            self._grid[key] = value

    def cell(self, row0: int, col0: int) -> Any:
        return self._grid.get((row0, col0))

    def rectangle(self, row1: int, col1: int, row2: int, col2: int) -> Any:
        self.cells_marshalled += (row2 - row1 + 1) * (col2 - col1 + 1)
        if row1 == row2 and col1 == col2:
            return self._grid.get((row1, col1))  # Excel hands back a scalar for one cell
        return tuple(
            tuple(self._grid.get((row, col)) for col in range(col1, col2 + 1))
            for row in range(row1, row2 + 1)
        )

    def Range(self, a1: str) -> FakeCellRange:
        row1, col1, row2, col2 = parse_range(a1)
        return FakeCellRange(self, (row1, col1, row2 or row1, col2 or col1))

    @property
    def UsedRange(self) -> FakeUsedRange:
        if not self._grid:
            box = (0, 0, 0, 0)  # a blank sheet's used range is A1, holding nothing
        else:
            rows = [row for row, _ in self._grid]
            cols = [col for _, col in self._grid]
            box = (min(rows), min(cols), max(rows), max(cols))
        if self._claims is not None:
            box = (box[0], box[1], max(box[2], self._claims[0]), max(box[3], self._claims[1]))
        return FakeUsedRange(self, box)


class FakeWorksheets:
    def __init__(self, sheets: list[FakeWorksheet]) -> None:
        self._sheets = sheets

    @property
    def Count(self) -> int:
        return len(self._sheets)

    def Item(self, index: int) -> FakeWorksheet:
        return self._sheets[index - 1]


class FakeWorkbook:
    def __init__(self, sheets: list[FakeWorksheet]) -> None:
        self.Worksheets = FakeWorksheets(sheets)


def _instance(kind: str, document: Any) -> OfficeInstance:
    return office_com.OfficeInstance(
        kind=kind,  # type: ignore[arg-type]
        pid=PID,
        window_id=HANDLE.window_id,
        adopted=False,
        app=object(),
        document=document,
    )


@pytest.fixture
async def com() -> Any:
    """A real ``ShellHostBackend`` whose apartment thread the bridge uses, with a
    factory to dock a stand-in document and get the bridge pointed at it."""
    backend = ShellHostBackend(ShellChannel())
    bridge = ShellDocumentBridge(backend)

    def dock(kind: str, document: Any) -> ShellDocumentBridge:
        backend._bound[PID] = _Bound(instance=_instance(kind, document), host_id="host-1")
        return bridge

    try:
        yield dock
    finally:
        await backend.aclose()


# ---- Word -------------------------------------------------------------------


class TestWord:
    async def test_structure_and_read_over_com(self, com: Any) -> None:
        bridge = com("word", FakeWordDoc(["Heading", "First body.", "Second body."]))
        structure = await bridge.structure(HANDLE, "word")
        assert structure == DocStructure(kind="word", paragraph_count=3)
        word = await bridge.read_word(HANDLE, 0, 6_000)
        # The paragraph marks Word carries on Range.Text are stripped by the bridge.
        assert word.text == "Heading\n\nFirst body.\n\nSecond body."
        assert word.total_paragraphs == 3

    async def test_write_replaces_only_that_paragraph_and_reads_back(self, com: Any) -> None:
        doc = FakeWordDoc(["Heading", "First body.", "Second body."])
        bridge = com("word", doc)
        edit = await bridge.write_word(HANDLE, 1, "Rewritten body.")
        assert isinstance(edit, WordEdit)
        assert (edit.paragraph, edit.written_chars, edit.total_paragraphs) == (1, 15, 3)
        # The COM member the write set — Paragraphs(2).Range.Text — carries the new
        # text and a re-supplied paragraph mark; the neighbours are byte-for-byte.
        assert doc.Paragraphs.Item(1).Range.Text == "Heading\r"
        assert doc.Paragraphs.Item(2).Range.Text == "Rewritten body.\r"
        assert doc.Paragraphs.Item(3).Range.Text == "Second body.\r"
        # And a subsequent read reflects the edit — the whole point of the seam.
        word = await bridge.read_word(HANDLE, 0, 6_000)
        assert word.text.split("\n\n") == ["Heading", "Rewritten body.", "Second body."]

    async def test_empty_content_empties_the_paragraph_without_removing_it(self, com: Any) -> None:
        doc = FakeWordDoc(["Heading", "Body."])
        bridge = com("word", doc)
        edit = await bridge.write_word(HANDLE, 1, "")
        assert edit.written_chars == 0
        assert edit.total_paragraphs == 2  # still two paragraphs
        assert doc.Paragraphs.Item(2).Range.Text == "\r"  # the mark, no content

    async def test_write_preserves_a_table_cell_end_of_cell_marker(self, com: Any) -> None:
        # Document.Paragraphs includes paragraphs inside table cells, and the last
        # paragraph in a cell ends with the end-of-cell marker chr(7), not \r.
        # Forcing \r there rewrites the cell structure; the write must re-supply
        # the marker the paragraph already carries.
        doc = FakeWordDoc(["Before cell.", "In cell.", "After cell."])
        doc.Paragraphs.Item(2).Range.Text = "In cell." + "\x07"  # a table-cell tail
        bridge = com("word", doc)
        edit = await bridge.write_word(HANDLE, 1, "Rewritten cell.")
        assert isinstance(edit, WordEdit)
        assert (edit.paragraph, edit.written_chars, edit.total_paragraphs) == (1, 15, 3)
        # chr(7) re-supplied, not \r — the cell structure is untouched.
        assert doc.Paragraphs.Item(2).Range.Text == "Rewritten cell.\x07"
        # Neighbours are byte-for-byte, count unchanged.
        assert doc.Paragraphs.Item(1).Range.Text == "Before cell.\r"
        assert doc.Paragraphs.Item(3).Range.Text == "After cell.\r"

    async def test_write_to_an_empty_document_is_range_invalid(self, com: Any) -> None:
        bridge = com("word", FakeWordDoc([]))
        with pytest.raises(RangeInvalidError):
            await bridge.write_word(HANDLE, 0, "text")

    async def test_paragraph_past_the_end_is_range_invalid(self, com: Any) -> None:
        bridge = com("word", FakeWordDoc(["Only one."]))
        with pytest.raises(RangeInvalidError):
            await bridge.write_word(HANDLE, 9, "text")


# ---- Excel ------------------------------------------------------------------


def _budget() -> FakeWorksheet:
    grid: dict[tuple[int, int], Any] = {}
    for col, header in enumerate(("Month", "Revenue", "Cost", "Margin")):
        grid[(0, col)] = header
    for row in range(1, 4):
        grid[(row, 0)] = f"M{row}"
        grid[(row, 1)] = 1000 * row  # a real number, as Excel hands it back
    return FakeWorksheet("Budget", grid)


class TestExcel:
    async def test_structure_lists_sheets_with_used_dims(self, com: Any) -> None:
        bridge = com("excel", FakeWorkbook([_budget(), FakeWorksheet("Blank", {})]))
        structure = await bridge.structure(HANDLE, "excel")
        assert structure.kind == "excel"
        assert structure.sheets is not None
        dims = {sheet.name: (sheet.rows, sheet.cols) for sheet in structure.sheets}
        assert dims == {"Budget": (4, 4), "Blank": (0, 0)}

    async def test_read_a_populated_grid_over_com(self, com: Any) -> None:
        bridge = com("excel", FakeWorkbook([_budget()]))
        window = await bridge.read_excel(HANDLE, "Budget", None, 600, 6_000)
        assert isinstance(window, CellWindow)
        assert window.cells[0] == ["Month", "Revenue", "Cost", "Margin"]
        # 1000.0 from COM renders as "1000", not "1000.0".
        assert window.cells[1][1] == "1000"

    async def test_a_used_range_below_a1_keeps_its_offset(self, com: Any) -> None:
        # Data at B2:C2 — UsedRange starts at row 2, col 2, and the bridge must
        # anchor it back to A1 so total dims count from the top-left of the sheet.
        sheet = FakeWorksheet("Off", {(1, 1): "x", (1, 2): "y"})
        bridge = com("excel", FakeWorkbook([sheet]))
        window = await bridge.read_excel(HANDLE, "Off", None, 600, 6_000)
        assert (window.total_rows, window.total_cols) == (2, 3)
        assert window.cells[1][1:] == ["x", "y"]  # row index 1, cols B and C

    async def test_write_sets_only_that_cell_and_reads_back(self, com: Any) -> None:
        sheet = _budget()
        bridge = com("excel", FakeWorkbook([sheet]))
        before = {key: sheet.cell(*key) for key in [(1, 1), (1, 2), (2, 1)]}
        edit = await bridge.write_excel(HANDLE, "Budget", "B2", "9999")
        assert isinstance(edit, CellEdit)
        assert (edit.sheet, edit.a1_cell, edit.written_chars) == ("Budget", "B2", 4)
        # Only B2 (row 1, col 1) moved on the worksheet; its neighbours are intact.
        assert sheet.cell(1, 1) == "9999"
        assert sheet.cell(1, 2) == before[(1, 2)]
        assert sheet.cell(2, 1) == before[(2, 1)]
        window = await bridge.read_excel(HANDLE, "Budget", None, 600, 6_000)
        assert window.cells[1][1] == "9999"

    async def test_empty_value_clears_the_cell(self, com: Any) -> None:
        sheet = _budget()
        bridge = com("excel", FakeWorkbook([sheet]))
        edit = await bridge.write_excel(HANDLE, "Budget", "B2", "")
        assert edit.written_chars == 0
        assert sheet.cell(1, 1) is None  # ClearContents-equivalent: the cell is gone
        window = await bridge.read_excel(HANDLE, "Budget", None, 600, 6_000)
        assert window.cells[1][1] == ""

    async def test_unknown_sheet_is_range_invalid(self, com: Any) -> None:
        bridge = com("excel", FakeWorkbook([_budget()]))
        with pytest.raises(RangeInvalidError, match="Budget"):
            await bridge.read_excel(HANDLE, "Nope", None, 600, 6_000)
        with pytest.raises(RangeInvalidError):
            await bridge.write_excel(HANDLE, "Nope", "A1", "x")

    async def test_a_malformed_cell_is_range_invalid(self, com: Any) -> None:
        bridge = com("excel", FakeWorkbook([_budget()]))
        with pytest.raises(RangeInvalidError):
            await bridge.write_excel(HANDLE, "Budget", "not-a-cell", "x")


# ---- asking for the shape must not pull the sheet ---------------------------
#
# `office_read` asks for the structure and *then* reads a sheet. The structure
# used to be `used_dims(excel_grid(worksheet))` for every sheet in the book — a
# whole-sheet pull over COM, and a text map built from all of it, to arrive at
# two integers. So a sheet-scoped read pulled the sheet the caller wanted twice
# and every other sheet once. Measured against a real Excel (Office 16.0, 3
# sheets of 5,000x20): 0.497 s for the structure alone, 0.024 s per sheet after.
#
# The budget below is the *work* — cells handed across the boundary — because it
# is the thing that cannot flake, and because it is the thing that was wrong.


class TestSheetDimensions:
    """`excel_used_dims` has to be exactly what the grid would have said.

    Every case here is a sheet shape that breaks one of the cheap answers. They
    were each checked against a real Excel as well (`ALL MATCH`, PR body); these
    pin the same shapes where CI can run them.
    """

    async def test_a_used_range_that_over_reports_still_gives_the_tight_dims(
        self, com: Any
    ) -> None:
        """Formatting a far cell extends Excel's used range but not the data.

        This is why the corners of `UsedRange` cannot simply be believed: against
        a real Excel, data in A1:B2 with a fill on J20 reports a used range of
        20x10 and a grid of 2x2.
        """
        sheet = FakeWorksheet("Padded", {(0, 0): 1, (1, 1): 2}, claims=(19, 9))
        bridge = com("excel", FakeWorkbook([sheet]))
        structure = await bridge.structure(HANDLE, "excel")
        assert structure.sheets is not None
        assert (structure.sheets[0].rows, structure.sheets[0].cols) == (2, 2)

    async def test_a_blank_sheet_is_none_not_one_by_one(self, com: Any) -> None:
        """A blank sheet's used range is A1 — one cell, holding nothing. Reported
        as ``0, 0``, which is what lets a reader say "none" instead of streaming
        an empty cell."""
        bridge = com("excel", FakeWorkbook([FakeWorksheet("Blank", {})]))
        structure = await bridge.structure(HANDLE, "excel")
        assert structure.sheets is not None
        assert (structure.sheets[0].rows, structure.sheets[0].cols) == (0, 0)

    async def test_a_last_row_of_zeroes_is_not_trimmed(self, com: Any) -> None:
        """`0`, `0.0` and `False` are values, not blanks — a load profile ends in
        a row of them, and a dimension read that treated falsy as empty would cut
        the last hour off every one."""
        grid = {(0, 0): 1.0, (0, 1): "x", (1, 0): 0.0, (1, 1): False}
        bridge = com("excel", FakeWorkbook([FakeWorksheet("Zeroes", grid)]))
        structure = await bridge.structure(HANDLE, "excel")
        assert structure.sheets is not None
        assert (structure.sheets[0].rows, structure.sheets[0].cols) == (2, 2)

    async def test_a_formula_that_returns_empty_string_is_blank(self, com: Any) -> None:
        """An `=IF(...,"","x")` whose result is `""` renders as an empty cell, so
        it does not extend the dimensions — the grid drops it, and this must too.
        (It is also the case that defeats `Range.Find`, which matches it.)"""
        grid: dict[tuple[int, int], Any] = {(0, 0): 1, (4, 1): ""}
        bridge = com("excel", FakeWorkbook([FakeWorksheet("EmptyStr", grid)]))
        structure = await bridge.structure(HANDLE, "excel")
        assert structure.sheets is not None
        assert (structure.sheets[0].rows, structure.sheets[0].cols) == (1, 1)

    async def test_the_shape_of_a_book_is_read_without_pulling_its_sheets(self, com: Any) -> None:
        """The budget: asking for the structure costs the *edges* of each sheet,
        not its contents.

        Three 200x8 sheets are 4,800 cells. The old structure call marshalled
        every one of them; the two edge strips of a sheet are 200 + 8, and the
        fast path needs nothing else.
        """
        sheets = [
            FakeWorksheet(
                f"S{n}",
                {(row, col): float(row + col) for row in range(200) for col in range(8)},
            )
            for n in range(3)
        ]
        bridge = com("excel", FakeWorkbook(sheets))
        structure = await bridge.structure(HANDLE, "excel")
        assert structure.sheets is not None
        assert [(s.rows, s.cols) for s in structure.sheets] == [(200, 8)] * 3
        marshalled = sum(sheet.cells_marshalled for sheet in sheets)
        assert marshalled <= 3 * (200 + 8), (
            f"the structure read pulled {marshalled} cells; the whole book is "
            f"{3 * 200 * 8} and its edges are {3 * (200 + 8)}"
        )

    async def test_a_sheet_scoped_read_pulls_that_sheet_once(self, com: Any) -> None:
        """The double read, from where `office_read` performs it: structure first
        (so an Excel request with no sheet can answer with the sheet list), then
        the sheet. Only the second of those may pull cells."""
        target = FakeWorksheet(
            "Prices", {(row, col): float(row) for row in range(200) for col in range(8)}
        )
        other = FakeWorksheet("Notes", {(0, 0): "hello"})
        bridge = com("excel", FakeWorkbook([target, other]))
        await bridge.structure(HANDLE, "excel")
        await bridge.read_excel(HANDLE, "Prices", None, 600, 6_000)
        # One whole-sheet pull for the window, plus the edge strips of both
        # sheets for the structure — never two whole-sheet pulls.
        whole = 200 * 8
        assert target.cells_marshalled < 2 * whole
        assert target.cells_marshalled <= whole + 200 + 8


# ---- degrading honestly -----------------------------------------------------


class TestRefusals:
    async def test_an_instance_the_backend_no_longer_holds_is_gone(self, com: Any) -> None:
        # com() docks a document; drop it so instance_for returns None, the way a
        # closed-and-reaped host does.
        bridge = com("word", FakeWordDoc(["Body."]))
        bridge._backend._bound.clear()
        with pytest.raises(DocGoneError):
            await bridge.read_word(HANDLE, 0, 6_000)

    async def test_a_dead_com_object_mid_read_is_document_gone(self, com: Any) -> None:
        # RPC_E_DISCONNECTED: the instance closed under the call in flight.
        bridge = com("word", FakeWordDoc(["Body."], dead_hresult=-2147417848))
        with pytest.raises(DocGoneError):
            await bridge.read_word(HANDLE, 0, 6_000)

    async def test_co_e_objnotconnected_mid_read_is_document_gone(self, com: Any) -> None:
        # 0x800401FD CO_E_OBJNOTCONNECTED = -2147220995: the proxy's server is
        # gone. is_object_gone must recognise it (the literal used to be wrong),
        # and the bridge must map it to document_gone, not the catch-all.
        assert office_com.CO_E_OBJNOTCONNECTED == -2147220995
        assert office_com.is_object_gone(Exception(-2147220995)) is True
        bridge = com("word", FakeWordDoc(["Body."], dead_hresult=-2147220995))
        with pytest.raises(DocGoneError):
            await bridge.read_word(HANDLE, 0, 6_000)

    async def test_any_other_com_failure_is_not_readable_not_a_crash(self, com: Any) -> None:
        # A non-"dead" COM error must reach the tool as an honest typed refusal,
        # never an unhandled COM exception out of the bridge.
        bridge = com("word", FakeWordDoc(["Body."], dead_hresult=-2147418113))
        with pytest.raises(DocNotReadableError):
            await bridge.read_word(HANDLE, 0, 6_000)


# ---- the seam is chosen for a real host, deferred for the fake ---------------


class TestBuildBridge:
    def test_a_real_shell_backend_gets_the_real_bridge(self) -> None:
        backend = ShellHostBackend(ShellChannel())
        assert isinstance(build_bridge("auto", False, backend), ShellDocumentBridge)

    def test_the_fake_backend_keeps_the_fake_bridge(self) -> None:
        assert isinstance(build_bridge("auto", True, FakeHostBackend()), FakeDocumentBridge)

    def test_off_and_no_backend_read_nothing(self) -> None:
        assert build_bridge("off", False, ShellHostBackend(ShellChannel())) is None
        assert build_bridge("auto", False, None) is None
