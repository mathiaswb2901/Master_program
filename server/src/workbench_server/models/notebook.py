"""Notebook (``.ipynb``) read API schemas — the M4 tail deferred by PR #62.

A notebook is **read** here, never run. There is no kernel, no execution state
and no write path in this module, and that is a product decision rather than a
staging one (ROADMAP M4): a run button that cannot run is worse than no button.
What crosses the wire is therefore a *rendering* of the document — the cells in
order, their source, what they last produced — and nothing a client could use to
change it.

The shape is deliberately **flat**. A notebook output is a MIME bundle in the
file (``{"text/plain": …, "image/png": …, "text/html": …}``) and a union of four
``output_type`` values on top of that; mirroring that structure into TypeScript
would make every renderer re-implement the same "which representation do I
prefer?" decision, differently. So the decision is made once, here, on the
server: each output arrives with the parts a viewer can actually paint, already
chosen, already capped, and with the choice stated (:attr:`NotebookOutput.mime`).

**HTML is carried as source, never as markup.** ``text/html`` in a notebook is
arbitrary author-controlled HTML — scripts, iframes, event handlers — and this
app has no HTML-sanitizing dependency and wants none (``ui/src/markdown.tsx``
makes the same call for chat). The field is named ``html_source`` so a client
that renders it as markup has to lie about what it is holding.
"""

from typing import Literal

from pydantic import BaseModel

#: Cell kinds nbformat 4 defines. Anything else is refused by validation before
#: it reaches us, so this is closed rather than a fallback string.
NotebookCellType = Literal["code", "markdown", "raw"]

#: Output kinds nbformat 4 defines, kept under their own names so a client can
#: style a stream differently from a result without re-deriving which is which.
NotebookOutputType = Literal["stream", "execute_result", "display_data", "error"]

#: Image MIME types carried as data. PNG is what matplotlib and every plotting
#: library in the domain emits by default; the other two cost nothing to accept
#: and are the only other raster bundles nbformat's schema names.
NotebookImageMime = Literal["image/png", "image/jpeg", "image/gif"]

#: A cell's source is capped at this many characters. Generous — a 20k-character
#: cell is already unreadable — and stated on the cell when it bites.
MAX_CELL_SOURCE_CHARS = 20_000

#: Per-output text cap. A runaway loop's stdout is the common case, and the
#: first 10k characters of it say everything the next 3 MB would.
MAX_OUTPUT_TEXT_CHARS = 10_000

#: Cells returned per notebook. Beyond this the response says how many were cut.
MAX_CELLS = 400

#: Refuse to parse anything larger. `MAX_TEXT_FILE_BYTES` is 5 MB and applies to
#: a file a human edits; a notebook legitimately carries base64 images, so this
#: is higher — but it is still a ceiling, because a 200 MB notebook is a JSON
#: parse that would hold the event loop.
MAX_NOTEBOOK_BYTES = 40 * 1024 * 1024


class NotebookImage(BaseModel):
    """A raster output, base64 exactly as the notebook stores it.

    Not re-encoded and not decoded on the way through: the client builds a
    ``data:`` URI from these two fields, which is the one form a browser can
    paint without the bytes ever becoming a URL the network could be asked for.
    """

    mime: NotebookImageMime
    base64: str


class NotebookOutput(BaseModel):
    """One output of one code cell, reduced to what a viewer can paint.

    Exactly one of the payload fields is the *primary* representation, and
    :attr:`mime` names which — so a client renders one thing rather than
    guessing, and a future representation we do not handle arrives as a stated
    MIME type with no payload instead of as silence.
    """

    output_type: NotebookOutputType
    #: The representation chosen from the bundle: ``text/plain``, ``image/png``,
    #: ``text/html`` … or ``""`` for a bundle with nothing we can show.
    mime: str
    #: ``stdout``/``stderr`` for a stream output; ``None`` otherwise.
    stream: str | None = None
    #: Plain text: the stream body, the ``text/plain`` representation, or the
    #: error's traceback with ANSI escapes stripped.
    text: str | None = None
    #: ``text/html`` as *source*. Never markup — see the module docstring.
    html_source: str | None = None
    image: NotebookImage | None = None
    #: Error name and value, for the two lines a traceback buries the point in.
    ename: str | None = None
    evalue: str | None = None
    #: True when :attr:`text` or :attr:`html_source` was cut at the cap above.
    truncated: bool = False


class NotebookCell(BaseModel):
    """One cell, in document order."""

    #: Position-derived (``c0``, ``c1``, …), so it is unique within a response
    #: for every notebook including the pre-4.5 ones whose cells carry no id of
    #: their own. It is a list key, not a link target — nothing addresses a cell
    #: across requests, and inventing an identity that survives an edit would be
    #: a promise this read-only view cannot keep.
    id: str
    cell_type: NotebookCellType
    source: str
    #: ``[n]`` beside a code cell, or ``None`` for one never run. Always
    #: ``None`` for markdown and raw cells.
    execution_count: int | None = None
    outputs: list[NotebookOutput] = []
    #: True when :attr:`source` was cut at :data:`MAX_CELL_SOURCE_CHARS`.
    truncated: bool = False


class NotebookDocument(BaseModel):
    """A whole notebook, read and rendered.

    :attr:`valid` is the *schema* answer, and it is deliberately not fatal: a
    notebook that reads but does not validate (a stale field, a hand-edited
    output) is still worth showing, with the problem said out loud. A notebook
    that does not *read* never becomes one of these — the route answers 422 and
    the panel shows the degraded card instead.
    """

    path: str
    nbformat: int
    nbformat_minor: int
    #: Highlighting language, lowercased, from ``metadata.language_info.name``
    #: or the kernelspec. ``"python"`` when the notebook says nothing — which is
    #: a default, not a claim, and only ever decides a colour.
    language: str
    #: ``metadata.kernelspec.display_name``, for the one line that says what
    #: this notebook was written against. ``None`` when unrecorded.
    kernel: str | None = None
    cells: list[NotebookCell]
    #: How many cells the file has, which is *not* ``len(cells)`` when capped.
    cell_count: int
    #: True when cells beyond :data:`MAX_CELLS` were dropped.
    truncated: bool = False
    valid: bool = True
    #: The first schema complaint, one line, when :attr:`valid` is false.
    validation_message: str | None = None
