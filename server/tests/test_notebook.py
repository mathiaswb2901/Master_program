"""Reading a notebook: what renders, what is refused, and what is capped.

The notebook view's whole claim is that a `.ipynb` is a *document* rather than a
blob of JSON, so these tests are about the shapes a real notebook actually
contains — a markdown cell, a code cell that printed, one that drew a figure,
one that raised — plus the two failure modes a user hits by accident: a file
that is not a notebook at all, and one that is a notebook from 2015.

Nothing here executes anything. There is no kernel in this feature and no test
below could summon one.
"""

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from workbench_server.models.notebook import (
    MAX_CELL_SOURCE_CHARS,
    MAX_CELLS,
    MAX_NOTEBOOK_BYTES,
    MAX_OUTPUT_TEXT_CHARS,
    NotebookDocument,
)
from workbench_server.services.notebook import (
    NotANotebookError,
    NotebookTooLargeError,
    NotebookUnreadableError,
    read_notebook,
)
from workbench_server.services.workspace import PathOutsideWorkspaceError, Workspace

# A 1x1 transparent PNG — the smallest thing that is genuinely an image, so the
# image path is exercised with bytes a browser would really paint.
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _code(
    source: str, execution_count: int | None = None, outputs: object = None
) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "metadata": {},
        "source": source,
        "execution_count": execution_count,
        "outputs": outputs if outputs is not None else [],
    }


def _markdown(source: str) -> dict[str, Any]:
    return {"cell_type": "markdown", "metadata": {}, "source": source}


def _notebook(cells: list[dict[str, Any]], **metadata: object) -> dict[str, Any]:
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.11.9"},
            **metadata,
        },
        "nbformat": 4,
        # 4.4, not 4.5: cell ids became required in 4.5, and a fixture that
        # claims 4.5 without them makes every `validate` call in this file emit
        # nbformat's MissingIDFieldWarning. The 4.5 shape has its own test below.
        "nbformat_minor": 4,
    }


def _write(root: Path, relative: str, document: object) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else json.dumps(document)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


# ---- the happy path ----------------------------------------------------------


