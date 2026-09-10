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

from dataclasses import dataclass, field

from workbench_server.models.office_bridge import (
    CellWindow,
    SlideDim,
    SlideText,
    WordParagraphStyle,
    WordText,
)
from workbench_server.services.office_host.a1 import cell_ref, parse_cell, parse_range
from workbench_server.services.office_host.document_bridge import RangeInvalidError

#: One worksheet as a sparse ``(row, col) -> text`` map, zero-based and anchored
#: at A1 — the intermediate both bridges build before windowing.
Grid = dict[tuple[int, int], str]


@dataclass
class SlideContent:
    """One slide's readable content — the PowerPoint intermediate.

    The counterpart of :data:`Grid` and of Word's ``list[str]``: what both
    bridges build before windowing, so the fake and the COM reader produce the
    identical shape. ``texts`` holds the text of each shape that had any, in
    shape order; a slide with none is legitimately empty.
    """

    title: str | None = None
    texts: list[str] = field(default_factory=list)


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


#: The anchor that means "before the first paragraph" — the one insert position
#: no existing paragraph can name, since ``insert after`` always lands *below*
#: its anchor. A document whose first paragraph should be a new title has no
#: other way to be addressed.
TOP_OF_DOCUMENT = -1

#: The mark an ordinary Word paragraph's range ends with.
PARAGRAPH_MARK = "\r"

#: Word's end-of-cell (and end-of-row) marker, which the last paragraph inside a
#: table cell ends with instead. It is *structure*: a write that puts text across
#: it moves the cell boundary. #92 found this in the replace path; it is the same
#: hazard an insert has to refuse.
CELL_MARK = "\x07"

#: What each style intent is called in an English Word. The *fake* uses these as
#: its style names and the probe reads them back; the real bridge addresses the
#: same styles by their built-in enum ids (``office_com.WORD_BUILTIN_STYLES``)
#: precisely so it never has to know this table — a Norwegian Word calls
#: ``Heading 1`` "Overskrift 1", and a write that sent the English string would
#: fail on the user's own machine.
BUILTIN_STYLE_NAMES: dict[WordParagraphStyle, str] = {
    "body": "Normal",
    "heading1": "Heading 1",
    "heading2": "Heading 2",
    "heading3": "Heading 3",
}


def following_style(style: str) -> str:
    """The style Word gives the paragraph you get by pressing Enter at the end of
    one styled ``style`` — the fake's stand-in for ``Style.NextParagraphStyle``.

    This is the rule an insert with **no** named style follows, and it is not the
    same as defaulting to body: continuing after a body paragraph must keep the
    template's own body style (a thesis whose body is ``Body Text`` would
    otherwise sprout ``Normal`` paragraphs among its real ones), while continuing
    after a heading must *not* produce a second heading — which is exactly what
    inheriting would give, because Word's insert copies the anchor's formatting
    where its Enter key does not.
    """
    return "Normal" if style.lower().startswith("heading") else style


def resolve_insert_index(after_paragraph: int | None, total: int) -> int:
    """Where a new paragraph lands, or the refusal both bridges share.

    ``after_paragraph`` is the zero-based paragraph to insert *after*; ``None``
    appends at the end of the document and :data:`TOP_OF_DOCUMENT` (-1) inserts
    before the first paragraph. The answer is the zero-based index the new
    paragraph will occupy — which is also the index every later paragraph shifts
    up from.

    Unlike :func:`check_paragraph`, an empty document is **not** an error here:
    a document with nothing in it is precisely where drafting starts.
    """
    if after_paragraph is None:
        return total
    if after_paragraph == TOP_OF_DOCUMENT:
        return 0
    if after_paragraph < TOP_OF_DOCUMENT:
        raise RangeInvalidError(
            f"{after_paragraph} is not a paragraph to insert after; use -1 for the top of "
            "the document, or omit after_paragraph to append at the end"
        )
    if total == 0:
        raise RangeInvalidError(
            "the document is empty; omit after_paragraph to insert the first paragraph"
        )
    if after_paragraph >= total:
        raise RangeInvalidError(
            f"paragraph {after_paragraph} is past the last paragraph ({total - 1}); "
            "omit after_paragraph to append at the end"
        )
    return after_paragraph + 1


