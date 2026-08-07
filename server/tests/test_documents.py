"""New-document creation (M5 item 16): every kind opens without a repair prompt.

"Opens without a repair prompt" is proven the strongest way that needs no Office
install: the OOXML kinds are real zips whose integrity checks out, whose required
parts are present, and whose every XML/rels part is well-formed; the notebook
parses as JSON under the nbformat-v4 skeleton rules; the empty kinds are empty
files with the right extension. The API create -> open round trip is exercised
end to end against the real app.
"""

import json
import zipfile
from io import BytesIO
from pathlib import Path
from xml.dom.minidom import parseString

import pytest
from httpx import AsyncClient

from workbench_server.models.documents import DocumentKind
from workbench_server.services.documents import (
    DocumentExtensionError,
    DocumentService,
    TemplateUnavailableError,
)
from workbench_server.services.workspace import Workspace

# Required parts per OOXML kind: the package content-types map, the package
# relationships, and the format's main part. A consumer that cannot find these
# is the "repair this file" prompt.
_REQUIRED_PARTS: dict[str, list[str]] = {
    "docx": ["[Content_Types].xml", "_rels/.rels", "word/document.xml"],
    "xlsx": ["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/worksheets/sheet1.xml"],
    "pptx": [
        "[Content_Types].xml",
        "_rels/.rels",
        "ppt/presentation.xml",
        "ppt/slideMasters/slideMaster1.xml",
        "ppt/slideLayouts/slideLayout1.xml",
        "ppt/slides/slide1.xml",
        "ppt/theme/theme1.xml",
    ],
}

TEMPLATE_KINDS: list[DocumentKind] = ["docx", "xlsx", "pptx", "ipynb"]
EMPTY_KINDS: list[DocumentKind] = ["py", "txt", "md"]
ALL_KINDS: list[DocumentKind] = [*TEMPLATE_KINDS, *EMPTY_KINDS]


def _assert_valid_ooxml(data: bytes, kind: str) -> None:
    with zipfile.ZipFile(BytesIO(data)) as z:
        assert z.testzip() is None, "corrupt zip member"
        names = set(z.namelist())
        for part in _REQUIRED_PARTS[kind]:
            assert part in names, f"{kind} missing {part}"
        # Every XML/rels part must be well-formed, or the app has shipped a
        # document that opens to a repair prompt.
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                # The bytes are our own bundled template, not untrusted input —
                # this asserts they are well-formed, so a real parser is wanted.
                parseString(z.read(name))  # noqa: S318


def _assert_valid_ipynb(data: bytes) -> None:
    nb = json.loads(data.decode("utf-8"))
    # nbformat v4 skeleton: the two version integers a reader keys on, a cells
    # list (empty is legal) and a metadata object.
    assert nb["nbformat"] == 4
    assert isinstance(nb["nbformat_minor"], int)
    assert nb["cells"] == []
    assert isinstance(nb["metadata"], dict)


class TestTemplatesShip:
    """The bundled templates are present and valid before any API is involved."""

    def test_service_finds_templates(self) -> None:
        assert DocumentService()._templates is not None

    @pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx"])
    def test_blank_bytes_are_valid_ooxml(self, kind: str) -> None:
        _assert_valid_ooxml(DocumentService().blank_bytes(kind), kind)  # type: ignore[arg-type]

    def test_blank_ipynb_is_valid(self) -> None:
        _assert_valid_ipynb(DocumentService().blank_bytes("ipynb"))

    @pytest.mark.parametrize("kind", EMPTY_KINDS)
    def test_empty_kinds_are_empty(self, kind: DocumentKind) -> None:
        assert DocumentService().blank_bytes(kind) == b""


