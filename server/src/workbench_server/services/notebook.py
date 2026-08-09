"""Read a ``.ipynb`` into the flat, paintable shape ``models/notebook.py`` defines.

Reading is done by **nbformat**, Jupyter's own reference implementation, and the
two calls it contributes are the two questions this module cannot answer itself:

``nbformat.reads(text, as_version=4)``
    parses *and migrates*. A notebook written in 2015 is nbformat 3 — different
    key names, outputs shaped differently, no ``cell_type: "raw"`` — and this
    call hands back a version-4 document either way. Without it, "corrupt" and
    "old" would be the same answer, and the older one is the one a user is most
    likely to have lying around.

``nbformat.validate(nb)``
    is the schema the format is *defined* by. It is asked **after** the read and
    its failure is **not** fatal: a notebook that parses but does not validate
    (a hand-edited output, a field a newer tool added) still renders, with the
    complaint said out loud on the document. Only a notebook that will not read
    at all becomes a refusal, and then the reason travels with it.

Everything else here is our decision, made once so no renderer has to make it
again: which representation to take out of a MIME bundle, what a cap is, and
what ANSI escapes in a traceback become. The jail is the workspace's own —
:meth:`Workspace.safe_path` — so a notebook route is no more reachable outside
the workspace than a file route is.

No execution, no kernel, no write path. ``nbclient``/``jupyter-client`` are not
dependencies and a notebook read here can never be run.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import nbformat
import structlog
from nbformat.validator import ValidationError

from workbench_server.models.notebook import (
    MAX_CELL_SOURCE_CHARS,
    MAX_CELLS,
    MAX_IMAGE_BASE64_CHARS,
    MAX_IMAGE_BASE64_TOTAL_CHARS,
    MAX_NOTEBOOK_BYTES,
    MAX_OUTPUT_TEXT_CHARS,
    NotebookCell,
    NotebookCellType,
    NotebookDocument,
    NotebookImage,
    NotebookImageMime,
    NotebookOutput,
    NotebookOutputType,
)
from workbench_server.services.workspace import Workspace

log = structlog.get_logger()

NOTEBOOK_SUFFIX = ".ipynb"

#: `nbformat` ships `py.typed` but `reads` itself is undecorated, so calling it
#: from `--strict` code is a `no-untyped-call`. Named once here, with the
#: signature we actually rely on, rather than silenced at the call site: the
#: return really is `Any` (a `NotebookNode`, which is a `dict` whose values are
#: whatever the JSON held), and the coercion helpers at the bottom of this
#: module are where that `Any` stops.
_reads: Callable[[str, int], Any] = cast("Callable[[str, int], Any]", nbformat.reads)

#: CSI escapes, which is what IPython colours a traceback with. Stripped rather
#: than rendered: the alternative is a second ANSI interpreter in the browser
#: for the one surface that is not a terminal.
_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: Raster bundles we can hand a browser as a ``data:`` URI, most-preferred first.
_IMAGE_MIMES: tuple[str, ...] = ("image/png", "image/jpeg", "image/gif")

#: Cell kinds nbformat 4 defines. Anything else in the file is shown as ``raw``
#: rather than dropped — an unknown cell is still the user's content.
_CELL_TYPES: frozenset[str] = frozenset({"code", "markdown", "raw"})

#: Likewise for outputs: an unrecognised ``output_type`` is rendered as display
#: data, which is the branch that shows whatever representation it can find.
_OUTPUT_TYPES: frozenset[str] = frozenset({"stream", "execute_result", "display_data", "error"})

#: Outputs rendered per code cell. The per-output byte caps in ``models`` bound
#: each output's *size*; this bounds their *number*. A cell rarely has more than
#: a handful, and past this many it is not being read output-by-output anyway —
#: while thousands of them (a ``display()`` in a tight loop, a progress bar
#: re-emitting each step) is exactly what balloons a response those byte caps do
#: not catch, because every dropped output is individually small. Mirrors the
#: cell cap in shape: the overflow is dropped and a single trailing notice output
#: says how many. That notice is an empty-``mime`` text output — the same shape
#: :func:`_bundle` already emits for a bundle it cannot paint — so it states the
#: cut without any renderer, or any model field, having to learn a new one.
MAX_OUTPUTS_PER_CELL = 50


@dataclass
class _ImageBudget:
    """What one response may still spend on base64 image payloads.

    Threaded down to :func:`_bundle` rather than applied per output, because the
    limit worth having is the **response's**: a per-image cap on its own would
    call four hundred cells of just-under-the-cap figures a fine reply. One of
    these per :func:`read_notebook` call, so nothing is carried between reads.
    """

    remaining: int = MAX_IMAGE_BASE64_TOTAL_CHARS

    def take(self, chars: int) -> bool:
        """Spend ``chars`` on one image, or refuse and leave the budget alone.

        Refusing outright — rather than handing back a smaller allowance — is
        the whole shape of this cap: a truncated base64 string decodes to a
        corrupt image, which is a worse answer than a stated absence.
        """
        if chars > MAX_IMAGE_BASE64_CHARS or chars > self.remaining:
            return False
        self.remaining -= chars
        return True


class NotANotebookError(ValueError):
    """The path does not name a ``.ipynb`` file.

    A guard on the route rather than a parse failure: without it, any file in
    the workspace could be handed to a JSON parser by asking for it here.
    """


class NotebookTooLargeError(ValueError):
    """Past :data:`MAX_NOTEBOOK_BYTES`. Refused before a byte is parsed."""


class NotebookUnreadableError(ValueError):
    """The file is not a notebook this reader can make sense of.

    Carries the reason, because the panel's degraded card shows it: "what
    failed" plus a way to look at the raw file is the difference between a card
    a user can act on and a blank pane.
    """


def read_notebook(workspace: Workspace, relative: str) -> NotebookDocument:
    """Parse the notebook at ``relative`` (workspace-relative, forward slashes).

    Raises :class:`~workbench_server.services.workspace.PathOutsideWorkspaceError`
    for a path outside the jail, :class:`FileNotFoundError`,
    :class:`IsADirectoryError`, :class:`NotANotebookError`,
    :class:`NotebookTooLargeError` and :class:`NotebookUnreadableError`.
    """
    path = workspace.safe_path(relative)
    # Suffix, not content sniffing: a notebook is named `.ipynb` by universal
    # convention, and `.lower()` here compares an extension string — it assumes
    # nothing about whether the *filesystem* is case-sensitive, so this reads the
    # same on all three platforms the CI matrix runs.
    if path.suffix.lower() != NOTEBOOK_SUFFIX:
        raise NotANotebookError(relative)
    # Asked before opening, for the reason `Workspace.read_text` documents:
    # opening a directory raises IsADirectoryError on POSIX and PermissionError
    # on Windows, and this is a Windows-first app that now ships on three.
    if path.is_dir():
        raise IsADirectoryError(relative)
    size = path.stat().st_size  # raises FileNotFoundError, which the router maps
    if size > MAX_NOTEBOOK_BYTES:
        raise NotebookTooLargeError(f"{size} bytes")

    text = path.read_bytes().decode("utf-8", errors="replace")
    try:
        notebook = _reads(text, 4)
    except ValidationError as exc:  # a v3 file whose migration will not validate
        raise NotebookUnreadableError(_first_line(str(exc))) from exc
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # NotJSONError and UnicodeDecodeError are both ValueError; the other
        # three are what a structurally-wrong-but-parseable JSON document
        # produces inside nbformat's own converters.
        raise NotebookUnreadableError(_reason(exc)) from exc

    validation_message: str | None = None
    try:
        nbformat.validate(notebook)
    except ValidationError as exc:
        # Not fatal, deliberately — see the module docstring.
        validation_message = _first_line(str(exc))
        log.info("notebook.invalid", path=relative, problem=validation_message)

    raw_cells = _as_list(_get(notebook, "cells"))
    kept = raw_cells[:MAX_CELLS]
    # Spent down the document in reading order, so the figures a reader reaches
    # first are the ones that arrive whole.
    budget = _ImageBudget()
    return NotebookDocument(
        path=relative,
        nbformat=_as_int(_get(notebook, "nbformat")) or 4,
        nbformat_minor=_as_int(_get(notebook, "nbformat_minor")) or 0,
        language=_language(notebook),
        kernel=_kernel(notebook),
        cells=[_cell(index, cell, budget) for index, cell in enumerate(kept)],
        cell_count=len(raw_cells),
        truncated=len(raw_cells) > len(kept),
        valid=validation_message is None,
        validation_message=validation_message,
    )


# ---- cells -------------------------------------------------------------------


def _cell(index: int, raw: Any, budget: _ImageBudget) -> NotebookCell:
    kind = _as_str(_get(raw, "cell_type"))
    cell_type: NotebookCellType = (
        "code" if kind == "code" else "markdown" if kind == "markdown" else "raw"
    )
    if kind not in _CELL_TYPES:
        log.info("notebook.unknown_cell_type", cell_type=kind, index=index)
    source, source_cut = _cap(_joined(_get(raw, "source")), MAX_CELL_SOURCE_CHARS)
    outputs = _outputs(raw, budget) if cell_type == "code" else []
    return NotebookCell(
        id=f"c{index}",
        cell_type=cell_type,
        source=source,
        execution_count=(_as_int(_get(raw, "execution_count")) if cell_type == "code" else None),
        outputs=outputs,
        truncated=source_cut,
    )


# ---- outputs -----------------------------------------------------------------


def _outputs(raw: Any, budget: _ImageBudget) -> list[NotebookOutput]:
    """A code cell's outputs, capped in **count** as well as in bytes.

    The per-output byte caps bound each output's size; this bounds their number
    at :data:`MAX_OUTPUTS_PER_CELL`. The overflow is dropped in reading order —
    so the outputs a reader reaches first are the ones that survive — and a
    single trailing notice states how many went, in the empty-``mime`` text
    shape :func:`_bundle` already produces for a bundle it cannot paint. Building
    only the kept outputs is also why a cell of ten thousand figures is not ten
    thousand base64 decodes before the budget above ever bites.
    """
    raw_outputs = _as_list(_get(raw, "outputs"))
    kept = raw_outputs[:MAX_OUTPUTS_PER_CELL]
    outputs = [_output(output, budget) for output in kept]
    dropped = len(raw_outputs) - len(kept)
    if dropped > 0:
        outputs.append(
            NotebookOutput(
                output_type="display_data",
                mime="",
                text=(
                    f"{dropped:,} more outputs not shown — this cell produced "
                    f"{len(raw_outputs):,}, capped at {MAX_OUTPUTS_PER_CELL}. "
                    "Open the notebook file to see the rest."
                ),
                truncated=True,
            )
        )
    return outputs


def _output(raw: Any, budget: _ImageBudget) -> NotebookOutput:
    kind = _as_str(_get(raw, "output_type"))
    if kind == "stream":
        text, cut = _cap(_joined(_get(raw, "text")), MAX_OUTPUT_TEXT_CHARS)
        return NotebookOutput(
            output_type="stream",
            mime="text/plain",
            # `stderr` is the only value that changes how this is painted, so a
            # missing name reads as stdout rather than as its own third state.
            stream=_as_str(_get(raw, "name")) or "stdout",
            text=text,
            truncated=cut,
        )
    if kind == "error":
        traceback = _ANSI_CSI.sub("", _joined(_get(raw, "traceback"), separator="\n"))
        text, cut = _cap(traceback, MAX_OUTPUT_TEXT_CHARS)
        return NotebookOutput(
            output_type="error",
            mime="text/plain",
            text=text,
            ename=_as_str(_get(raw, "ename")) or None,
            evalue=_as_str(_get(raw, "evalue")) or None,
            truncated=cut,
        )
    output_type: NotebookOutputType = (
        "execute_result" if kind == "execute_result" else "display_data"
    )
    if kind not in _OUTPUT_TYPES:
        log.info("notebook.unknown_output_type", output_type=kind)
    return _bundle(output_type, _get(raw, "data"), budget)


def _bundle(output_type: NotebookOutputType, data: Any, budget: _ImageBudget) -> NotebookOutput:
    """Pick the one representation to paint out of a MIME bundle.

    Order: **image, then plain text, then HTML source.** An image is the whole
    point of the output that carries one (a matplotlib figure also ships a
    ``text/plain`` ``<Figure …>`` that says nothing). Plain text beats HTML
    because we do not render HTML — a DataFrame's ``text/plain`` repr is the
    readable one, and taking the HTML would mean showing markup where a table
    should be. HTML is the primary representation only when it is the *only*
    one, and then it is shown as source, which is honest about what it is.

    Carrying HTML we are not going to show would put a DataFrame's ~50 KB of
    ``<table>`` on the wire for every result in the notebook, so it is left on
    disk. Nothing is lost that the user cannot get from the file itself.

    An image is the one representation that can be **too big to carry**, and it
    is the one that is dropped whole rather than cut: see :class:`_ImageBudget`
    and :data:`MAX_IMAGE_BASE64_CHARS`. When that happens the output still
    arrives — same ``output_type``, same ``mime`` naming what kind of picture it
    was, ``image_omitted_bytes`` saying how big, ``truncated`` set. It does
    *not* fall through to the ``text/plain`` beside it: ``<Figure size 640x480
    with 1 Axes>`` in place of a stated omission is how a viewer ends up looking
    like it lost the figure.
    """
    for mime in _IMAGE_MIMES:
        raw = _get(data, mime)
        if isinstance(raw, str) and raw.strip() != "":
            # Whitespace out: notebooks store base64 wrapped at 76 columns, and
            # a `data:` URI wants it in one piece. Also the length the budget is
            # charged in, so what is measured is what would go on the wire.
            payload = _unwrap(raw)
            if not budget.take(len(payload)):
                log.info("notebook.image_omitted", mime=mime, base64_chars=len(payload))
                return NotebookOutput(
                    output_type=output_type,
                    mime=mime,
                    image_omitted_bytes=_base64_bytes(payload),
                    truncated=True,
                )
            return NotebookOutput(
                output_type=output_type,
                mime=mime,
                image=NotebookImage(
                    # `mime` came from `_IMAGE_MIMES`, which is exactly the
                    # model's Literal — the cast records that rather than
                    # re-deriving it at runtime.
                    mime=cast(NotebookImageMime, mime),
                    base64=payload,
                ),
            )
    plain = _get(data, "text/plain")
    if plain is not None:
        text, cut = _cap(_joined(plain), MAX_OUTPUT_TEXT_CHARS)
        return NotebookOutput(output_type=output_type, mime="text/plain", text=text, truncated=cut)
    html = _get(data, "text/html")
    if html is not None:
        source, cut = _cap(_joined(html), MAX_OUTPUT_TEXT_CHARS)
        return NotebookOutput(
            output_type=output_type,
            mime="text/html",
            html_source=source,
            truncated=cut,
        )
    # A bundle of representations we cannot paint (a widget's JSON state, a
    # vendor MIME type). Say which ones, rather than rendering nothing: an empty
    # output box with no explanation reads as a bug in the viewer.
    available = ", ".join(sorted(_keys(data)))
    return NotebookOutput(
        output_type=output_type,
        mime="",
        text=f"No displayable representation ({available})" if available else None,
    )


# ---- notebook metadata -------------------------------------------------------


def _language(notebook: Any) -> str:
    """The highlighting language: ``language_info`` first, kernelspec second.

    ``language_info`` is written by the kernel that actually ran the notebook,
    so it is the one that is true; the kernelspec is what the notebook *asks*
    for and survives a document that was never run. Neither is a guarantee, and
    the value only ever decides a colour.
    """
    metadata = _get(notebook, "metadata")
    for holder, key in (("language_info", "name"), ("kernelspec", "language")):
        value = _as_str(_get(_get(metadata, holder), key)).strip().lower()
        if value != "":
            return value
    return "python"


def _kernel(notebook: Any) -> str | None:
    display = _as_str(_get(_get(_get(notebook, "metadata"), "kernelspec"), "display_name")).strip()
    return display or None


# ---- coercion ----------------------------------------------------------------
#
# A notebook that validates has exactly the shapes below, but this reader also
# serves the ones that only *read* — so every access is total. `nbformat` hands
# back `NotebookNode` (a dict subclass), and the values inside it are whatever
# the JSON held; `Any` is the honest type for that, and these five functions are
# where it stops.


def _get(container: Any, key: str) -> Any:
    return container.get(key) if isinstance(container, dict) else None


def _keys(container: Any) -> list[str]:
    return [str(key) for key in container] if isinstance(container, dict) else []


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int | None:
    # `bool` is an `int`; an execution count of `True` is a corrupt file, not 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _joined(value: Any, separator: str = "") -> str:
    """A notebook string field, which the format allows to be a list of lines.

    nbformat's reader rejoins these for us on the happy path; this is what makes
    the unhappy one (a file that read but did not validate) render text instead
    of ``['a\\n', 'b\\n']``.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return separator.join(item for item in value if isinstance(item, str))
    return ""


