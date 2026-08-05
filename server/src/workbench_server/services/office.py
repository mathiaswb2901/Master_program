"""OnlyOffice Document Server integration.

Flow: the UI asks for an editor config; the Document Server pulls the file from
our ``/api/office/files/{token}`` URL, and pushes saves to
``/api/office/callback/{token}``. Both URLs carry a JWT binding them to one
workspace path, so the endpoints are useless for anything else. The
``document.key`` derives from the content hash: any change made outside the
editor (agent, git, another program) produces a new key, which forces a clean
reopen instead of OnlyOffice serving its stale cached copy.
"""

import hashlib
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt
import structlog

from workbench_server.models.office import (
    EDITABLE_EXTENSIONS,
    EXTENSION_TYPES,
    DocEditorConfig,
    DocumentType,
    OfficeDocument,
    OfficeEditorConfig,
    UiTheme,
    default_customization,
)
from workbench_server.services.workspace import Workspace, content_hash

log = structlog.get_logger()

_FILE_TOKEN_TTL_S = 15 * 60
_CALLBACK_TOKEN_TTL_S = 48 * 3600
_ALGO = "HS256"

# Callback statuses (OnlyOffice API): 2 = ready for saving, 6 = forcesave result.
_SAVE_STATUSES = {2, 6}


class OfficeDisabledError(Exception):
    pass


class InvalidOfficeTokenError(Exception):
    pass


class NotAnOfficeFileError(Exception):
    pass


class UserWriteObserver(Protocol):
    """The one thing this service tells the provenance correlator: a save that
    landed here is the *user's* keystrokes, flushed by the Document Server —
    never the agent's, even when an agent wrote the same document seconds ago."""

    def note_user_write(self, relative_path: str) -> None: ...


class OfficeService:
    def __init__(
        self,
        workspace: Workspace,
        server_url: str | None,
        jwt_secret: str | None,
        public_base_url: str,
        backup_originals: bool = True,
        provenance: UserWriteObserver | None = None,
    ) -> None:
        self._workspace = workspace
        self._provenance = provenance
        self.server_url = server_url.rstrip("/") if server_url else None
        self._secret = jwt_secret
        self._public_base = public_base_url.rstrip("/")
        self._backup = backup_originals
        # document.key -> workspace path, for the forcesave command service call
        self._keys: dict[str, str] = {}
        # path -> hash of the last bytes a Document Server save wrote; lets the
        # UI distinguish the editor's own saves from external (agent/git) edits
        self._last_saves: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self.server_url is not None and self._secret is not None

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    def document_type(path: str) -> DocumentType | None:
        return EXTENSION_TYPES.get(Path(path).suffix.lower())

    def make_key(self, path: str, digest: str) -> str:
        # allowed charset [0-9a-zA-Z.=_-], max 128 chars; hex is safely inside both
        return hashlib.sha256(f"{path}:{digest}".encode()).hexdigest()[:40]

    def _sign(self, claims: dict[str, Any], ttl_s: int) -> str:
        if self._secret is None:
            raise OfficeDisabledError
        return jwt.encode(
            {**claims, "exp": int(time.time()) + ttl_s}, self._secret, algorithm=_ALGO
        )

    def verify_path_token(self, token: str) -> str:
        """Returns the workspace path a file/callback token was minted for."""
        if self._secret is None:
            raise OfficeDisabledError
        try:
            claims = jwt.decode(token, self._secret, algorithms=[_ALGO])
        except jwt.PyJWTError as e:
            raise InvalidOfficeTokenError(str(e)) from e
        path = claims.get("path")
        if not isinstance(path, str):
            raise InvalidOfficeTokenError("missing path claim")
        return path

    def sign_config(self, payload: dict[str, Any]) -> str:
        """OnlyOffice validates the editor config as a JWT of the config itself."""
        if self._secret is None:
            raise OfficeDisabledError
        return jwt.encode(payload, self._secret, algorithm=_ALGO)

    def verify_body_token(self, body: dict[str, Any]) -> None:
        """Callback bodies carry their own JWT in the ``token`` field."""
        if self._secret is None:
            raise OfficeDisabledError
        token = body.get("token")
        if not isinstance(token, str):
            raise InvalidOfficeTokenError("callback body has no token")
        try:
            jwt.decode(token, self._secret, algorithms=[_ALGO])
        except jwt.PyJWTError as e:
            raise InvalidOfficeTokenError(str(e)) from e

    # ---- editor config ------------------------------------------------------

    def editor_config(self, path: str, theme: UiTheme = "dark") -> DocEditorConfig:
        if not self.enabled:
            raise OfficeDisabledError
        doc_type = self.document_type(path)
        if doc_type is None:
            raise NotAnOfficeFileError(path)
        file_path = self._workspace.safe_path(path)
        digest = content_hash(file_path.read_bytes())
        key = self.make_key(path, digest)
        self._keys[key] = path

        extension = file_path.suffix.lower()
        editable = extension in EDITABLE_EXTENSIONS
        file_token = self._sign({"path": path, "use": "file"}, _FILE_TOKEN_TTL_S)
        callback_token = self._sign({"path": path, "use": "callback"}, _CALLBACK_TOKEN_TTL_S)

        config = DocEditorConfig(
            document=OfficeDocument(
                file_type=extension.lstrip("."),
                key=key,
                title=file_path.name,
                url=f"{self._public_base}/api/office/files/{file_token}",
            ),
            document_type=doc_type,
            editor_config=OfficeEditorConfig(
                callback_url=f"{self._public_base}/api/office/callback/{callback_token}",
                mode="edit" if editable else "view",
                customization=default_customization(theme),
            ),
        )
        payload = config.model_dump(by_alias=True, exclude={"token"})
        config.token = self.sign_config(payload)
        return config

    # ---- saving -------------------------------------------------------------

    async def handle_callback(
        self, path: str, body: dict[str, Any], http: httpx.AsyncClient
    ) -> None:
        """Process a Document Server callback for ``path``. Raises on hard errors;
        non-save statuses (editing, closed-no-changes) are no-ops."""
        self.verify_body_token(body)
        status = body.get("status")
        if status not in _SAVE_STATUSES:
            log.debug("office.callback_ignored", path=path, status=status)
            return
        url = body.get("url")
        if not isinstance(url, str):
            raise InvalidOfficeTokenError("save callback without download url")
        response = await http.get(url)
        response.raise_for_status()
        self._workspace.write_bytes(path, response.content, backup=self._backup)
        self._last_saves[path] = content_hash(response.content)
        # These bytes are the user's own edits. Without this the watcher event
        # would be matched against any agent claim still open on the document —
        # and a .docx is the one file the user cannot diff for themselves.
        if self._provenance is not None:
            self._provenance.note_user_write(path)
        log.info("office.saved", path=path, status=status, size=len(response.content))

    def last_save_hash(self, path: str) -> str | None:
        return self._last_saves.get(path)

    # ---- forcesave ----------------------------------------------------------

    async def forcesave(self, path: str, http: httpx.AsyncClient) -> bool:
        """Ask the Document Server to flush pending edits for ``path`` now."""
        if not self.enabled:
            raise OfficeDisabledError
        keys = [k for k, p in self._keys.items() if p == path]
        if not keys:
            return False
        key = keys[-1]  # most recently issued key for this path
        command = {"c": "forcesave", "key": key}
        command["token"] = self.sign_config(command)
        response = await http.post(
            f"{self.server_url}/coauthoring/CommandService.ashx", json=command
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        # error 0 = ok, 4 = no changes to save — both fine from the caller's view
        return result.get("error") in (0, 4)
