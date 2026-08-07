"""The named-session store on disk — CRUD over ``<app data>/sessions.json``.

This is the persistence half of detachable working sessions (M5 item 15, PR2):
the store and its discipline, no UI. It copies ``services/workspaces.py``'s
:class:`~workbench_server.services.workspaces.RecentsStore` almost line for line,
because the two files answer the same question about the same kind of data — a
list about the *user* rather than about a project — and the discipline is the
point:

* **Version-stamped.** A document from a version this code does not understand is
  discarded, not guessed at (:data:`SESSIONS_VERSION`).
* **Never raises on read.** A corrupt, truncated, oversized, wrong-version or
  non-JSON file costs the list and *only* the list — never a server that will not
  start. Every failure resolves to "empty, plus a sentence saying why".
* **Atomic writes, retried past a transient Windows lock.** ``tmp`` then
  ``os.replace``, and the replace is retried the way ``services/layouts.py``
  retries it: on Windows a replace onto a path another process momentarily holds
  open (the search indexer, Defender) fails outright instead of waiting, and a
  short bounded retry turns a *lost* write into a marginally slower one.
* **utf-8-sig read**, so a file a curious user opened in Notepad (which prepends
  a BOM) still parses.

**Global, not re-rooted.** Unlike the layouts file, this store is *not* handed to
``WorkspaceService`` and owns no ``set_workspace_root``: it lives once under the
machine's app data dir and is queried *by* workspace. One project can host more
than one named session, and a session is a thing you return to from anywhere —
so switching the current workspace must not change which sessions exist, only
which ones a given window asks to see. That is asserted rather than described
(``test_sessions.py``).
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import structlog

from workbench_server.models.sessions import (
    MAX_FILE_BYTES,
    MAX_NAMED_SESSIONS,
    SESSIONS_VERSION,
    CreateNamedSessionRequest,
    NamedSession,
    SessionsFile,
    UpdateNamedSessionRequest,
)
from workbench_server.services.app_data import app_data_dir

log = structlog.get_logger()

SESSIONS_FILE = "sessions.json"

# Windows: `os.replace` onto a path another process momentarily holds open fails
# with PermissionError instead of waiting. Same holder classes and same fix as
# `services/layouts.py` — a short bounded retry turns a lost write into a slower
# one. A lock that outlasts the budget is a real one and the last attempt raises.
REPLACE_ATTEMPTS = 10
REPLACE_BACKOFF_S = 0.02


class SessionNotFoundError(Exception):
    """No named session with that id — update and delete both raise it."""


class SessionsFullError(Exception):
    """The store already holds `MAX_NAMED_SESSIONS`; a create would overflow it."""


class SessionTooLargeError(Exception):
    """The document a create/update would write is over ``MAX_FILE_BYTES``."""


class SessionsStore:
    """The named-session list on disk. Never raises on read — see the module note.

    Losing this file costs the user a list they can rebuild; it must never cost
    them a server that will not start, so every read failure resolves to "empty,
    plus a sentence saying why". Writes *do* raise on a real, non-transient
    failure (a full store, an oversized document, a lock that never clears), so
    the router can turn each into the status the client can act on.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._path = (directory or app_data_dir()) / SESSIONS_FILE
        self._problem: str | None = None
        self._sessions: list[NamedSession] = []
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def problem(self) -> str | None:
        self._ensure_loaded()
        return self._problem

    def entries(self, workspace: str | None = None) -> list[NamedSession]:
        """Most recently attached first, optionally only for one workspace.

        ``workspace`` is matched case-foldedly for the same reason the recents
        list dedupes that way: Windows paths are case-insensitive, and a window
        rooted at ``C:\\Work`` must see a session created when it was spelled
        ``c:\\work``. ``None`` returns every session across every project — the
        query a global session list makes.
        """
        self._ensure_loaded()
        rows = sorted(self._sessions, key=lambda s: s.last_attached_at, reverse=True)
        if workspace is None:
            return rows
        key = self._key(workspace)
        return [s for s in rows if self._key(s.workspace) == key]

    def create(self, request: CreateNamedSessionRequest) -> NamedSession:
        """Mint a session, assigning the fields the server owns, and persist it."""
        self._ensure_loaded()
        if len(self._sessions) >= MAX_NAMED_SESSIONS:
            raise SessionsFullError(
                f"the session store is full ({MAX_NAMED_SESSIONS}); delete one to make room"
            )
        now = time.time()
        session = NamedSession(
            id=uuid.uuid4().hex,
            name=request.name,
            workspace=request.workspace,
            arrangement=request.arrangement,
            agents=request.agents,
            leases=request.leases,
            created_at=now,
            last_attached_at=now,
        )
        self._sessions = [session, *self._sessions]
        self._save()
        return session

    def update(self, session_id: str, request: UpdateNamedSessionRequest) -> NamedSession:
        """Replace a session's mutable fields whole; stamp it as just attached.

        ``id`` and ``created_at`` are carried over from the stored session — the
        client cannot forge identity or rewrite when the session began. An update
        *is* an attach, so ``last_attached_at`` moves to now.
        """
        self._ensure_loaded()
        index = self._index_of(session_id)
        existing = self._sessions[index]
        updated = NamedSession(
            id=existing.id,
            name=request.name,
            workspace=request.workspace,
            arrangement=request.arrangement,
            agents=request.agents,
            leases=request.leases,
            created_at=existing.created_at,
            last_attached_at=time.time(),
        )
        self._sessions = [*self._sessions[:index], updated, *self._sessions[index + 1 :]]
        self._save()
        return updated

    def delete(self, session_id: str) -> None:
        """Forget a session. Raises if it was never there — a delete that
        silently no-ops would hide a client pointing at the wrong id."""
        self._ensure_loaded()
        index = self._index_of(session_id)
        self._sessions = [*self._sessions[:index], *self._sessions[index + 1 :]]
        self._save()

    # ---- internals ----------------------------------------------------------

    @staticmethod
    def _key(workspace: str) -> str:
        """Identity of a workspace path — case-folded, because Windows paths are."""
        return os.path.normcase(workspace)

    def _index_of(self, session_id: str) -> int:
        for index, session in enumerate(self._sessions):
            if session.id == session_id:
                return index
        raise SessionNotFoundError(session_id)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._sessions, self._problem = self._load()

    def _load(self) -> tuple[list[NamedSession], str | None]:
        try:
            if self._path.stat().st_size > MAX_FILE_BYTES:
                return [], f"{SESSIONS_FILE}: larger than a sessions file can be — ignored"
            # utf-8-sig for the same reason the neighbours use it: a small JSON
            # file a curious user may open in Notepad, which prepends a BOM.
            raw = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return [], None  # nothing recorded yet is not a problem
        except OSError as err:
            return [], f"{SESSIONS_FILE}: unreadable ({err.strerror or err})"
        try:
            document = SessionsFile.model_validate(json.loads(raw))
        except (ValueError, TypeError) as err:
            return [], f"{SESSIONS_FILE}: not a sessions document ({err.__class__.__name__})"
        if document.version != SESSIONS_VERSION:
            return [], f"{SESSIONS_FILE}: written by another version of Workbench — ignored"
        return document.sessions[:MAX_NAMED_SESSIONS], None

    def _save(self) -> None:
        document = SessionsFile(version=SESSIONS_VERSION, sessions=self._sessions)
        data = document.model_dump_json().encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            # Refuse before touching disk: a caller sent an arrangement blob too
            # large to persist, which is a 4xx, not a corrupt half-written file.
            raise SessionTooLargeError(f"{len(data)} bytes")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            self._replace(tmp_name)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        # A successful write heals the reported problem: the file on disk is now a
        # fresh, valid document, so a corrupt/oversized/wrong-version read that
        # poisoned `problem` on first load must not keep being reported once the
        # store — a process-lived singleton in `main.py` — has rewritten it clean.
        self._problem = None

    def _replace(self, tmp_name: str) -> None:
        """`os.replace`, retried past a transient Windows lock — see the constants."""
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_name, self._path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                log.debug("sessions.replace_retry", path=str(self._path), attempt=attempt + 1)
                time.sleep(REPLACE_BACKOFF_S)
