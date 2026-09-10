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


#: What Word resolves a ``WdBuiltinStyle`` id to. The stand-in knows the *ids*
#: and not the names on purpose: assigning ``"Heading 1"`` here is an
#: AssertionError, which is how these tests pin that the bridge never sends a
#: style name — on this machine's Norwegian Word that string does not exist.
BUILTIN_STYLE_IDS = {-1: "Normal", -2: "Heading 1", -3: "Heading 2", -4: "Heading 3"}

#: ``WdInformation.wdWithInTable``, the only Information member the bridge asks for.
WD_WITH_IN_TABLE = 12


class FakeStyle:
    """A Word ``Style``: what it is called, and what follows it.

    ``NextParagraphStyle`` is the property Word's Enter key consults, and the one
    an insert with no named style has to honour — a heading is followed by body
    text, body text by itself.
    """

    def __init__(self, name: str, follows: "FakeStyle | None" = None) -> None:
        self.NameLocal = name
        self._follows = follows

    @property
    def NextParagraphStyle(self) -> "FakeStyle":
        return self._follows or self


NORMAL = FakeStyle("Normal")
HEADING_1 = FakeStyle("Heading 1", follows=NORMAL)


class FakeRange:
    """A Word ``Range``: its ``Text`` carries the trailing paragraph mark, the
    way Word's does, so the bridge is what strips it on read and re-adds it on
    write. It also carries the paragraph's style and whether it sits in a table,
    because an insert asks both before it touches anything."""

    def __init__(
        self,
        owner: "FakeParagraphs",
        text: str,
        style: FakeStyle = NORMAL,
        in_table: bool = False,
    ) -> None:
        self._owner = owner
        self.Text = text
        self._style = style
        self._in_table = in_table

    @property
    def Style(self) -> FakeStyle:
        return self._style

    @Style.setter
    def Style(self, value: Any) -> None:
        if isinstance(value, FakeStyle):
            self._style = value
            return
        if isinstance(value, int):
            self._style = FakeStyle(BUILTIN_STYLE_IDS[value], follows=NORMAL)
            return
        raise AssertionError(f"Word takes a built-in style id or a Style, not {value!r}")

    def Information(self, which: int) -> bool:
        assert which == WD_WITH_IN_TABLE, f"unexpected Information member {which}"
        return self._in_table

    def _at(self) -> int:
        return next(i for i, other in enumerate(self._owner._ranges) if other is self)

    def _new_neighbour(self) -> "FakeRange":
        # Word's InsertParagraph* splits this paragraph, so the new one inherits
        # its formatting — the very reason the bridge re-applies a style rather
        # than trusting what it got.
        return FakeRange(self._owner, "\r", style=self._style, in_table=self._in_table)

    def InsertParagraphAfter(self) -> None:
        self._owner._ranges.insert(self._at() + 1, self._new_neighbour())

    def InsertParagraphBefore(self) -> None:
        self._owner._ranges.insert(self._at(), self._new_neighbour())


class FakeParagraph:
    def __init__(self, range_: FakeRange) -> None:
        self.Range = range_


class FakeParagraphs:
    def __init__(
        self,
        texts: list[str],
        *,
        dead_hresult: int | None = None,
        styles: dict[int, FakeStyle] | None = None,
        in_table: set[int] | None = None,
    ) -> None:
        # Stored with the paragraph mark Word keeps; the same FakeRange instance
        # is handed back by Item, so a write mutates what a later read sees.
        self._ranges: list[FakeRange] = []
        for index, text in enumerate(texts):
            self._ranges.append(
                FakeRange(
                    self,
                    text + "\r",
                    style=(styles or {}).get(index, NORMAL),
                    in_table=index in (in_table or set()),
                )
            )
        self._dead = dead_hresult

    @property
    def Count(self) -> int:
        if self._dead is not None:
            raise Exception(self._dead)  # a COM object that died under the call
        return len(self._ranges)

    def Item(self, index: int) -> FakeParagraph:  # 1-based, the COM convention
        return FakeParagraph(self._ranges[index - 1])


