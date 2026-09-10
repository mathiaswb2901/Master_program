"""Probe: does the Word insert really land where it says, in a *real* document?

Run from the repo root, on a Windows machine with Word installed::

    uv run python scripts/dev/probe_word_insert.py

It is a **probe, not a gate** — the sibling of ``probe_live_com.py``, and it
exists for the same reason: the claims the insert path rests on belong to *Word
and pywin32*, not to us, so they are measured rather than remembered. The fake
bridge and the COM stand-ins in ``server/tests/test_real_document_bridge.py``
pin what the bridge *reaches for*; only a real Word can say what those members
actually do to a document.

Five claims, each asserted against a document this probe builds and re-reads:

1. ``Range.InsertParagraphAfter`` adds exactly **one** paragraph in the addressed
   position, and every other paragraph survives byte-for-byte.
2. ``Range.InsertParagraphBefore`` on paragraph 1 is the top-of-document insert,
   and the paragraph it split keeps its own style.
3. A new paragraph **inherits the anchor's style**, which is why the bridge sets
   one explicitly — an insert after a Heading 1 would otherwise be a Heading 1.
   ``Style.NextParagraphStyle`` is what Word's own Enter key would have given.
4. ``Range.Style`` takes a **built-in id** (``wdStyleHeading2 = -3``) and the
   style comes back under this machine's *local* name — which is the whole
   reason the bridge never sends "Heading 2" as a string.
5. **The table boundary holds.** The last paragraph of a table cell ends with
   ``\\x07`` rather than ``\\r``, the insert refuses that anchor, and the table
   still has its original rows, columns and cell text afterwards. A paragraph
   *inside* a cell is a legal anchor and stays inside the cell.

It drives ``services/office_host/office_com.insert_word_paragraph`` itself — the
production primitive, not a re-implementation — against a private Word launched
with ``DispatchEx``, and it cleans up: the document is closed without saving, the
application is quit, and the probe reports whether the pid it created is still
running. A leaked Office process is the one failure mode a probe like this must
never be quiet about.

structlog is deliberately not used: this is a developer script run by hand, and
its output is a report a person reads, not a log line a service emits.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# The document the probe builds, before anything is inserted. Paragraph 0 is a
# heading on purpose: it is the anchor whose style must *not* be inherited.
SEED = [
    ("Chapter 3", -2),  # wdStyleHeading1
    ("First body paragraph.", -1),  # wdStyleNormal
    ("Second body paragraph.", -1),
]


def _fail(message: str) -> int:
    print(f"FAIL  {message}")
    return 1


def _paragraph_texts(document: Any) -> list[str]:
    return [
        str(document.Paragraphs(index).Range.Text).rstrip("\r\n\x07\x0b\x0c")
        for index in range(1, int(document.Paragraphs.Count) + 1)
    ]


def _paragraph_marks(document: Any) -> list[str]:
    return [
        str(document.Paragraphs(index).Range.Text)[-1:]
        for index in range(1, int(document.Paragraphs.Count) + 1)
    ]


def _styles(document: Any) -> list[str]:
    return [
        str(document.Paragraphs(index).Range.Style.NameLocal)
        for index in range(1, int(document.Paragraphs.Count) + 1)
    ]


def _seed(word: Any) -> Any:
    """A three-paragraph chapter with a 2x2 table in the middle of it."""
    document = word.Documents.Add()
    for offset, (text, style) in enumerate(SEED):
        if offset:
            document.Content.InsertParagraphAfter()
        paragraph = document.Paragraphs(document.Paragraphs.Count)
        paragraph.Range.Text = text
        paragraph.Range.Style = style
    # A table after the body: Word keeps an ordinary paragraph after it, exactly
    # as the fake's `…table…` mint does.
    document.Content.InsertParagraphAfter()
    tail = document.Paragraphs(document.Paragraphs.Count)
    tail.Range.Text = "Table 1: hourly SE3 prices."
    tail.Range.Style = -1
    table = document.Tables.Add(document.Paragraphs(4).Range, 2, 2)
    for row in range(1, 3):
        for column in range(1, 3):
            table.Cell(row, column).Range.Text = f"r{row}c{column}"
    # One cell with *two* paragraphs. Measured: in a cell holding a single
    # paragraph, that paragraph is the cell's terminal one and ends with \x07 —
    # so a table of one-line cells has no legal insert anchor anywhere inside it,
    # and the only mid-cell anchor that exists is a cell like this one.
    table.Cell(1, 1).Range.Text = "r1c1\rsecond line"
    return document


def _instance(word: Any, document: Any) -> Any:
    from workbench_server.services.office_host.office_com import OfficeInstance

    return OfficeInstance(
        kind="word", pid=0, window_id=0, adopted=False, app=word, document=document
    )


def _still_running(pid: int) -> bool:
    import win32api  # type: ignore[import-not-found]
    import win32con  # type: ignore[import-not-found]
    import win32event  # type: ignore[import-not-found]

    try:
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
    except Exception:
        return False
    try:
        return bool(win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT)
    finally:
        win32api.CloseHandle(handle)


def _pid_of(document: Any) -> int:
    import win32process  # type: ignore[import-not-found]

    _, pid = win32process.GetWindowThreadProcessId(int(document.ActiveWindow.Hwnd))
    return int(pid)


def main() -> int:
    if sys.platform != "win32":
        return _fail("this probe needs Windows and a real Word")
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client  # type: ignore[import-not-found]

    from workbench_server.services.office_host import office_com
    from workbench_server.services.office_host.document_bridge import RangeInvalidError
    from workbench_server.services.office_host.document_window import resolve_insert_index

    problems: list[str] = []
    pythoncom.CoInitialize()
    with TemporaryDirectory(prefix="wb-word-probe-") as tmp:
        path = Path(tmp) / "chapter.docx"
        word = win32com.client.DispatchEx("Word.Application")
        pid = 0
        try:
            word.Visible = False
            word.DisplayAlerts = 0
            document = _seed(word)
            document.SaveAs2(str(path))
            pid = _pid_of(document)
            instance = _instance(word, document)
            print(f"launched a private Word (pid {pid})")
            before = _paragraph_texts(document)
            print(f"  seeded paragraphs             {before}")
            print(f"  seeded styles                 {_styles(document)}")
            marks = [hex(ord(mark)) for mark in _paragraph_marks(document)]
            print(f"  seeded marks                  {marks}")

            # ---- 1. an insert lands where addressed, and moves nothing else ----
            index = resolve_insert_index(1, int(document.Paragraphs.Count))
            applied = office_com.insert_word_paragraph(instance, index, "Drafted body.", None)
            after = _paragraph_texts(document)
            print(f"  insert after paragraph 1      index {index}, style {applied!r}")
            print(f"  paragraphs now                {after}")
            if len(after) != len(before) + 1:
                problems.append(f"one insert changed the count by {len(after) - len(before)}")
            if after[index] != "Drafted body.":
                problems.append(f"the new paragraph landed at {after.index('Drafted body.')}")
            if after[:index] != before[:index] or after[index + 1 :] != before[index:]:
                problems.append("an insert disturbed paragraphs it was not addressed to")

            # ---- 3. the style is Word's Enter answer, not the anchor's --------
            heading_follows = str(document.Paragraphs(1).Range.Style.NextParagraphStyle.NameLocal)
            print(f"  Heading 1 NextParagraphStyle  {heading_follows!r}")
            after_heading = office_com.insert_word_paragraph(
                instance, resolve_insert_index(0, int(document.Paragraphs.Count)), "Body.", None
            )
            print(f"  insert after the heading      style {after_heading!r}")
            if after_heading != heading_follows:
                problems.append(
                    f"an insert after a heading came out {after_heading!r}, not the "
                    f"{heading_follows!r} Word's own Enter key gives"
                )

            # ---- 4. a built-in id, and the local name back --------------------
            styled = office_com.insert_word_paragraph(
                instance,
                resolve_insert_index(0, int(document.Paragraphs.Count)),
                "3.1 Method",
                "heading2",
            )
            print(f"  style='heading2' came back as {styled!r} (local name)")
            if styled is None:
                problems.append("a styled insert could not read its own style back")

            # ---- 2. the top of the document -----------------------------------
            top_before = _paragraph_texts(document)
            top_style = office_com.insert_word_paragraph(instance, 0, "Front matter.", None)
            top_after = _paragraph_texts(document)
            print(f"  insert at the top             style {top_style!r}")
            if top_after[0] != "Front matter." or top_after[1:] != top_before:
                problems.append("the top insert did not push the document down by exactly one")

            # ---- 5. the table boundary ----------------------------------------
            table = document.Tables(1)
            cells_before = [
                str(table.Cell(row, column).Range.Text).rstrip("\r\x07")
                for row in range(1, 3)
                for column in range(1, 3)
            ]
            rows_before, cols_before = int(table.Rows.Count), int(table.Columns.Count)
            texts = _paragraph_texts(document)
            marks = _paragraph_marks(document)
            terminal = [i for i, mark in enumerate(marks) if mark == "\x07"]
            print(f"  end-of-cell paragraphs at     {terminal} of {len(texts)}")
            if not terminal:
                problems.append("no paragraph carried the \\x07 end-of-cell marker to refuse")
            for anchor in terminal:
                try:
                    office_com.insert_word_paragraph(
                        instance,
                        resolve_insert_index(anchor, int(document.Paragraphs.Count)),
                        "Would corrupt the table.",
                        None,
                    )
                except RangeInvalidError as refusal:
                    print(f"  anchor {anchor} refused             {refusal}")
                else:
                    problems.append(f"an insert on the end-of-cell paragraph {anchor} was allowed")
            cells_after = [
                str(table.Cell(row, column).Range.Text).rstrip("\r\x07")
                for row in range(1, 3)
                for column in range(1, 3)
            ]
            print(
                f"  table after the refusals      {int(table.Rows.Count)}x"
                f"{int(table.Columns.Count)} {cells_after}"
            )
            if (int(table.Rows.Count), int(table.Columns.Count)) != (rows_before, cols_before):
                problems.append("the table's shape changed across a refused insert")
            if cells_after != cells_before:
                problems.append("the table's cell text changed across a refused insert")

            # A paragraph *inside* a cell — one that is not the cell's last — is a
            # legal anchor, and the new paragraph stays inside the cell. Cell(1,1)
            # is the two-paragraph cell the seed built for exactly this.
            in_cell = int(table.Cell(1, 1).Range.Paragraphs(1).Range.Start)
            anchor = next(
                offset
                for offset in range(1, int(document.Paragraphs.Count) + 1)
                if int(document.Paragraphs(offset).Range.Start) == in_cell
            )
            cell_paragraphs = int(table.Cell(1, 1).Range.Paragraphs.Count)
            office_com.insert_word_paragraph(instance, anchor, "09:00", None)
            cell_text = str(table.Cell(1, 1).Range.Text).rstrip("\r\x07")
            print(f"  after a mid-cell insert       cell(1,1) = {cell_text!r}")
            if "09:00" not in cell_text:
                problems.append("a mid-cell insert did not land inside the cell")
            if int(table.Cell(1, 1).Range.Paragraphs.Count) != cell_paragraphs + 1:
                problems.append("a mid-cell insert did not add exactly one paragraph to the cell")
            if (int(table.Rows.Count), int(table.Columns.Count)) != (rows_before, cols_before):
                problems.append("a mid-cell insert changed the table's shape")

            # ---- the file round trip -------------------------------------------
            document.Save()
            final = _paragraph_texts(document)
            document.Close(0)
            document = word.Documents.Open(str(path))
            reopened = _paragraph_texts(document)
            print(f"  paragraphs after a reopen     {len(reopened)}")
            if reopened != final:
                problems.append("the saved document did not re-open with the paragraphs it had")
            if int(document.Tables.Count) != 1:
                problems.append(f"the reopened document has {document.Tables.Count} tables, not 1")
            document.Close(0)
        finally:
            try:
                word.Quit(0)
            except Exception as error:  # the probe still has to report the pid
                problems.append(f"Quit() raised: {error}")
            del word
            pythoncom.CoUninitialize()

        for _ in range(40):
            if not pid or not _still_running(pid):
                break
            time.sleep(0.1)
        leaked = bool(pid) and _still_running(pid)
        print(f"  leaked an Office process?     {'YES' if leaked else 'no'} (pid {pid})")
        if leaked:
            problems.append(f"pid {pid} is still running — kill it by hand")

    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        return 1
    print("OK    insert addressing, style intent and the table boundary all hold on real Word")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
