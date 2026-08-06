"""Reading the live docked document — the COM bridge seam, fake-first.

Everything here runs with no Microsoft Office, no Rust and no window: the fake
document bridge shares the fake host backend, so a read is answered for exactly
the instance the lifecycle drove to ``embedded``. Every read branch the real COM
reader will have is reachable and asserted — multi-paragraph and empty Word, a
populated grid, a window smaller than the used range, an unknown sheet, a blank
sheet, a document that was never docked, one that closed mid-read, and a window
that is not the instance we launched — plus the three AXI shapes the result owes
the model (stated truncation, an explicit "none", and the next step).

The service is driven directly *and* through ``handle_office_read``: the service
already satisfies ``OfficeDocumentReader``, so passing it to the tool body is the
whole path a session takes, formatting and all.
"""

from pathlib import Path
from typing import cast

import pytest

from workbench_server.models.office_bridge import CellWindow, DocStructure, WordText
from workbench_server.models.office_host import PanelRect
from workbench_server.services.agent_tools import handle_office_read
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
    async def test_multi_paragraph_read_and_structure(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        structure = await service.document_structure(name)
        assert structure == DocStructure(kind="word", paragraph_count=5)
        word = await service.read_document(name, max_chars=6_000, max_cells=600)
        assert isinstance(word, WordText)
        assert word.total_paragraphs == 5
        assert word.start_paragraph == 0
        assert "Report" in word.text
        text = _text(await handle_office_read(service, {"path": name}))
        assert "end of document" in text
        assert "paragraphs 1-5 of 5" in text

    async def test_empty_document_says_none(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "empty-notes.docx")
        word = await service.read_document(name, max_chars=6_000, max_cells=600)
        assert isinstance(word, WordText)
        assert word.total_paragraphs == 0
        assert word.text == ""
        text = _text(await handle_office_read(service, {"path": name}))
        assert "empty" in text.lower()

    async def test_max_chars_truncates_with_a_footer(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        # 60 chars fits the heading but not the first body paragraph, so the read
        # stops at a paragraph boundary and says how to continue (AXI shape 1).
        text = _text(await handle_office_read(service, {"path": name, "max_chars": 60}))
        assert "paragraphs 1-1 of 5" in text
        assert "start_paragraph=1" in text

    async def test_start_paragraph_past_the_end_is_range_invalid(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        with pytest.raises(RangeInvalidError):
            await service.read_document(name, max_chars=6_000, max_cells=600, start_paragraph=99)


# ---- Excel ------------------------------------------------------------------


class TestExcel:
    async def test_structure_lists_the_sheets(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        structure = await service.document_structure(name)
        assert structure.kind == "excel"
        assert structure.sheets is not None
        dims = {sheet.name: (sheet.rows, sheet.cols) for sheet in structure.sheets}
        assert dims == {"Budget": (6, 4), "Forecast": (2000, 8)}
        # Omitting the sheet lists them and says what to do next (AXI shape 3).
        text = _text(await handle_office_read(service, {"path": name}))
        assert "Budget" in text and "Forecast" in text
        assert "Pass sheet=" in text

    async def test_populated_grid_fits_in_one_window(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        window = await service.read_document(name, sheet="Budget", max_chars=6_000, max_cells=600)
        assert isinstance(window, CellWindow)
        assert (window.total_rows, window.total_cols) == (6, 4)
        assert window.rows == 6 and window.cols == 4
        assert window.cells[0] == ["Month", "Revenue", "Cost", "Margin"]
        text = _text(await handle_office_read(service, {"path": name, "sheet": "Budget"}))
        assert "Revenue" in text
        assert "whole sheet" in text

    async def test_window_smaller_than_used_range_states_the_rest(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        window = await service.read_document(name, sheet="Forecast", max_chars=6_000, max_cells=600)
        assert isinstance(window, CellWindow)
        assert window.total_rows == 2000
        assert window.rows == 75  # 600 cells / 8 columns
        text = _text(await handle_office_read(service, {"path": name, "sheet": "Forecast"}))
        assert "rows 1-75 of 2000" in text
        assert "range=A76:" in text  # the escape hatch names the next window

    async def test_a_named_range_reads_that_corner(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        window = await service.read_document(
            name, sheet="Forecast", a1_range="B2:D3", max_chars=6_000, max_cells=600
        )
        assert isinstance(window, CellWindow)
        assert window.a1_range == "B2:D3"
        assert window.rows == 2 and window.cols == 3

    async def test_unknown_sheet_is_range_invalid(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        with pytest.raises(RangeInvalidError):
            await service.read_document(name, sheet="Nope", max_chars=6_000, max_cells=600)
        text = _text(await handle_office_read(service, {"path": name, "sheet": "Nope"}))
        assert "Nope" in text

    async def test_blank_sheet_says_none(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "empty-book.xlsx")
        window = await service.read_document(name, sheet="Sheet1", max_chars=6_000, max_cells=600)
        assert isinstance(window, CellWindow)
        assert window.total_rows == 0
        assert window.cells == []
        text = _text(await handle_office_read(service, {"path": name, "sheet": "Sheet1"}))
        assert "empty" in text.lower()

    async def test_a_bad_a1_range_is_range_invalid(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "book.xlsx")
        with pytest.raises(RangeInvalidError):
            await service.read_document(
                name, sheet="Forecast", a1_range="not-a-range", max_chars=6_000, max_cells=600
            )

    async def test_a_long_cell_is_bounded_by_text_not_just_cell_count(self, tmp_path: Path) -> None:
        # The Notes sheet is six rows of one 35k-char non-ASCII cell each: well
        # under the 600-cell cap, so a count-only bound would return the lot and
        # blow the tool's byte budget. The read must bound aggregate text — trim
        # rows and cap each cell — so the rendered result stays within budget.
        from workbench_server.services.agent_tools import OFFICE_READ

        service, name = await _docked(tmp_path, "notes.xlsx")
        window = await service.read_document(name, sheet="Notes", max_chars=6_000, max_cells=600)
        assert isinstance(window, CellWindow)
        assert window.total_rows == 6  # the whole used range is still reported
        # No single cell dominates, and the aggregate text is bounded near max_chars.
        assert all(len(cell) <= 6_000 for row in window.cells for cell in row)
        assert sum(len(cell) for row in window.cells for cell in row) <= 6_000 + 6_000
        text = _text(await handle_office_read(service, {"path": name, "sheet": "Notes"}))
        assert len(text.encode()) <= OFFICE_READ.max_result_bytes


# ---- refusals ---------------------------------------------------------------


class TestRefusals:
    async def test_a_document_that_is_not_docked(self, tmp_path: Path) -> None:
        service, _ = _service(tmp_path)
        with pytest.raises(DocNotHostedError):
            await service.read_document("missing.docx", max_chars=6_000, max_cells=600)
        text = _text(await handle_office_read(service, {"path": "missing.docx"}))
        assert "not docked" in text.lower()
        assert "open it first" in text.lower()

    async def test_closed_mid_read_is_document_gone(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        host = next(iter(service._hosts.values()))  # white-box: reach the fake pid
        assert host.handle is not None
        backend = cast(FakeHostBackend, service._backend)
        backend.kill(host.handle.pid)
        with pytest.raises(DocGoneError):
            await service.read_document(name, max_chars=6_000, max_cells=600)
        text = _text(await handle_office_read(service, {"path": name}))
        assert "closed" in text.lower()

    async def test_a_window_we_did_not_launch_is_refused(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx")
        host = next(iter(service._hosts.values()))  # white-box: tamper the handle
        assert host.handle is not None
        # Swap in a handle for a different pid: the ownership guard must refuse it
        # exactly as it does for a window op, never read a document we did not open.
        host.handle = host.handle.__class__(
            pid=host.handle.pid + 1, window_id=host.handle.window_id
        )
        with pytest.raises(DocNotReadableError):
            await service.read_document(name, max_chars=6_000, max_cells=600)
        # And the agent gets a clean AXI refusal, not a blank or a traceback: the
        # generic catch-all serves this branch, so pin what it actually renders.
        text = _text(await handle_office_read(service, {"path": name}))
        assert text.strip()
        assert name in text

    async def test_no_bridge_reports_unavailable(self, tmp_path: Path) -> None:
        service, name = await _docked(tmp_path, "report.docx", with_bridge=False)
        with pytest.raises(DocNotReadableError):
            await service.read_document(name, max_chars=6_000, max_cells=600)
        with pytest.raises(DocNotReadableError):
            await service.document_structure(name)
        # The no-reader-available case must also reach the agent as an actionable
        # sentence naming the document, not an empty result it cannot interpret.
        text = _text(await handle_office_read(service, {"path": name}))
        assert text.strip()
        assert name in text