class FakeContent:
    """``Document.Content`` — reached only by the insert into a document with no
    paragraphs at all, which Word does not produce but the seam allows."""

    def __init__(self, paragraphs: FakeParagraphs) -> None:
        self._paragraphs = paragraphs

    def InsertAfter(self, text: str) -> None:
        self._paragraphs._ranges.append(FakeRange(self._paragraphs, text + "\r"))


class FakeWordDoc:
    def __init__(
        self,
        texts: list[str],
        *,
        dead_hresult: int | None = None,
        styles: dict[int, FakeStyle] | None = None,
        in_table: set[int] | None = None,
    ) -> None:
        self.Paragraphs = FakeParagraphs(
            texts, dead_hresult=dead_hresult, styles=styles, in_table=in_table
        )
        self.Content = FakeContent(self.Paragraphs)

    def texts(self) -> list[str]:
        """Every paragraph's raw range text, marks and all — what a fidelity
        assertion compares before and after a write."""
        return [range_.Text for range_ in self.Paragraphs._ranges]

    def styles(self) -> list[str]:
        return [range_.Style.NameLocal for range_ in self.Paragraphs._ranges]


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


# ---- Word: the insert (the write that changes the document's shape) ---------


def _chapter() -> FakeWordDoc:
    """A chapter whose first paragraph is a heading — the anchor an insert must
    not inherit from."""
    return FakeWordDoc(["Chapter 3", "First body.", "Second body."], styles={0: HEADING_1})


def _with_table() -> FakeWordDoc:
    """A document with a two-cell table in the middle: paragraph 1 is inside a
    cell and ends with an ordinary mark, paragraph 2 is the cell's *last*
    paragraph and ends with the end-of-cell marker, paragraph 3 is the ordinary
    body Word always keeps after a table."""
    doc = FakeWordDoc(["Before.", "Hour", "SE3 price", "After."], in_table={1, 2})
    doc.Paragraphs.Item(3).Range.Text = "SE3 price\x07"
    return doc