class TestServiceWrite:
    """The service writes through the jail and never overwrites."""

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_creates_file_on_disk(self, tmp_path: Path, kind: DocumentKind) -> None:
        ws = Workspace(tmp_path)
        digest = DocumentService().create(ws, f"new.{kind}", kind)
        written = (tmp_path / f"new.{kind}").read_bytes()
        assert len(digest) == 64
        if kind in _REQUIRED_PARTS:
            _assert_valid_ooxml(written, kind)
        elif kind == "ipynb":
            _assert_valid_ipynb(written)
        else:
            assert written == b""

    def test_extension_must_match_kind(self, tmp_path: Path) -> None:
        with pytest.raises(DocumentExtensionError):
            DocumentService().create(Workspace(tmp_path), "budget.txt", "xlsx")

    def test_never_overwrites(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path)
        DocumentService().create(ws, "notes.md", "md")
        with pytest.raises(FileExistsError):
            DocumentService().create(ws, "notes.md", "md")

    def test_template_unavailable_is_loud(self, tmp_path: Path) -> None:
        service = DocumentService()
        service._templates = None
        # Empty kinds still work with no templates on disk...
        service.create(Workspace(tmp_path), "a.py", "py")
        assert (tmp_path / "a.py").read_bytes() == b""
        # ...but a template kind fails loudly rather than writing a corrupt file.
        with pytest.raises(TemplateUnavailableError):
            service.create(Workspace(tmp_path), "a.docx", "docx")


class TestCreateDocumentApi:
    """POST /api/files/document, end to end through the real app."""

    @pytest.mark.parametrize("kind", TEMPLATE_KINDS)
    async def test_create_ooxml_and_open_round_trip(
        self, client: AsyncClient, tmp_path: Path, kind: DocumentKind
    ) -> None:
        resp = await client.post("/api/files/document", json={"path": f"doc.{kind}", "kind": kind})
        assert resp.status_code == 200, resp.text
        assert resp.json()["path"] == f"doc.{kind}"
        assert len(resp.json()["hash"]) == 64

        written = (tmp_path / f"doc.{kind}").read_bytes()
        if kind == "ipynb":
            _assert_valid_ipynb(written)
            # A notebook is text, so the editor's own open path returns it as JSON.
            opened = await client.get("/api/files/content", params={"path": "doc.ipynb"})
            assert opened.status_code == 200
            _assert_valid_ipynb(opened.json()["content"].encode("utf-8"))
        else:
            _assert_valid_ooxml(written, kind)

    @pytest.mark.parametrize("kind", EMPTY_KINDS)
    async def test_create_empty_and_open_round_trip(
        self, client: AsyncClient, tmp_path: Path, kind: DocumentKind
    ) -> None:
        resp = await client.post(
            "/api/files/document", json={"path": f"src/blank.{kind}", "kind": kind}
        )
        assert resp.status_code == 200, resp.text
        # Nested path was created, and the file is empty with the right extension.
        assert (tmp_path / "src" / f"blank.{kind}").read_bytes() == b""
        opened = await client.get("/api/files/content", params={"path": f"src/blank.{kind}"})
        assert opened.status_code == 200
        assert opened.json()["content"] == ""

    async def test_appears_in_directory_listing(self, client: AsyncClient) -> None:
        await client.post("/api/files/document", json={"path": "report.docx", "kind": "docx"})
        listing = await client.get("/api/files/dir", params={"path": ""})
        names = [e["name"] for e in listing.json()["entries"]]
        assert "report.docx" in names

    async def test_duplicate_is_conflict(self, client: AsyncClient) -> None:
        first = await client.post("/api/files/document", json={"path": "a.docx", "kind": "docx"})
        assert first.status_code == 200
        again = await client.post("/api/files/document", json={"path": "a.docx", "kind": "docx"})
        assert again.status_code == 409

    async def test_jail_escape_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/files/document", json={"path": "../escape.docx", "kind": "docx"}
        )
        assert resp.status_code == 400

    async def test_extension_mismatch_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/api/files/document", json={"path": "a.txt", "kind": "docx"})
        assert resp.status_code == 400

    async def test_unknown_kind_is_422(self, client: AsyncClient) -> None:
        # The DocumentKind Literal guards the wire — an unknown kind never reaches
        # the service.
        resp = await client.post("/api/files/document", json={"path": "a.rtf", "kind": "rtf"})
        assert resp.status_code == 422
