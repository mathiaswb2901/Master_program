"""Read and write models for the Office COM bridge — the *live* docked document.

These describe what an agent gets back when it reads or edits the real Word or
Excel document Workbench has hosted in a panel (``services/office_host/``). They
are the typed values of the ``DocumentBridge`` seam
(``services/office_host/document_bridge.py``), and they are **not** mirrored in
``ui/src/types.ts``: nothing on the wire carries them. They are consumed
in-process by the ``office_read`` and ``office_write`` agent tools
(``services/agent_tools.py``), which render them to the compact text the model
reads. Modelling them anyway keeps the seam honest — the fake and the real COM
implementation must both produce the same shape — and keeps every field
``mypy --strict`` can check.

The read half arrived first (PR 1), because reading the live document — including
the user's unsaved on-screen edits — is the whole point of hosting a real Word
instead of a preview. The write half (PR 2, the ``*Edit`` return values below) is
its mirror: a *targeted* edit — one addressed paragraph or one cell — that leaves
the rest of the document untouched, applied to the live in-memory instance so the
user can undo it, never a whole-file rewrite.
"""

from pydantic import BaseModel, Field

from workbench_server.models.office_host import HostAppKind


class SheetDim(BaseModel):
    """One Excel worksheet and the size of its used range.

    ``rows``/``cols`` are the *used* range, not the sheet's theoretical maximum:
    a blank sheet is ``0, 0``, and that is what lets the reader say "none" rather
    than stream a million empty cells.
    """

    name: str
    rows: int = Field(ge=0)
    cols: int = Field(ge=0)


class DocStructure(BaseModel):
    """The shape of a hosted document, cheap enough to fetch before reading.

    Exactly one of ``paragraph_count`` / ``sheets`` is set, chosen by ``kind``:
    Word documents are a flat paragraph stream, Excel workbooks are a set of
    named sheets. It is what the tool returns when asked to read an Excel
    document without naming a sheet — the list of sheets to pick from.
    """

    kind: HostAppKind
    #: Word only: how many paragraphs the body has.
    paragraph_count: int | None = None
    #: Excel only: every worksheet and its used-range dimensions.
    sheets: list[SheetDim] | None = None


class WordText(BaseModel):
    """A window onto a Word document's body, addressed by paragraph.

    ``text`` holds whole paragraphs joined by blank lines, starting at
    ``start_paragraph`` and stopping when ``max_chars`` would be exceeded. The
    caller widens the window by asking again from a later paragraph — which is
    why ``total_paragraphs`` travels with every read.
    """

    #: Zero-based index of the first paragraph in ``text``.
    start_paragraph: int = Field(ge=0)
    #: ``len(text)`` — stated so a truncated read is self-describing.
    returned_chars: int = Field(ge=0)
    #: The document's paragraph count, so the reader knows what it did not see.
    total_paragraphs: int = Field(ge=0)
    text: str


class CellWindow(BaseModel):
    """A rectangular window onto one Excel worksheet.

    ``rows``/``cols`` describe the window actually returned; ``total_rows``/
    ``total_cols`` describe the sheet's whole used range, so a window smaller
    than the sheet is self-describing and the reader knows the range still to
    ask for.
    """

    sheet: str
    #: A1 range actually returned, e.g. ``"A1:H50"``. Empty string for a blank
    #: sheet (there is no cell to name).
    a1_range: str
    rows: int = Field(ge=0)
    cols: int = Field(ge=0)
    total_rows: int = Field(ge=0)
    total_cols: int = Field(ge=0)
    #: Row-major grid of cell text, ``rows`` lists of ``cols`` strings. Blank
    #: cells are the empty string, never null, so the grid is always rectangular.
    cells: list[list[str]] = Field(default_factory=list)


class WordEdit(BaseModel):
    """Confirmation of a single, targeted write to a Word paragraph.

    The write replaces the text of one addressed paragraph and nothing else —
    the mirror of :class:`WordText`'s windowed read. ``total_paragraphs`` travels
    back so the caller knows the write did not change the document's shape (a
    replace never adds or removes a paragraph), and ``written_chars`` is
    ``len(new text)`` so a truncated echo in the tool result is self-describing.
    """

    #: Zero-based index of the paragraph whose text was replaced.
    paragraph: int = Field(ge=0)
    #: ``len`` of the new paragraph text — 0 means the paragraph was emptied.
    written_chars: int = Field(ge=0)
    #: The document's paragraph count after the edit (unchanged by a replace).
    total_paragraphs: int = Field(ge=0)


class CellEdit(BaseModel):
    """Confirmation of a single, targeted write to one Excel cell.

    The write sets the text of one addressed cell and nothing else — the mirror
    of :class:`CellWindow`'s windowed read. Empty content clears the cell, which
    ``written_chars == 0`` records, so the tool can say "cleared" rather than
    leave the reader guessing.
    """

    sheet: str
    #: A1 address written, e.g. ``"B2"``.
    a1_cell: str
    #: ``len`` of the value written — 0 means the cell was cleared.
    written_chars: int = Field(ge=0)
