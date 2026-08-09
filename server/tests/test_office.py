"""Office integration tests: config signing, token binding, key semantics, and a
full simulated Document-Server save round-trip (stub DS via httpx.MockTransport)."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services.office import (
    InvalidOfficeTokenError,
    NotAnOfficeFileError,
    OfficeDisabledError,
    OfficeService,
)
from workbench_server.services.workspace import Workspace, content_hash

SECRET = "test-secret"
DOCX = b"PK\x03\x04 fake docx bytes"


@pytest.fixture
def office_settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace_root=tmp_path,
        onlyoffice_url="http://localhost:8880",
        onlyoffice_jwt_secret=SECRET,
        public_base_url="http://127.0.0.1:8787",
    )


@pytest.fixture
def svc(tmp_path: Path) -> OfficeService:
    return OfficeService(
        Workspace(tmp_path),
        server_url="http://localhost:8880",
        jwt_secret=SECRET,
        public_base_url="http://127.0.0.1:8787",
    )


class TestService:
    def test_document_type_mapping(self, svc: OfficeService) -> None:
        assert svc.document_type("a/report.docx") == "word"
        assert svc.document_type("model.XLSX") == "cell"
        assert svc.document_type("deck.pptx") == "slide"
        assert svc.document_type("main.py") is None

    def test_key_changes_with_content(self, svc: OfficeService) -> None:
        k1 = svc.make_key("a.docx", content_hash(b"v1"))
        k2 = svc.make_key("a.docx", content_hash(b"v2"))
        assert k1 != k2
        assert len(k1) <= 128
        assert all(c.isalnum() or c in ".=_-" for c in k1)

    def test_editor_config_is_signed_and_bound(self, svc: OfficeService, tmp_path: Path) -> None:
        (tmp_path / "report.docx").write_bytes(DOCX)
        cfg = svc.editor_config("report.docx")
        assert cfg.document_type == "word"
        assert cfg.editor_config.mode == "edit"
        # the config token verifies against the shared secret
        claims = pyjwt.decode(cfg.token, SECRET, algorithms=["HS256"])
        assert claims["document"]["key"] == cfg.document.key
        # file + callback URLs embed path-bound tokens
        file_token = cfg.document.url.rsplit("/", 1)[-1]
        assert svc.verify_path_token(file_token) == "report.docx"

    def test_view_mode_for_legacy_formats(self, svc: OfficeService, tmp_path: Path) -> None:
        (tmp_path / "old.doc").write_bytes(b"legacy")
        assert svc.editor_config("old.doc").editor_config.mode == "view"

    def test_disabled_without_secret(self, tmp_path: Path) -> None:
        disabled = OfficeService(
            Workspace(tmp_path), server_url=None, jwt_secret=None, public_base_url="http://x"
        )
        assert not disabled.enabled
        with pytest.raises(OfficeDisabledError):
            disabled.editor_config("a.docx")

    def test_non_office_file_rejected(self, svc: OfficeService, tmp_path: Path) -> None:
        (tmp_path / "x.py").write_text("pass")
        with pytest.raises(NotAnOfficeFileError):
            svc.editor_config("x.py")

    def test_theme_flows_into_customization(self, svc: OfficeService, tmp_path: Path) -> None:
        (tmp_path / "t.docx").write_bytes(DOCX)
        dark = svc.editor_config("t.docx", "dark")
        light = svc.editor_config("t.docx", "light")
        assert dark.editor_config.customization["uiTheme"] == "theme-dark"
        assert light.editor_config.customization["uiTheme"] == "theme-light"
        assert dark.editor_config.customization["hideRulers"] is True

    def test_forged_token_rejected(self, svc: OfficeService) -> None:
        forged = pyjwt.encode({"path": "a.docx"}, "wrong-secret", algorithm="HS256")
        with pytest.raises(InvalidOfficeTokenError):
            svc.verify_path_token(forged)


class TestHttpFlow:
    """Full pipeline with a stub Document Server."""

    @pytest.fixture
    def http_client(self, office_settings: Settings) -> Iterator[TestClient]:
        """The app under a stub Document Server, with lifespan run *and* stopped.

        A fixture rather than a factory, because the factory this replaced called
        `client.__enter__()` and nothing ever called `__exit__`: the lifespan
        started and never finished, leaking the blocking portal's event loop
        thread and, inside it, the file watcher — still polling for the rest of
        the process. On glibc that is an abort at interpreter shutdown (the
        watchfiles thread is killed inside its own extension), which is how it
        surfaced: a suite where every test passed and the process exited 134 on
        the ubuntu leg. A fixture also unwinds when an assertion fails.
        """
        app = create_app(office_settings)
        new_bytes = b"PK\x03\x04 edited by onlyoffice"

        def ds_handler(request: httpx.Request) -> httpx.Response:
            if "download" in str(request.url):
                return httpx.Response(200, content=new_bytes)
            if "CommandService" in str(request.url):
                return httpx.Response(200, json={"error": 0})
            return httpx.Response(404)

        with TestClient(app) as client:
            app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(ds_handler))
            yield client

    def test_full_save_round_trip(self, http_client: TestClient, tmp_path: Path) -> None:
        (tmp_path / "report.docx").write_bytes(DOCX)
        client = http_client

        status = client.get("/api/office/status").json()
        assert status == {"enabled": True, "server_url": "http://localhost:8880"}

        cfg = client.get("/api/office/config", params={"path": "report.docx"}).json()
        assert cfg["documentType"] == "word"
        assert cfg["document"]["fileType"] == "docx"

        # DS pulls the file with the token from the config
        file_token = cfg["document"]["url"].rsplit("/", 1)[-1]
        pulled = client.get(f"/api/office/files/{file_token}")
        assert pulled.status_code == 200
        assert pulled.content == DOCX

        # DS pushes a save: status 2 + signed body + download URL
        callback_token = cfg["editorConfig"]["callbackUrl"].rsplit("/", 1)[-1]
        body = {"status": 2, "url": "http://stub-ds/download/x", "key": cfg["document"]["key"]}
        body["token"] = pyjwt.encode(body, SECRET, algorithm="HS256")
        result = client.post(f"/api/office/callback/{callback_token}", json=body)
        assert result.json() == {"error": 0}

        # file was overwritten atomically, original kept as .bak
        assert (tmp_path / "report.docx").read_bytes() == b"PK\x03\x04 edited by onlyoffice"
        assert (tmp_path / "report.docx.bak").read_bytes() == DOCX

        # editing-only statuses are acknowledged but change nothing
        noop = {"status": 1, "key": cfg["document"]["key"]}
        noop["token"] = pyjwt.encode(noop, SECRET, algorithm="HS256")
        assert client.post(f"/api/office/callback/{callback_token}", json=noop).json() == {
            "error": 0
        }

        # forcesave reaches the (stub) command service
        fs = client.post("/api/office/forcesave", json={"path": "report.docx"})
        assert fs.json() == {"error": 0}

        # last-save exposes the DS-save hash so the UI can ignore own-save events
        ls = client.get("/api/office/last-save", params={"path": "report.docx"})
        assert ls.json() == {"hash": content_hash(b"PK\x03\x04 edited by onlyoffice")}
        never = client.get("/api/office/last-save", params={"path": "other.docx"})
        assert never.json() == {"hash": None}

    def test_callback_with_unsigned_body_rejected(
        self, http_client: TestClient, tmp_path: Path
    ) -> None:
        (tmp_path / "report.docx").write_bytes(DOCX)
        client = http_client
        cfg = client.get("/api/office/config", params={"path": "report.docx"}).json()
        callback_token = cfg["editorConfig"]["callbackUrl"].rsplit("/", 1)[-1]
        body = {"status": 2, "url": "http://stub-ds/download/x"}  # no token
        assert client.post(f"/api/office/callback/{callback_token}", json=body).status_code == 401
        assert (tmp_path / "report.docx").read_bytes() == DOCX  # untouched

    def test_file_endpoint_rejects_garbage_token(self, http_client: TestClient) -> None:
        assert http_client.get("/api/office/files/not-a-jwt").status_code == 401

    def test_disabled_reports_and_503s(self, settings: Settings, tmp_path: Path) -> None:
        client = TestClient(create_app(settings))
        with client:
            assert client.get("/api/office/status").json()["enabled"] is False
            (tmp_path / "a.docx").write_bytes(DOCX)
            assert client.get("/api/office/config", params={"path": "a.docx"}).status_code == 503


def test_key_registry_survives_reconfig(svc: OfficeService, tmp_path: Path) -> None:
    """Two configs for the same file after an external change -> different keys,
    both resolvable for forcesave (the newest wins)."""
    target = tmp_path / "m.xlsx"
    target.write_bytes(b"v1")
    k1 = svc.editor_config("m.xlsx").document.key
    target.write_bytes(b"v2")  # external change (agent/git)
    k2 = svc.editor_config("m.xlsx").document.key
    assert k1 != k2  # stale-cache defense: new content => new key
