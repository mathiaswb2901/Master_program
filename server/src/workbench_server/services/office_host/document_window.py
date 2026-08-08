"""The windowing the fake and the real COM bridge share, so both produce the
*same shape*.

The read seam owes the model a truncated-but-self-describing window
(``models/office_bridge.py``), and the fidelity rule owes it identical semantics
whether the paragraphs came from a name-minted fake or from
``Document.Paragraphs(i).Range.Text`` over COM. Keeping the trimming in one place
is what makes "the fake and the real must produce the same shape" a fact rather
than a hope: :class:`~...fake_document_bridge.FakeDocumentBridge` and
:class:`~...real_document_bridge.ShellDocumentBridge` both read their content into
the plain structures below — a ``list[str]`` of paragraph text, a sparse
:data:`Grid` of cell text — and hand it to the same functions.

Nothing here touches COM or mints anything; it is pure and runs in CI.
"""

from workbench_server.models.office_bridge import CellWindow, WordText
from workbench_server.services.office_host.a1 import cell_ref, parse_cell, parse_range
from workbench_server.services.office_host.document_bridge import RangeInvalidError

#: One worksheet as a sparse ``(row, col) -> text`` map, zero-based and anchored
#: at A1 — the intermediate both bridges build before windowing.
Grid = dict[tuple[int, int], str]


def used_dims(grid: Grid) -> tuple[int, int]:
    """The used range of ``grid`` as ``(rows, cols)`` counted from A1.

    A blank grid is ``0, 0``, which is what lets a reader say "none" rather than
    stream empty cells.
    """
    if not grid:
        return 0, 0
    return max(row for row, _ in grid) + 1, max(col for _, col in grid) + 1


def cap_cell(value: str, limit: int) -> str:
    """One cell's text, truncated to ``limit`` chars with an ellipsis if cut.

    No single cell may dominate the window, so a lone long cell is trimmed here
    rather than left to overrun the byte budget the tool clamps at the far end.
    """
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)] + "…"


def no_sheet_error(sheet: str, names: list[str]) -> RangeInvalidError:
    """The identical refusal both bridges raise for an unknown sheet."""
    return RangeInvalidError(f"no sheet named {sheet!r}; sheets are {', '.join(names) or '(none)'}")


def parse_write_cell(cell: str) -> tuple[int, int]:
    """An A1 write target to zero-based ``(row, col)``, or the shared refusal."""
    try:
        return parse_cell(cell)
    except ValueError as error:
        raise RangeInvalidError(f"bad cell {cell!r}: {error}") from error


def check_paragraph(paragraph: int, total: int) -> None:
    """Guard a Word write's target the same way for the fake and the real bridge.

    An empty document has no paragraph to replace — the invalid-target branch the
    tool renders as an explicit "empty", not a silent no-op.
    """
    if total == 0:
        raise RangeInvalidError("the document is empty; there is no paragraph to replace")
    if paragraph < 0 or paragraph >= total:
        raise RangeInvalidError(f"paragraph {paragraph} is past the last paragraph ({total - 1})")


def window_word(paragraphs: list[str], start_paragraph: int, max_chars: int) -> WordText:
    """A window onto ``paragraphs`` from ``start_paragraph``, up to ``max_chars``.

    Whole paragraphs joined by blank lines, stopping before ``max_chars`` is
    exceeded; a lone paragraph longer than the whole window is truncated so a read
    is never empty when there is text to show. Raises :class:`RangeInvalidError`
    when ``start_paragraph`` is past the end of a non-empty document.
    """
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


def window_cells(
    sheet: str, grid: Grid, a1_range: str | None, max_cells: int, max_chars: int
) -> CellWindow:
    """A rectangular window onto one worksheet's ``grid``.

    ``a1_range`` selects the corner or rectangle; ``None`` starts at A1. The
    window is trimmed to ``max_cells`` cells *and* to ``max_chars`` of aggregate
    text, with each cell capped so one long cell cannot fill the window by itself.
    Raises :class:`RangeInvalidError` for a malformed range or one that starts
    past the used range.
    """
    total_rows, total_cols = used_dims(grid)
    if total_rows == 0:
        return CellWindow(
            sheet=sheet, a1_range="", rows=0, cols=0, total_rows=0, total_cols=0, cells=[]
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
    max_rows = max(1, max_cells // ncols)
    last_row = min(last_row, row1 + max_rows - 1)
    per_cell = max(1, max_chars // ncols)
    cells: list[list[str]] = []
    used = 0
    for row in range(row1, last_row + 1):
        row_cells = [
            cap_cell(grid.get((row, col), ""), per_cell) for col in range(col1, last_col + 1)
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
