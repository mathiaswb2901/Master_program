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


class FakeUsedRange:
    def __init__(self, row: int, col: int, value: Any) -> None:
        self.Row = row
        self.Column = col
        self.Value = value


class FakeWorksheet:
    """A worksheet backed by a zero-based ``(row, col) -> value`` map, with a
    ``UsedRange`` anchored at the first used cell — so the bridge's A1-offset
    arithmetic is exercised, not bypassed."""

    def __init__(self, name: str, grid: dict[tuple[int, int], Any]) -> None:
        self.Name = name
        self._grid = dict(grid)
        self.Cells = FakeCells(self)

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

    @property
    def UsedRange(self) -> FakeUsedRange:
        if not self._grid:
            return FakeUsedRange(1, 1, None)
        min_row = min(row for row, _ in self._grid)
        min_col = min(col for _, col in self._grid)
        max_row = max(row for row, _ in self._grid)
        max_col = max(col for _, col in self._grid)
        value = tuple(
            tuple(self._grid.get((row, col)) for col in range(min_col, max_col + 1))
            for row in range(min_row, max_row + 1)
        )
        return FakeUsedRange(min_row + 1, min_col + 1, value)


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
