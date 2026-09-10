"""Editing the live docked document — the COM bridge write seam, fake-first.

The mirror of ``test_office_document_bridge.py``. Everything here runs with no
Microsoft Office, no Rust and no window: the fake document bridge shares the fake
host backend, so a write is applied to exactly the instance the lifecycle drove
to ``embedded``, mutates only the addressed target, and a subsequent read sees
the edit. Every write branch the real COM writer will have is reachable and
asserted — a Word paragraph replaced, an Excel cell set and cleared, an empty
document, an out-of-range paragraph, an unknown sheet, a malformed cell, a
document that was never docked, one that closed mid-write, a foreign window, and
the no-writer-available degrade — plus the AXI shapes the confirmation owes the
model (an explicit "emptied"/"cleared", the read-back next step, and an honest
refusal that names the target).

The service is driven directly *and* through ``handle_office_write``: the service
already satisfies ``OfficeDocumentWriter``, so passing it to the tool body is the
whole path a session takes, formatting and all.
"""

from pathlib import Path
from typing import cast

import pytest

from workbench_server.models.office_bridge import CellEdit, WordEdit
from workbench_server.models.office_host import PanelRect
from workbench_server.services.agent_tools import handle_office_write
from workbench_server.services.event_bus import EventBus
from workbench_server.services.office_host.document_bridge import (
    DocGoneError,
    DocNotHostedError,
    DocNotReadableError,
    RangeInvalidError,
)
from workbench_server.services.office_host.fake_backend import FakeHostBackend
from workbench_server.services.office_host.fake_document_bridge import FakeDocumentBridge
from workbench_server.services.office_host.service import OfficeHostService
from workbench_server.services.workspace import Workspace

RECT = PanelRect(x=10, y=20, width=640, height=480)


def _service(root: Path, *, with_bridge: bool = True) -> tuple[OfficeHostService, FakeHostBackend]:
    backend = FakeHostBackend()
    bridge = FakeDocumentBridge(backend) if with_bridge else None
    service = OfficeHostService(
        Workspace(root),
        EventBus(),
        backend,
        bridge=bridge,
        mode="on",
        fake=True,
        detector=lambda: False,
        poll_interval_s=60.0,
    )
    return service, backend


async def _docked(root: Path, name: str, with_bridge: bool = True) -> tuple[OfficeHostService, str]:
    (root / name).write_bytes(b"PK\x03\x04 fake office bytes")
    service, _ = _service(root, with_bridge=with_bridge)
    info = await service.open(name, RECT)
    assert info.state == "embedded", info
    return service, name


def _text(result: dict) -> str:  # type: ignore[type-arg]
    text: str = result["content"][0]["text"]
    return text


# ---- Word -------------------------------------------------------------------