class TestWordInsert:
    async def test_insert_after_a_paragraph_moves_nothing_else(self, com: Any) -> None:
        doc = _chapter()
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, 0, "Drafted body.", None)
        assert isinstance(edit, WordEdit)
        assert (edit.op, edit.paragraph, edit.written_chars) == ("insert", 1, 13)
        assert edit.total_paragraphs == 4
        # The new paragraph is exactly where it was addressed, carrying an
        # ordinary paragraph mark, and every existing paragraph is byte-for-byte.
        assert doc.texts() == ["Chapter 3\r", "Drafted body.\r", "First body.\r", "Second body.\r"]
        word = await bridge.read_word(HANDLE, 0, 6_000)
        assert word.text.split("\n\n")[1] == "Drafted body."

    async def test_no_anchor_appends_at_the_end(self, com: Any) -> None:
        doc = _chapter()
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, None, "Closing remark.", None)
        assert (edit.paragraph, edit.total_paragraphs) == (3, 4)
        assert doc.texts()[-1] == "Closing remark.\r"
        assert doc.texts()[:3] == ["Chapter 3\r", "First body.\r", "Second body.\r"]

    async def test_minus_one_inserts_before_the_first_paragraph(self, com: Any) -> None:
        doc = _chapter()
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, -1, "Front matter.", None)
        assert (edit.paragraph, edit.total_paragraphs) == (0, 4)
        assert doc.texts()[0] == "Front matter.\r"
        # Splitting the first paragraph keeps its style, which is what typing at
        # the very start of a document and pressing Enter gives you.
        assert doc.styles()[0] == "Heading 1"
        assert edit.style == "Heading 1"

    async def test_a_named_style_is_applied_by_builtin_id(self, com: Any) -> None:
        doc = _chapter()
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, 0, "3.1 Method", "heading2")
        # The stand-in resolves ids, never names — so this also pins that the
        # bridge sent wdStyleHeading2 and not the English string.
        assert doc.styles() == ["Heading 1", "Heading 2", "Normal", "Normal"]
        assert edit.style == "Heading 2"

    async def test_no_style_after_a_heading_is_body_not_another_heading(self, com: Any) -> None:
        # The fidelity trap: Word's InsertParagraphAfter copies the anchor's
        # formatting, so an insert after a Heading 1 is a Heading 1 unless the
        # bridge applies what Enter would have given — NextParagraphStyle.
        doc = _chapter()
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, 0, "Drafted body.", None)
        assert doc.styles()[1] == "Normal"
        assert edit.style == "Normal"

    async def test_an_end_of_cell_anchor_is_refused_and_nothing_changes(self, com: Any) -> None:
        doc = _with_table()
        before = doc.texts()
        bridge = com("word", doc)
        with pytest.raises(RangeInvalidError, match="table cell"):
            await bridge.insert_word(HANDLE, 2, "Would corrupt the table.", None)
        # The refusal is *before* the mutation: the table is exactly as it was.
        assert doc.texts() == before

    async def test_a_mid_cell_anchor_is_allowed_and_keeps_the_cell_marker(self, com: Any) -> None:
        doc = _with_table()
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, 1, "09:00", None)
        assert edit.paragraph == 2
        # The new paragraph is inside the cell with an ordinary mark, and the
        # cell's terminal marker is still the cell's.
        assert doc.texts() == ["Before.\r", "Hour\r", "09:00\r", "SE3 price\x07", "After.\r"]

    async def test_a_top_insert_into_a_table_is_refused(self, com: Any) -> None:
        doc = FakeWordDoc(["In a cell.", "After."], in_table={0})
        before = doc.texts()
        bridge = com("word", doc)
        with pytest.raises(RangeInvalidError, match="table cell"):
            await bridge.insert_word(HANDLE, -1, "Front matter.", None)
        assert doc.texts() == before

    async def test_a_line_break_in_content_is_refused_before_anything_moves(self, com: Any) -> None:
        doc = _chapter()
        before = doc.texts()
        bridge = com("word", doc)
        with pytest.raises(RangeInvalidError, match="one paragraph"):
            await bridge.insert_word(HANDLE, 0, "First line.\nSecond line.", None)
        assert doc.texts() == before

    async def test_an_anchor_past_the_end_is_refused(self, com: Any) -> None:
        doc = _chapter()
        bridge = com("word", doc)
        with pytest.raises(RangeInvalidError, match="past the last paragraph"):
            await bridge.insert_word(HANDLE, 9, "Nowhere.", None)
        assert len(doc.texts()) == 3

    async def test_inserting_into_a_document_with_no_paragraphs(self, com: Any) -> None:
        # Word does not produce one (a blank document still has one empty
        # paragraph), but the seam allows it and drafting starts here.
        doc = FakeWordDoc([])
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, None, "The first sentence.", "heading1")
        assert (edit.paragraph, edit.total_paragraphs) == (0, 1)
        assert doc.texts() == ["The first sentence.\r"]
        assert doc.styles() == ["Heading 1"]

    async def test_the_new_total_is_re_read_from_word_not_computed(self, com: Any) -> None:
        # If Word ever did something other than "one insert, one paragraph", the
        # honest number is the one Word reports. A bridge that returned
        # `total + 1` would be plausible and wrong, and the caller addresses from
        # it. This stand-in inserts two paragraphs to prove the count is asked.
        doc = _chapter()

        class TwoAtOnce(FakeRange):
            def InsertParagraphAfter(self) -> None:
                super().InsertParagraphAfter()
                super().InsertParagraphAfter()

        doc.Paragraphs._ranges[0].__class__ = TwoAtOnce
        bridge = com("word", doc)
        edit = await bridge.insert_word(HANDLE, 0, "Drafted body.", None)
        assert len(doc.texts()) == 5
        assert edit.total_paragraphs == 5


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