def check_insert_text(text: str) -> None:
    """One insert writes one paragraph — so a line break in ``content`` is refused.

    Not pedantry: a ``\\r`` or ``\\n`` handed to Word's ``Range.Text`` becomes a
    *paragraph mark*, so the document would gain several paragraphs while the
    result reported one index and one new total. Every address the agent computes
    from that answer would then be wrong, which is the exact failure the shift
    reporting exists to prevent. Refusing costs one extra call per paragraph and
    the refusal says so.
    """
    if "\r" in text or "\n" in text:
        raise RangeInvalidError(
            "an insert writes one paragraph, and this content contains a line break. "
            "Insert paragraphs one at a time: each result names the index to insert after "
            "for the next one."
        )


def cell_anchor_error(paragraph: int) -> RangeInvalidError:
    """The refusal both bridges raise for an insert anchored on a table cell's
    last paragraph.

    That paragraph does not end with a paragraph mark: it ends with Word's
    end-of-cell marker, which is *structure*, not text (the ``\\x07`` fidelity
    bug #92 found in the replace path). Inserting across it moves the boundary
    and can merge cells or strand a row, so the anchor is refused by name rather
    than repaired by guesswork — a corrupted table is not something the user can
    undo their way out of by reading a confirmation that claimed success.
    """
    return RangeInvalidError(
        f"paragraph {paragraph} is the last paragraph of a table cell; inserting there would "
        "rewrite the cell boundary. Address a paragraph outside the table, or edit the "
        "table in the panel."
    )


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


def slide_dims(slides: list[SlideContent]) -> list[SlideDim]:
    """The deck's structure: one :class:`SlideDim` per slide, one-based."""
    return [
        SlideDim(index=index, title=slide.title, shapes=len(slide.texts))
        for index, slide in enumerate(slides, start=1)
    ]


def render_slide(index: int, slide: SlideContent) -> str:
    """One slide as the block a reader sees.

    A heading that always names the slide by its one-based number — so a window
    starting at slide 12 is self-locating without counting — and the shape text
    beneath it. A slide with no text says so rather than rendering as a bare
    heading a model could read as a truncation.
    """
    heading = f"Slide {index}" + (f": {slide.title}" if slide.title else "")
    if not slide.texts:
        return f"{heading}\n(no text)"
    return "\n".join([heading, *slide.texts])


def window_slides(slides: list[SlideContent], start_slide: int, max_chars: int) -> SlideText:
    """A window onto ``slides`` from ``start_slide`` (one-based), up to ``max_chars``.

    The PowerPoint mirror of :func:`window_word`, and deliberately the same
    algorithm: whole slides joined by blank lines, stopping before ``max_chars``
    is exceeded, with a lone oversized first slide truncated so a read is never
    empty when there is text to show. The slide is the unit because it is what
    the caller can address on the next call.

    Raises :class:`RangeInvalidError` when ``start_slide`` is past the end of a
    non-empty deck, or is below one — the deck is one-based everywhere the user
    and PowerPoint see it, so a zero here is a caller error worth naming rather
    than quietly treating as the first slide.
    """
    total = len(slides)
    if total == 0:
        return SlideText(
            start_slide=1, returned_slides=0, returned_chars=0, total_slides=0, text=""
        )
    if start_slide < 1:
        raise RangeInvalidError(f"slides are numbered from 1; {start_slide} is not a slide")
    if start_slide > total:
        raise RangeInvalidError(f"start_slide {start_slide} is past the last slide ({total})")
    chunks: list[str] = []
    length = 0
    separator = "\n\n"
    for offset, slide in enumerate(slides[start_slide - 1 :]):
        block = render_slide(start_slide + offset, slide)
        added = (len(separator) if chunks else 0) + len(block)
        if not chunks and len(block) > max_chars:
            chunks.append(block[:max_chars])
            break
        if chunks and length + added > max_chars:
            break
        chunks.append(block)
        length += added
    text = separator.join(chunks)
    return SlideText(
        start_slide=start_slide,
        returned_slides=len(chunks),
        returned_chars=len(text),
        total_slides=total,
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