class TestReadsARealNotebook:
    def test_cells_arrive_in_order_with_their_kinds(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        _write(
            tmp_path,
            "analysis.ipynb",
            _notebook(
                [
                    _markdown("# SE3 prices\n\nA look at the spread."),
                    _code("import pandas as pd", execution_count=1),
                    {"cell_type": "raw", "metadata": {}, "source": "raw text"},
                ]
            ),
        )
        doc = read_notebook(workspace, "analysis.ipynb")

        assert [cell.cell_type for cell in doc.cells] == ["markdown", "code", "raw"]
        assert doc.cells[0].source.startswith("# SE3 prices")
        assert doc.cells[1].execution_count == 1
        # Cell ids are unique within the response — that is the whole contract.
        assert len({cell.id for cell in doc.cells}) == len(doc.cells)
        assert doc.cell_count == 3
        assert not doc.truncated

    def test_metadata_names_the_language_and_the_kernel(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        _write(tmp_path, "nb.ipynb", _notebook([_markdown("hi")]))
        doc = read_notebook(workspace, "nb.ipynb")
        assert doc.language == "python"
        assert doc.kernel == "Python 3 (ipykernel)"
        assert doc.valid and doc.validation_message is None

    def test_language_falls_back_to_the_kernelspec_then_to_python(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # `language_info` is written by the kernel that ran it; a notebook that
        # was never run has only the kernelspec.
        _write(
            tmp_path,
            "r.ipynb",
            _notebook(
                [_markdown("x")],
                kernelspec={"display_name": "R", "language": "R", "name": "ir"},
                language_info={},
            ),
        )
        assert read_notebook(workspace, "r.ipynb").language == "r"

        bare = _notebook([_markdown("x")])
        bare["metadata"] = {}
        _write(tmp_path, "bare.ipynb", bare)
        # A default, never a claim — and it only ever decides a colour.
        assert read_notebook(workspace, "bare.ipynb").language == "python"

    def test_a_markdown_cell_never_carries_outputs_or_a_count(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        cell = _markdown("text")
        cell["outputs"] = [{"output_type": "stream", "name": "stdout", "text": "leak"}]
        cell["execution_count"] = 7
        _write(tmp_path, "nb.ipynb", _notebook([cell]))
        rendered = read_notebook(workspace, "nb.ipynb").cells[0]
        assert rendered.outputs == []
        assert rendered.execution_count is None


# ---- outputs -----------------------------------------------------------------


class TestOutputs:
    def test_stream_output_keeps_its_name(self, tmp_path: Path, workspace: Workspace) -> None:
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "print('hi')",
                        1,
                        [
                            {"output_type": "stream", "name": "stdout", "text": ["hi\n"]},
                            {"output_type": "stream", "name": "stderr", "text": "warn\n"},
                        ],
                    )
                ]
            ),
        )
        outputs = read_notebook(workspace, "nb.ipynb").cells[0].outputs
        assert [(o.stream, o.text) for o in outputs] == [("stdout", "hi\n"), ("stderr", "warn\n")]
        assert all(o.output_type == "stream" for o in outputs)

    def test_an_image_wins_over_the_text_repr_beside_it(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # matplotlib emits both; `<Figure size 640x480>` is not what to show.
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "df.plot()",
                        2,
                        [
                            {
                                "output_type": "display_data",
                                "data": {
                                    "image/png": PNG_BASE64,
                                    "text/plain": "<Figure size 640x480 with 1 Axes>",
                                },
                                "metadata": {},
                            }
                        ],
                    )
                ]
            ),
        )
        output = read_notebook(workspace, "nb.ipynb").cells[0].outputs[0]
        assert output.mime == "image/png"
        assert output.image is not None
        # Round-trips to the exact bytes a browser will paint from the data URI.
        assert base64.b64decode(output.image.base64)[:8] == b"\x89PNG\r\n\x1a\n"

    def test_wrapped_base64_is_rejoined(self, tmp_path: Path, workspace: Workspace) -> None:
        # Notebooks store base64 wrapped at 76 columns; a `data:` URI wants one
        # piece, and a newline inside it is what makes an image silently blank.
        wrapped = "\n".join([PNG_BASE64[:40], PNG_BASE64[40:]])
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "img",
                        1,
                        [
                            {
                                "output_type": "display_data",
                                "data": {"image/png": wrapped},
                                "metadata": {},
                            }
                        ],
                    )
                ]
            ),
        )
        image = read_notebook(workspace, "nb.ipynb").cells[0].outputs[0].image
        assert image is not None and image.base64 == PNG_BASE64

    def test_plain_text_wins_over_html_and_the_html_is_not_carried(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # A DataFrame ships both. We render no HTML, so the readable one is the
        # text repr — and the ~50 KB of `<table>` stays on disk.
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "df",
                        3,
                        [
                            {
                                "output_type": "execute_result",
                                "execution_count": 3,
                                "data": {
                                    "text/html": "<table><tr><td>4.2</td></tr></table>",
                                    "text/plain": "   mw\n0 4.2",
                                },
                                "metadata": {},
                            }
                        ],
                    )
                ]
            ),
        )
        output = read_notebook(workspace, "nb.ipynb").cells[0].outputs[0]
        assert output.mime == "text/plain"
        assert output.text == "   mw\n0 4.2"
        assert output.html_source is None

    def test_html_only_output_is_carried_as_source_never_as_markup(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        hostile = '<img src=x onerror="alert(1)">'
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "HTML(...)",
                        1,
                        [
                            {
                                "output_type": "display_data",
                                "data": {"text/html": hostile},
                                "metadata": {},
                            }
                        ],
                    )
                ]
            ),
        )
        output = read_notebook(workspace, "nb.ipynb").cells[0].outputs[0]
        assert output.mime == "text/html"
        # Verbatim, in a field whose name says it is source. The client renders
        # it as text; nothing on either side treats it as markup.
        assert output.html_source == hostile
        assert output.text is None

    def test_an_error_output_keeps_the_name_and_loses_the_ansi(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "1/0",
                        4,
                        [
                            {
                                "output_type": "error",
                                "ename": "ZeroDivisionError",
                                "evalue": "division by zero",
                                "traceback": [
                                    "\x1b[0;31m---------\x1b[0m",
                                    "\x1b[0;32m----> 1\x1b[0m \x1b[38;5;241m1\x1b[0m"
                                    "/\x1b[38;5;241m0\x1b[0m",
                                ],
                            }
                        ],
                    )
                ]
            ),
        )
        output = read_notebook(workspace, "nb.ipynb").cells[0].outputs[0]
        assert output.output_type == "error"
        assert output.ename == "ZeroDivisionError"
        assert output.evalue == "division by zero"
        assert output.text is not None
        assert "\x1b[" not in output.text
        assert "---------" in output.text and "----> 1" in output.text

    def test_a_bundle_we_cannot_paint_says_what_it_held(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # An ipywidget. Rendering nothing here reads as a broken viewer.
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "slider",
                        1,
                        [
                            {
                                "output_type": "display_data",
                                "data": {"application/vnd.jupyter.widget-view+json": {"v": 1}},
                                "metadata": {},
                            }
                        ],
                    )
                ]
            ),
        )
        output = read_notebook(workspace, "nb.ipynb").cells[0].outputs[0]
        assert output.mime == ""
        assert output.text is not None
        assert "application/vnd.jupyter.widget-view+json" in output.text