def _cap(text: str, limit: int) -> tuple[str, bool]:
    """Cut to ``limit`` characters, and say so. The flag is what the client
    turns into "3,000 characters not shown" — a cap nobody is told about is the
    one that makes a reader doubt everything else on screen."""
    return (text, False) if len(text) <= limit else (text[:limit], True)


def _unwrap(base64_text: str) -> str:
    return "".join(base64_text.split())


def _base64_bytes(text: str) -> int:
    """What ``text`` would decode to, without decoding it.

    The number is for a sentence a human reads ("2.4 MB"), and decoding a
    payload we have just decided is too big to carry — only to throw the bytes
    away and keep their length — is the one thing this branch exists to avoid.
    Exact for the padded, well-formed base64 a notebook holds.
    """
    padding = len(text) - len(text.rstrip("="))
    return max(len(text) // 4 * 3 - padding, 0)


def _first_line(message: str) -> str:
    """One line out of a jsonschema complaint, which is a paragraph with a
    rendered instance in it — unbounded, and often the whole notebook."""
    line = message.strip().split("\n", 1)[0].strip()
    return line[:200] if line else "does not match the notebook schema"


def _reason(exc: Exception) -> str:
    text = _first_line(str(exc))
    return text or type(exc).__name__


__all__ = [
    "NOTEBOOK_SUFFIX",
    "NotANotebookError",
    "NotebookTooLargeError",
    "NotebookUnreadableError",
    "read_notebook",
]
