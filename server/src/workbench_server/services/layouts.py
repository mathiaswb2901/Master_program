"""Layout persistence: one JSON document per workspace, read and written whole.

Deliberately dumb. The document's *contents* are dockview's business and its
*validity* is the tool registry's (``ui/src/layouts.ts`` prunes a restored layout
against the components the app can actually render). What this service owns is
the file: where it lives, that a write is atomic, that a read never raises, and
that neither side can grow without bound.

Nothing here ever raises on a bad file. A corrupt or stale ``layouts.json`` must
cost the user their arrangement and nothing else — never a blank window, never a
500 on startup — so every failure resolves to "empty state + a sentence saying
why", which the UI turns into one toast.
"""

import json
import os
import tempfile
from pathlib import Path

import structlog
from pydantic import ValidationError

from workbench_server.models.layouts import (
    MAX_FILE_BYTES,
    LayoutsResponse,
    LayoutsState,
)

log = structlog.get_logger()

LAYOUTS_PATH = ".workbench/layouts.json"


class LayoutTooLargeError(Exception):
    """The document the client asked to persist is over ``MAX_FILE_BYTES``."""


class LayoutsService:
    """Loads and stores ``<workspace>/.workbench/layouts.json``."""

    def __init__(self, workspace_root: Path) -> None:
        self._path = workspace_root.resolve() / Path(LAYOUTS_PATH)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> LayoutsResponse:
        """The persisted document, or the empty one plus the reason it is empty."""
        try:
            if self._path.stat().st_size > MAX_FILE_BYTES:
                return self._empty(f"larger than {MAX_FILE_BYTES // 1024} KB — ignored")
            # utf-8-sig, not utf-8: this is a file a user may well open in
            # Notepad or rewrite with PowerShell's `Set-Content -Encoding utf8`,
            # both of which prepend a BOM that `json.loads` refuses. Losing an
            # arrangement to three invisible bytes is not a fallback anyone can
            # act on. (Observed on Windows while testing this by hand.)
            raw = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return LayoutsResponse(state=LayoutsState())  # nothing saved yet
        except OSError as err:
            return self._empty(f"unreadable: {err.strerror or err}")
        try:
            parsed = json.loads(raw)
        except ValueError as err:
            return self._empty(f"not valid JSON ({err.args[0] if err.args else err})")
        try:
            return LayoutsResponse(state=LayoutsState.model_validate(parsed))
        except ValidationError:
            # Written by an older (or newer) Workbench, or edited by hand into a
            # shape this version cannot use. The default layout is the answer.
            return self._empty("not a layouts document this version understands")

    def save(self, state: LayoutsState) -> None:
        """Persist the document atomically (tmp + replace, like every other write)."""
        data = state.model_dump_json().encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise LayoutTooLargeError(f"{len(data)} bytes")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _empty(self, reason: str) -> LayoutsResponse:
        log.warning("layouts.unusable", path=str(self._path), reason=reason)
        return LayoutsResponse(state=LayoutsState(), problem=f"{LAYOUTS_PATH}: {reason}")