class TestWord:
    async def test_replace_a_paragraph_and_read_it_back(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        edit = await service.write_document(name, content="Rewritten body.", paragraph=2)
        assert isinstance(edit, WordEdit)
        assert edit.paragraph == 2
        assert edit.written_chars == len("Rewritten body.")
        # The document's shape is unchanged: a replace never adds or drops one.
        assert edit.total_paragraphs == 5
        # And a subsequent read reflects the edit — the whole point of the seam.
        word = await service.read_document(name, max_chars=6_000, max_cells=600)
        assert word.text.split("\n\n")[2] == "Rewritten body."  # type: ignore[union-attr]

    async def test_only_the_addressed_paragraph_changes(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        before = await service.read_document(name, max_chars=6_000, max_cells=600)
        paras_before = before.text.split("\n\n")  # type: ignore[union-attr]
        await service.write_document(name, content="Only this one.", paragraph=0)
        after = await service.read_document(name, max_chars=6_000, max_cells=600)
        paras_after = after.text.split("\n\n")  # type: ignore[union-attr]
        assert paras_after[0] == "Only this one."
        # Every other paragraph is byte-for-byte what it was.
        assert paras_after[1:] == paras_before[1:]

    async def test_tool_confirms_and_names_the_read_back(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        text = _text(
            await handle_office_write(service, {"path": name, "paragraph": 1, "content": "New."})
        )
        # AXI shape 3: the confirmation ends with the obvious next step.
        assert "office_read" in text
        assert "start_paragraph=1" in text
        assert "New." in text

    async def test_zero_length_content_clears_the_paragraph(self, tmp_path: Path) -> None:
        # NB: the test name must avoid "empty"/"blank" — the fake keys its content
        # off the launched path, and pytest's tmp_path embeds the function name, so
        # a trigger word there would flip report.docx into the empty-document branch.
        service, name = await _docked(tmp_path, "report.docx")
        edit = await service.write_document(name, content="", paragraph=3)
        assert isinstance(edit, WordEdit)
        assert edit.written_chars == 0
        # AXI shape 2: the empty write is said out loud, not left as a silent no-op.
        text = _text(
            await handle_office_write(service, {"path": name, "paragraph": 3, "content": ""})
        )
        assert "emptied" in text.lower()

    async def test_write_to_an_empty_document_is_invalid(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "empty-notes.docx")
        with pytest.raises(RangeInvalidError):
            await service.write_document(name, content="text", paragraph=0)
        # The tool renders it as an explicit, actionable refusal — not a traceback.
        text = _text(
            await handle_office_write(service, {"path": name, "paragraph": 0, "content": "text"})
        )
        assert "empty" in text.lower()

    async def test_paragraph_past_the_end_is_range_invalid(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        with pytest.raises(RangeInvalidError):
            await service.write_document(name, content="text", paragraph=99)

    async def test_word_without_a_paragraph_is_refused(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        with pytest.raises(RangeInvalidError):
            await service.write_document(name, content="text")
        text = _text(await handle_office_write(service, {"path": name, "content": "text"}))
        assert "paragraph" in text.lower()


# ---- Word: the insert ------------------------------------------------------
#
# The write that changes the document's *shape*, and the one that makes
# "Workbench writes my report" a true sentence rather than "Workbench rewords
# it". Every assertion here is a round trip: write, re-read, and check that the
# paragraphs nobody addressed are byte-for-byte where they were.


async def _paragraphs(service: OfficeHostService, name: str) -> list[str]:
    """The whole live body, read back the way the agent reads it."""
    word = await service.read_document(name, max_chars=6_000, max_cells=600)
    return word.text.split("\n\n")  # type: ignore[union-attr]


class TestWordInsert:
    async def test_insert_lands_where_addressed_and_moves_nothing_else(
        self, tmp_path: Path
    ) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        before = await _paragraphs(service, name)
        edit = await service.write_document(
            name, content="Drafted body.", op="insert", after_paragraph=1
        )
        assert isinstance(edit, WordEdit)
        assert (edit.op, edit.paragraph, edit.written_chars) == ("insert", 2, 13)
        # The shape changed, and the result says so: five paragraphs became six.
        assert edit.total_paragraphs == len(before) + 1 == 6
        after = await _paragraphs(service, name)
        assert after[2] == "Drafted body."
        # Everything above the insert is untouched, everything below is the same
        # text one index further down. Nothing was rewritten, merged or lost.
        assert after[:2] == before[:2]
        assert after[3:] == before[2:]

    async def test_appending_with_no_anchor(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        before = await _paragraphs(service, name)
        edit = await service.write_document(name, content="Closing remark.", op="insert")
        assert isinstance(edit, WordEdit)
        assert edit.paragraph == len(before)  # one past the old last
        after = await _paragraphs(service, name)
        assert after[:-1] == before
        assert after[-1] == "Closing remark."
        # An append moved nothing, and the confirmation must not claim a shift
        # over paragraphs that do not exist below it.
        text = _text(
            await handle_office_write(
                service, {"path": name, "op": "insert", "content": "One more."}
            )
        )
        assert "nothing else moved" in text
        assert "shifted" not in text

    async def test_inserting_before_the_first_paragraph(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        before = await _paragraphs(service, name)
        edit = await service.write_document(
            name, content="Front matter.", op="insert", after_paragraph=-1
        )
        assert isinstance(edit, WordEdit)
        assert edit.paragraph == 0
        after = await _paragraphs(service, name)
        assert after[0] == "Front matter."
        assert after[1:] == before
        # It split the title, so it *is* the title's style — Word's own answer.
        assert edit.style == "Heading 1"

    async def test_inserting_into_a_document_with_nothing_in_it(self, tmp_path: Path) -> None:
        # Where drafting actually starts. A replace refuses here (there is no
        # paragraph to replace); an insert must not.
        service, name = await _docked(tmp_path, "empty-notes.docx")
        edit = await service.write_document(name, content="The first sentence.", op="insert")
        assert isinstance(edit, WordEdit)
        assert (edit.paragraph, edit.total_paragraphs) == (0, 1)
        assert await _paragraphs(service, name) == ["The first sentence."]

    async def test_a_heading_intent_becomes_a_heading_style(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        edit = await service.write_document(
            name, content="3.1 Method", op="insert", after_paragraph=1, style="heading2"
        )
        assert isinstance(edit, WordEdit)
        assert edit.style == "Heading 2"

    async def test_no_style_after_a_heading_gives_body_not_a_second_heading(
        self, tmp_path: Path
    ) -> None:
        # The fidelity trap: an insert inherits the anchor's formatting, so
        # without this the paragraph after a title would itself be a title.
        service, name = await _docked(tmp_path, "report.docx")
        edit = await service.write_document(
            name, content="Drafted body.", op="insert", after_paragraph=0
        )
        assert isinstance(edit, WordEdit)
        assert edit.style == "Normal"

    async def test_an_end_of_cell_anchor_is_refused_and_the_table_is_untouched(
        self, tmp_path: Path
    ) -> None:
        service, name = await _docked(tmp_path, "chapter-with-table.docx")
        before = await _paragraphs(service, name)
        with pytest.raises(RangeInvalidError, match="table cell"):
            # Paragraph 6 is the last paragraph of a cell: its range ends with
            # the end-of-cell marker, and inserting across it moves the boundary.
            await service.write_document(name, content="x", op="insert", after_paragraph=6)
        assert await _paragraphs(service, name) == before
        text = _text(
            await handle_office_write(
                service, {"path": name, "op": "insert", "after_paragraph": 6, "content": "x"}
            )
        )
        assert "table" in text.lower()

    async def test_a_mid_cell_anchor_is_allowed(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "chapter-with-table.docx")
        edit = await service.write_document(name, content="09:00", op="insert", after_paragraph=5)
        assert isinstance(edit, WordEdit)
        assert edit.paragraph == 6
        assert (await _paragraphs(service, name))[6] == "09:00"

    async def test_content_with_a_line_break_is_refused(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        before = await _paragraphs(service, name)
        with pytest.raises(RangeInvalidError, match="one paragraph"):
            await service.write_document(
                name, content="First line.\nSecond line.", op="insert", after_paragraph=1
            )
        assert await _paragraphs(service, name) == before

    async def test_an_anchor_past_the_end_is_refused(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        with pytest.raises(RangeInvalidError, match="past the last paragraph"):
            await service.write_document(name, content="x", op="insert", after_paragraph=99)

    async def test_two_inserts_in_a_row_address_from_the_answer(self, tmp_path: Path) -> None:
        """The whole point of the returned index: an agent drafting a section
        keeps going without re-reading. Each call anchors on what the last one
        said, and the section comes out in order."""
        service, name = await _docked(tmp_path, "report.docx")
        first = await service.write_document(
            name, content="3.1 Method", op="insert", after_paragraph=1, style="heading2"
        )
        assert isinstance(first, WordEdit)
        second = await service.write_document(
            name, content="We fit the model on...", op="insert", after_paragraph=first.paragraph
        )
        assert isinstance(second, WordEdit)
        third = await service.write_document(
            name, content="Then we validate it on...", op="insert", after_paragraph=second.paragraph
        )
        assert isinstance(third, WordEdit)
        assert (await _paragraphs(service, name))[2:5] == [
            "3.1 Method",
            "We fit the model on...",
            "Then we validate it on...",
        ]

    async def test_the_tool_reports_the_index_the_total_and_the_shift(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        text = _text(
            await handle_office_write(
                service,
                {
                    "path": name,
                    "op": "insert",
                    "after_paragraph": 1,
                    "content": "Drafted body.",
                    "style": "heading3",
                },
            )
        )
        assert "index 2" in text
        assert "now has 6 paragraphs" in text
        assert "every index from 2 on has shifted by 1" in text  # the stale addresses
        assert "Heading 3" in text
        assert "start_paragraph=2" in text  # how to confirm it
        assert "after_paragraph=2" in text  # how to continue the section

    async def test_a_zero_length_insert_is_said_out_loud(self, tmp_path: Path) -> None:
        # NB: keep "empty"/"blank" out of the *test name* — the fake mints its
        # content from the launched path and pytest's tmp_path carries the
        # function name, so the word here would flip report.docx into the
        # empty-document branch (the sibling replace tests say the same).
        service, name = await _docked(tmp_path, "report.docx")
        text = _text(
            await handle_office_write(
                service, {"path": name, "op": "insert", "after_paragraph": 1, "content": ""}
            )
        )
        assert "empty paragraph" in text.lower()


class TestWriteCompatibility:
    """The replace path is a shipped contract. The insert arriving beside it must
    not have moved a byte of it — same fields, same defaults, same sentence."""

    async def test_a_replace_still_reports_op_replace_and_no_style(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        edit = await service.write_document(name, content="Rewritten body.", paragraph=2)
        assert isinstance(edit, WordEdit)
        assert edit.op == "replace"
        assert edit.style is None
        assert edit.total_paragraphs == 5  # a replace never changes the shape

    async def test_the_replace_confirmation_is_word_for_word_what_it_was(
        self, tmp_path: Path
    ) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        text = _text(
            await handle_office_write(service, {"path": name, "paragraph": 1, "content": "New."})
        )
        assert text == (
            f'wrote 4 chars to paragraph 2 of {name}: "New.". The document still has '
            "5 paragraphs; read it back with office_read (start_paragraph=1) to confirm."
        )


class TestInsertArguments:
    """Every one of these names a position in the user's document. A lenient
    parse does not produce an error message — it produces text somewhere nobody
    asked for, reported as success."""

    async def test_an_unknown_op_is_refused_rather_than_defaulted_to_replace(
        self, tmp_path: Path
    ) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        before = await _paragraphs(service, name)
        result = await handle_office_write(
            service, {"path": name, "op": "append", "paragraph": 0, "content": "x"}
        )
        assert result["is_error"] is True
        assert "'replace' or 'insert'" in _text(result)
        # And nothing was written: defaulting to replace would have destroyed a
        # paragraph the model meant to add to.
        assert await _paragraphs(service, name) == before

    async def test_insert_with_paragraph_instead_of_after_paragraph_is_refused(
        self, tmp_path: Path
    ) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        result = await handle_office_write(
            service, {"path": name, "op": "insert", "paragraph": 2, "content": "x"}
        )
        assert result["is_error"] is True
        assert "after_paragraph" in _text(result)

    async def test_a_nonsense_anchor_is_refused_not_rounded_to_the_end(
        self, tmp_path: Path
    ) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        for anchor in (-5, "two", 1.5):
            result = await handle_office_write(
                service, {"path": name, "op": "insert", "after_paragraph": anchor, "content": "x"}
            )
            assert result["is_error"] is True, anchor
            assert "after_paragraph" in _text(result)

    async def test_an_unknown_style_names_the_ones_that_exist(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        result = await handle_office_write(
            service, {"path": name, "op": "insert", "content": "x", "style": "Overskrift 1"}
        )
        assert result["is_error"] is True
        assert "heading1" in _text(result)

    async def test_an_anchor_without_the_insert_op_names_the_op(self, tmp_path: Path) -> None:
        # The likely slip: it addressed a position and forgot op=insert. The
        # service's own "name a paragraph" would read as a contradiction.
        service, name = await _docked(tmp_path, "report.docx")
        result = await handle_office_write(
            service, {"path": name, "after_paragraph": 1, "content": "x"}
        )
        assert result["is_error"] is True
        assert "op='insert'" in _text(result)

    async def test_a_style_on_a_replace_is_refused(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        result = await handle_office_write(
            service, {"path": name, "paragraph": 1, "content": "x", "style": "heading1"}
        )
        assert result["is_error"] is True
        assert "insert" in _text(result)


# ---- Excel ------------------------------------------------------------------


class TestExcel:
    async def test_insert_is_refused_and_names_what_excel_does_take(self, tmp_path: Path) -> None:
        # Inserting a *row* is a different operation with different hazards
        # (formulas and named ranges that reference what moved). Quietly setting
        # a cell instead would be an edit nobody asked for.
        service, name = await _docked(tmp_path, "book.xlsx")
        with pytest.raises(RangeInvalidError, match="Word-only"):
            await service.write_document(name, content="x", op="insert", sheet="Budget")

    async def test_set_a_cell_and_read_it_back(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        edit = await service.write_document(name, content="9999", sheet="Budget", cell="B2")
        assert isinstance(edit, CellEdit)
        assert edit.sheet == "Budget"
        assert edit.a1_cell == "B2"
        assert edit.written_chars == 4
        window = await service.read_document(name, sheet="Budget", max_chars=6_000, max_cells=600)
        # B2 is row index 1, col index 1 in the returned grid.
        assert window.cells[1][1] == "9999"  # type: ignore[union-attr]

    async def test_only_the_addressed_cell_changes(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        before = await service.read_document(name, sheet="Budget", max_chars=6_000, max_cells=600)
        rows_before = [list(row) for row in before.cells]  # type: ignore[union-attr]
        await service.write_document(name, content="X", sheet="Budget", cell="C3")
        after = await service.read_document(name, sheet="Budget", max_chars=6_000, max_cells=600)
        rows_after = [list(row) for row in after.cells]  # type: ignore[union-attr]
        # C3 is (row 2, col 2). Exactly one cell differs; nothing else moved.
        diffs = [
            (r, c)
            for r in range(len(rows_after))
            for c in range(len(rows_after[r]))
            if rows_after[r][c] != rows_before[r][c]
        ]
        assert diffs == [(2, 2)]
        assert rows_after[2][2] == "X"

    async def test_zero_length_content_clears_the_cell(self, tmp_path: Path) -> None:
        # See the Word sibling: keep "empty"/"blank" out of the test name.
        service, name = await _docked(tmp_path, "book.xlsx")
        edit = await service.write_document(name, content="", sheet="Budget", cell="B2")
        assert isinstance(edit, CellEdit)
        assert edit.written_chars == 0
        window = await service.read_document(name, sheet="Budget", max_chars=6_000, max_cells=600)
        assert window.cells[1][1] == ""  # type: ignore[union-attr]
        text = _text(
            await handle_office_write(
                service, {"path": name, "sheet": "Budget", "cell": "B2", "content": ""}
            )
        )
        assert "cleared" in text.lower()

    async def test_tool_confirms_and_names_the_read_back(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        text = _text(
            await handle_office_write(
                service, {"path": name, "sheet": "Budget", "cell": "B2", "content": "42"}
            )
        )
        assert "office_read" in text
        assert "sheet=Budget" in text
        assert "range=B2" in text

    async def test_unknown_sheet_is_range_invalid(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        with pytest.raises(RangeInvalidError):
            await service.write_document(name, content="x", sheet="Nope", cell="A1")
        text = _text(
            await handle_office_write(
                service, {"path": name, "sheet": "Nope", "cell": "A1", "content": "x"}
            )
        )
        assert "Nope" in text

    async def test_a_malformed_cell_is_range_invalid(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        with pytest.raises(RangeInvalidError):
            await service.write_document(name, content="x", sheet="Budget", cell="not-a-cell")

    async def test_excel_without_a_sheet_and_cell_is_refused(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        with pytest.raises(RangeInvalidError):
            await service.write_document(name, content="x", sheet="Budget")
        text = _text(
            await handle_office_write(service, {"path": name, "sheet": "Budget", "content": "x"})
        )
        assert "cell" in text.lower()


# ---- refusals ---------------------------------------------------------------


class TestRefusals:
    async def test_a_document_that_is_not_docked(self, tmp_path: Path) -> None:
        service, _ = _service(tmp_path)
        with pytest.raises(DocNotHostedError) as caught:
            await service.write_document("missing.docx", content="x", paragraph=0)
        # The shared _live_document guard phrases the imperative verb per caller;
        # the write path must read "then write it", not the "then writ it" a naive
        # gerund[:-3] slice would produce.
        assert "then write it" in str(caught.value)
        text = _text(
            await handle_office_write(
                service, {"path": "missing.docx", "content": "x", "paragraph": 0}
            )
        )
        assert "not docked" in text.lower()
        assert "open it first" in text.lower()

    async def test_closed_mid_write_is_document_gone(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        host = next(iter(service._hosts.values()))  # white-box: reach the fake pid
        assert host.handle is not None
        backend = cast(FakeHostBackend, service._backend)
        backend.kill(host.handle.pid)
        with pytest.raises(DocGoneError):
            await service.write_document(name, content="x", paragraph=0)
        text = _text(
            await handle_office_write(service, {"path": name, "paragraph": 0, "content": "x"})
        )
        assert "closed" in text.lower()

    async def test_a_window_we_did_not_launch_is_refused(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        host = next(iter(service._hosts.values()))  # white-box: tamper the handle
        assert host.handle is not None
        host.handle = host.handle.__class__(
            pid=host.handle.pid + 1, window_id=host.handle.window_id
        )
        with pytest.raises(DocNotReadableError):
            await service.write_document(name, content="x", paragraph=0)
        text = _text(
            await handle_office_write(service, {"path": name, "paragraph": 0, "content": "x"})
        )
        assert text.strip()
        assert name in text

    async def test_no_bridge_reports_unavailable(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx", with_bridge=False)
        with pytest.raises(DocNotReadableError):
            await service.write_document(name, content="x", paragraph=0)
        text = _text(
            await handle_office_write(service, {"path": name, "paragraph": 0, "content": "x"})
        )
        assert text.strip()
        assert name in text


# ---- argument handling ------------------------------------------------------


class TestArguments:
    async def test_a_missing_path_is_a_tool_error(self, tmp_path: Path) -> None:
        service, _ = _service(tmp_path)
        result = await handle_office_write(service, {"content": "x", "paragraph": 0})
        assert result["is_error"] is True
        assert "path" in _text(result).lower()

    async def test_missing_content_is_a_tool_error(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        result = await handle_office_write(service, {"path": name, "paragraph": 0})
        assert result["is_error"] is True
        assert "content" in _text(result).lower()
