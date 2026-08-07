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


# ---- Excel ------------------------------------------------------------------


class TestExcel:
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