# ---- caps: every one of them says it bit --------------------------------------


class TestCaps:
    def test_a_long_cell_is_cut_and_says_so(self, tmp_path: Path, workspace: Workspace) -> None:
        _write(tmp_path, "nb.ipynb", _notebook([_code("x" * (MAX_CELL_SOURCE_CHARS + 500), 1)]))
        cell = read_notebook(workspace, "nb.ipynb").cells[0]
        assert cell.truncated
        assert len(cell.source) == MAX_CELL_SOURCE_CHARS

    def test_runaway_output_is_cut_and_says_so(self, tmp_path: Path, workspace: Workspace) -> None:
        _write(
            tmp_path,
            "nb.ipynb",
            _notebook(
                [
                    _code(
                        "for i in range(10**6): print(i)",
                        1,
                        [
                            {
                                "output_type": "stream",
                                "name": "stdout",
                                "text": "y" * (MAX_OUTPUT_TEXT_CHARS + 1),
                            }
                        ],
                    )
                ]
            ),
        )
        output = read_notebook(workspace, "nb.ipynb").cells[0].outputs[0]
        assert output.truncated and output.text is not None
        assert len(output.text) == MAX_OUTPUT_TEXT_CHARS

    def test_too_many_cells_keeps_the_true_count(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        _write(
            tmp_path, "nb.ipynb", _notebook([_markdown(f"cell {i}") for i in range(MAX_CELLS + 7)])
        )
        doc = read_notebook(workspace, "nb.ipynb")
        assert len(doc.cells) == MAX_CELLS
        assert doc.truncated
        # The number the panel shows is the file's, not the response's — a cap
        # nobody is told the size of is the one that makes a reader doubt the rest.
        assert doc.cell_count == MAX_CELLS + 7

    def test_an_absurd_file_is_refused_before_it_is_parsed(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        path = tmp_path / "huge.ipynb"
        path.write_bytes(b"{" + b" " * MAX_NOTEBOOK_BYTES)
        with pytest.raises(NotebookTooLargeError):
            read_notebook(workspace, "huge.ipynb")


# ---- refusals and degradation -------------------------------------------------


class TestRefusals:
    def test_a_path_outside_the_workspace_is_refused(self, workspace: Workspace) -> None:
        with pytest.raises(PathOutsideWorkspaceError):
            read_notebook(workspace, "../escape.ipynb")

    def test_a_file_that_is_not_a_notebook_is_refused_by_name(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # Without this guard, any file in the workspace could be handed to a
        # JSON parser by asking for it here.
        (tmp_path / "secrets.env").write_text("TOKEN=1", encoding="utf-8")
        with pytest.raises(NotANotebookError):
            read_notebook(workspace, "secrets.env")

    def test_the_extension_check_is_on_the_name_not_the_filesystem(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # An uppercase extension is a naming convention question, answered the
        # same way on a case-sensitive filesystem as on a case-folding one.
        _write(tmp_path, "SHOUTED.IPYNB", _notebook([_markdown("hi")]))
        assert read_notebook(workspace, "SHOUTED.IPYNB").cells[0].cell_type == "markdown"

    def test_a_missing_notebook_is_a_missing_file(self, workspace: Workspace) -> None:
        with pytest.raises(FileNotFoundError):
            read_notebook(workspace, "gone.ipynb")

    def test_a_directory_named_like_a_notebook_is_not_a_file(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # Reachable from the tree on any OS, and on Windows opening it raises
        # PermissionError rather than IsADirectoryError — hence the explicit ask.
        (tmp_path / "folder.ipynb").mkdir()
        with pytest.raises(IsADirectoryError):
            read_notebook(workspace, "folder.ipynb")

    def test_json_that_is_not_a_notebook_fails_with_a_reason(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        _write(tmp_path, "config.ipynb", {"hello": "world"})
        with pytest.raises(NotebookUnreadableError) as caught:
            read_notebook(workspace, "config.ipynb")
        # The reason is what the degraded card shows; an empty one is a blank pane.
        assert str(caught.value).strip() != ""

    def test_a_truncated_file_fails_with_a_reason(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        _write(tmp_path, "cut.ipynb", '{"cells": [{"cell_type": "code",')
        with pytest.raises(NotebookUnreadableError):
            read_notebook(workspace, "cut.ipynb")

    def test_an_empty_file_fails_with_a_reason(self, tmp_path: Path, workspace: Workspace) -> None:
        (tmp_path / "empty.ipynb").write_text("", encoding="utf-8")
        with pytest.raises(NotebookUnreadableError):
            read_notebook(workspace, "empty.ipynb")

    def test_a_notebook_that_reads_but_does_not_validate_still_renders(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # The reason `validate` is asked *after* `reads` and is not fatal: a
        # hand-edited notebook is still the user's work.
        broken = _notebook([_code("x = 1", 1)])
        broken["cells"][0]["execution_count"] = "not a number"
        _write(tmp_path, "hand-edited.ipynb", broken)
        doc = read_notebook(workspace, "hand-edited.ipynb")
        assert not doc.valid
        assert doc.validation_message is not None
        # One line, not the paragraph jsonschema renders the whole instance into.
        assert "\n" not in doc.validation_message
        assert doc.cells[0].source == "x = 1"


class TestFormatVersions:
    def test_a_45_notebook_with_cell_ids_validates_and_still_gets_our_ids(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        # 4.5 is what Jupyter writes today. Its cell ids are the file's own; the
        # response's are positional, because nothing addresses a cell across
        # requests and an id that survives an edit would be a promise this
        # read-only view cannot keep.
        document = _notebook(
            [
                {**_markdown("intro"), "id": "abc12345"},
                {**_code("x = 1", 1), "id": "def67890"},
            ]
        )
        document["nbformat_minor"] = 5
        _write(tmp_path, "modern.ipynb", document)
        doc = read_notebook(workspace, "modern.ipynb")
        assert doc.valid and doc.validation_message is None
        assert [cell.id for cell in doc.cells] == ["c0", "c1"]


class TestOlderFormats:
    def test_an_nbformat_3_notebook_is_migrated_rather_than_called_corrupt(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        """The single strongest argument for the nbformat dependency.

        A notebook from 2015 has worksheets, `input` instead of `source`, and
        `pyout` instead of `execute_result`. `json.load` would parse it and find
        no `cells` at all; `nbformat.reads(as_version=4)` migrates it.
        """
        _write(
            tmp_path,
            "legacy.ipynb",
            {
                "metadata": {"name": "old", "language_info": {"name": "python"}},
                "nbformat": 3,
                "nbformat_minor": 0,
                "worksheets": [
                    {
                        "cells": [
                            {"cell_type": "heading", "level": 1, "source": ["Old"]},
                            {
                                "cell_type": "code",
                                "collapsed": False,
                                "input": ["print('hello')"],
                                "language": "python",
                                "prompt_number": 1,
                                "outputs": [
                                    {
                                        "output_type": "stream",
                                        "stream": "stdout",
                                        "text": ["hello\n"],
                                    }
                                ],
                            },
                        ],
                        "metadata": {},
                    }
                ],
            },
        )
        doc = read_notebook(workspace, "legacy.ipynb")
        assert doc.nbformat == 4
        sources = [cell.source for cell in doc.cells]
        assert any("print('hello')" in source for source in sources)
        assert any(output.text == "hello\n" for cell in doc.cells for output in cell.outputs)


# ---- the route ----------------------------------------------------------------


class TestRoute:
    async def test_reads_a_notebook_the_app_just_created(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        # Through the app's own create path, so this covers the round trip a
        # user makes: New document -> Notebook -> open it.
        created = await client.post(
            "/api/files/document", json={"path": "fresh.ipynb", "kind": "ipynb"}
        )
        assert created.status_code == 200

        response = await client.get("/api/notebook", params={"path": "fresh.ipynb"})
        assert response.status_code == 200
        doc = NotebookDocument.model_validate(response.json())
        assert doc.path == "fresh.ipynb"
        assert doc.valid
        assert doc.nbformat == 4

    async def test_a_notebook_in_a_subfolder_is_addressed_with_forward_slashes(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        # Wire paths are POSIX on all three platforms; `pathlib` does the rest.
        _write(tmp_path, "deep/nested/nb.ipynb", _notebook([_markdown("nested")]))
        response = await client.get("/api/notebook", params={"path": "deep/nested/nb.ipynb"})
        assert response.status_code == 200
        assert response.json()["cells"][0]["source"] == "nested"

    @pytest.mark.parametrize(
        ("path", "content", "status"),
        [
            ("../escape.ipynb", None, 400),
            ("notes.md", "# not a notebook", 400),
            ("missing.ipynb", None, 404),
            ("bad.ipynb", "{not json", 422),
        ],
    )
    async def test_refusals_map_to_their_own_status(
        self, client: AsyncClient, tmp_path: Path, path: str, content: str | None, status: int
    ) -> None:
        if content is not None:
            _write(tmp_path, path, content)
        response = await client.get("/api/notebook", params={"path": path})
        assert response.status_code == status
        # Every refusal carries a reason — the degraded card renders it verbatim.
        assert response.json()["detail"].strip() != ""

    async def test_the_unreadable_reason_reaches_the_client(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        _write(tmp_path, "config.ipynb", {"not": "a notebook"})
        response = await client.get("/api/notebook", params={"path": "config.ipynb"})
        assert response.status_code == 422
        assert response.json()["detail"].startswith("not a readable notebook:")
