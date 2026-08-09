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

PowerPoint arrived with its host and is **read-only** here: :class:`SlideDim` and
:class:`SlideText` have no ``SlideEdit`` beside them. That is a stated boundary,
not an oversight — a slide has no single addressable text run the way a paragraph
or a cell does (text lives in shapes, which are positional and typed), so a write
seam for it needs an addressing scheme this PR does not invent. Reading is the
half that pays for itself immediately: an agent can check the deck it just built.
"""

from typing import Literal

from pydantic import BaseModel, Field

from workbench_server.models.office_host import HostAppKind

#: Whether Excel has finished working out what the workbook's formulas say.
#: Mirrors ``Application.CalculationState``: ``xlDone`` (0), ``xlCalculating``
#: (1), ``xlPending`` (2). Anything but ``done`` means a live read would return
#: numbers Excel is still in the middle of revising — which is a reason to wait,
#: never a reason to call it a pass.
CalculationState = Literal["done", "pending", "calculating"]


class LiveWorkbookStatus(BaseModel):
    """Whether the live docked workbook can be believed right now.

    Two properties, both read straight off the live instance and both cheap
    enough to ask before any cell is touched:

    * :attr:`saved` is ``Workbook.Saved`` — ``False`` means the user has edits
      the ``.xlsx`` on disk does not have, so **the file is not the workbook**.
      That single boolean is what turns "reconcile the file" from a conservative
      fallback into a wrong answer, and it is the front gate of
      ``services/reconciliation.py``.
    * :attr:`calculation` is the application's calculation state. A workbook
      mid-calculation is a third thing, neither clean nor wrong: its formula
      cells hold values Excel has not finished revising.

    Measured over real COM on Windows 11 / Excel / pywin32 311 — ``Saved`` flips
    ``True → False`` across one cell edit and ``CalculationState`` reads ``0``
    when idle (``scripts/dev/probe_live_com.py`` reproduces both).
    """

    #: ``Workbook.Saved``: True when the live instance and the file agree.
    saved: bool
    calculation: CalculationState


class SheetDim(BaseModel):
    """One Excel worksheet and the size of its used range.

    ``rows``/``cols`` are the *used* range, not the sheet's theoretical maximum:
    a blank sheet is ``0, 0``, and that is what lets the reader say "none" rather
    than stream a million empty cells.
    """

    name: str
    rows: int = Field(ge=0)
    cols: int = Field(ge=0)


class SlideDim(BaseModel):
    """One PowerPoint slide, named the way a reader would pick it out.

    ``title`` is the slide's title placeholder when it has one — the thing that
    lets an agent choose a slide without reading the whole deck — and ``None``
    when the layout has no title or the placeholder is empty, never a fabricated
    stand-in. ``shapes`` counts the shapes carrying text *besides* the title, so
    a slide whose only content is its heading reads as ``0`` rather than
    promising a body that is not there.
    """

    #: One-based, as PowerPoint numbers slides everywhere the user sees them.
    index: int = Field(ge=1)
    title: str | None = None
    shapes: int = Field(ge=0)


class DocStructure(BaseModel):
    """The shape of a hosted document, cheap enough to fetch before reading.

    Exactly one of ``paragraph_count`` / ``sheets`` / ``slides`` is set, chosen
    by ``kind``: Word documents are a flat paragraph stream, Excel workbooks are
    a set of named sheets, PowerPoint decks are an ordered list of slides. It is
    what the tool returns when asked to read an Excel document without naming a
    sheet — the list of sheets to pick from.
    """

    kind: HostAppKind
    #: Word only: how many paragraphs the body has.
    paragraph_count: int | None = None
    #: Excel only: every worksheet and its used-range dimensions.
    sheets: list[SheetDim] | None = None
    #: PowerPoint only: every slide, in deck order.
    slides: list[SlideDim] | None = None


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


class SlideText(BaseModel):
    """A window onto a PowerPoint deck's text, addressed by slide.

    The PowerPoint mirror of :class:`WordText`: whole slides, starting at
    ``start_slide`` and stopping when ``max_chars`` would be exceeded, so the
    caller widens the window by asking again from a later slide — which is why
    ``total_slides`` travels with every read. A deck's text is the text of the
    shapes on each slide; there is no equivalent of a paragraph stream, so the
    slide is the unit of both addressing and truncation.
    """

    #: One-based index of the first slide in ``text``, matching :class:`SlideDim`
    #: and what PowerPoint shows the user.
    start_slide: int = Field(ge=1)
    #: How many slides ``text`` actually covers — the caller's next start is
    #: ``start_slide + returned_slides``.
    returned_slides: int = Field(ge=0)
    #: ``len(text)`` — stated so a truncated read is self-describing.
    returned_chars: int = Field(ge=0)
    #: The deck's slide count, so the reader knows what it did not see.
    total_slides: int = Field(ge=0)
    text: str


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
