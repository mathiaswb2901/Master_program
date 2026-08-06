"""Every Claude conversation on this machine, grouped by the folder it ran in.

The Claude Code resume list, natively. ``services/session_index.py`` already
reads one folder's transcripts; this walks the *whole* store
(``~/.claude/projects/``), resolves each encoded directory name back to a real
folder where it can, and answers with groups a panel can render.

**Three properties, and each one is a constraint rather than a nicety.**

1. **Read-only.** This is Claude Code's storage, not ours. Nothing here opens a
   transcript for writing, and there is no code path that deletes one.
2. **Cheap enough to open on a whim.** Two passes with very different costs:
   enumeration (`os.scandir` per project directory — 22 directories and 80
   transcripts measured at **5.2 ms** on the author's machine) tells us what
   exists and when it changed; reading a transcript for its title and turn count
   costs a full pass (**1.24 s for all 80, 397 MB** cold) and is therefore
   (a) bounded by ``limit`` — only the newest N conversations are read — and
   (b) cached against each file's ``(mtime_ns, size)``, so a second browse of an
   unchanged store is enumeration only. Transcripts are append-only, which is
   what makes that pair a sound invalidation and not a hope.
3. **Honest about what it cannot know.** ``encode_project_dir`` is lossy and not
   reversible, so a display path is *matched against directories that exist* and
   the raw encoded key is shown when nothing matches — never a guessed path. A
   transcript that will not parse keeps its row and carries the reason. An empty
   store says it is empty, and says where it looked.
"""

import os
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

import structlog

from workbench_server.models.conversations import (
    ConversationInfo,
    ConversationStore,
    ProjectGroup,
)
from workbench_server.services.ignore import is_ignored_dir
from workbench_server.services.session_index import (
    TranscriptFacts,
    encode_project_dir,
    scan_transcript,
)
from workbench_server.services.titles import derive_title

log = structlog.get_logger()

#: Conversations read in full (title + turns) per request, newest first.
#: Everything that exists is still *counted* and reported; this bounds only the
#: expensive pass. 200 covers the author's whole store (80) several times over.
DEFAULT_LIMIT = 200
#: Ceiling on ``?limit=``. The panel's "read them all" raises the window to
#: here; past it the honest answer is that the store is too big for one glance.
MAX_LIMIT = 2000

#: Directories visited while matching one encoded key back to a real folder.
#: The walk prunes to directories whose encoded name is a prefix of the key, so
#: it is normally a handful; the cap is what stops a pathological key (one whose
#: every segment is a common prefix) turning a browse into a filesystem crawl.
MAX_RESOLVE_VISITS = 400

_TITLE_UNREADABLE = "(unreadable transcript)"


class _CachedFacts:
    """One transcript's facts, and the file identity they were read from."""

    __slots__ = ("facts", "mtime_ns", "size")

    def __init__(self, mtime_ns: int, size: int, facts: TranscriptFacts) -> None:
        self.mtime_ns = mtime_ns
        self.size = size
        self.facts = facts

    def matches(self, mtime_ns: int, size: int) -> bool:
        return self.mtime_ns == mtime_ns and self.size == size


class _Entry:
    """One transcript, as enumeration knows it — before anything is read."""

    __slots__ = ("key", "mtime", "mtime_ns", "path", "session_id", "size")

    def __init__(self, key: str, path: Path, mtime_ns: int, size: int) -> None:
        self.key = key
        self.path = path
        self.session_id = path.stem
        self.mtime_ns = mtime_ns
        self.mtime = mtime_ns / 1e9
        self.size = size


class ConversationBrowser:
    """Reads Claude Code's transcript store; caches against file mtimes.

    Not loop-affine and not async: :meth:`browse` does real disk work, so the
    router hands it to a thread. One lock, held for the whole scan — two browsers
    arriving together share the result of one pass rather than both paying for it.
    """

    def __init__(self, projects_root: Path, workspace_root: Path) -> None:
        self._root = projects_root
        self._workspace = workspace_root
        self._lock = threading.Lock()
        self._facts: dict[Path, _CachedFacts] = {}
        self._resolved: dict[str, Path] = {}

    # ---- the one public entry point ---------------------------------------

    def browse(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        live_by_sdk_id: Mapping[str, str] | None = None,
    ) -> ConversationStore:
        live = live_by_sdk_id or {}
        with self._lock:
            started = time.perf_counter()
            entries = self._enumerate()
            # Newest first, globally: the limit then cuts the *oldest*
            # conversations rather than whichever project happened to sort last.
            entries.sort(key=lambda e: e.mtime, reverse=True)
            taken = entries[: max(limit, 0)]
            self._prune_cache(entries)
            groups = self._group(taken, live)
            store = ConversationStore(
                projects=groups,
                projects_root=str(self._root),
                root_exists=self._root.is_dir(),
                total_projects=len({entry.key for entry in entries}),
                total_conversations=len(entries),
                returned_conversations=len(taken),
                limit=limit,
                scanned_at=time.time(),
            )
            log.debug(
                "conversations.browsed",
                projects=store.total_projects,
                conversations=store.total_conversations,
                read=store.returned_conversations,
                ms=round((time.perf_counter() - started) * 1000, 1),
            )
            return store

    # ---- enumeration (cheap: one scandir per project directory) ------------

    def _enumerate(self) -> list[_Entry]:
        """Every transcript in the store, with the stats ordering needs.

        Only ``*.jsonl`` *files* directly inside a project directory count.
        Claude Code also writes subagent transcripts one level deeper
        (``<session-id>/subagents/agent-<id>.jsonl``); those belong to a
        conversation that is already a row here, and listing them would show a
        user rows for agents they never started.
        """
        entries: list[_Entry] = []
        try:
            with os.scandir(self._root) as projects:
                for project in projects:
                    if not project.is_dir():
                        continue
                    entries.extend(self._enumerate_project(project.name, Path(project.path)))
        except OSError:
            # No store yet (a machine that has never run Claude Code), or one we
            # may not read. Both are "nothing to show", said by root_exists.
            log.debug("conversations.no_store", root=str(self._root))
        return entries

    def _enumerate_project(self, key: str, directory: Path) -> list[_Entry]:
        entries: list[_Entry] = []
        try:
            with os.scandir(directory) as transcripts:
                for transcript in transcripts:
                    if not transcript.name.endswith(".jsonl") or not transcript.is_file():
                        continue
                    stat = transcript.stat()
                    entries.append(
                        _Entry(key, Path(transcript.path), stat.st_mtime_ns, stat.st_size)
                    )
        except OSError:
            log.debug("conversations.unreadable_project", key=key)
        return entries

    def _prune_cache(self, entries: Iterable[_Entry]) -> None:
        """Forget transcripts that are no longer on disk.

        Without this the cache is a leak with the shape of a memory of deleted
        conversations — small, but it is exactly the kind of thing that makes a
        long-running server's memory a mystery.
        """
        alive = {entry.path for entry in entries}
        for path in [path for path in self._facts if path not in alive]:
            del self._facts[path]

    # ---- reading (expensive: one full pass per transcript, cached) ---------

    def _facts_for(self, entry: _Entry) -> TranscriptFacts:
        cached = self._facts.get(entry.path)
        if cached is not None and cached.matches(entry.mtime_ns, entry.size):
            return cached.facts
        facts = scan_transcript(entry.path)
        self._facts[entry.path] = _CachedFacts(entry.mtime_ns, entry.size, facts)
        return facts

    def _row(self, entry: _Entry, live: Mapping[str, str]) -> ConversationInfo:
        facts = self._facts_for(entry)
        title = (
            derive_title(facts.first_user_text)
            if facts.first_user_text is not None
            else _TITLE_UNREADABLE
        )
        return ConversationInfo(
            session_id=entry.session_id,
            title=title,
            updated_at=entry.mtime,
            turns=facts.turns,
            turns_capped=facts.truncated,
            live_session_id=live.get(entry.session_id),
            problem=facts.problem,
        )

    # ---- grouping ----------------------------------------------------------

    def _group(self, entries: Iterable[_Entry], live: Mapping[str, str]) -> list[ProjectGroup]:
        by_key: dict[str, list[_Entry]] = {}
        for entry in entries:
            by_key.setdefault(entry.key, []).append(entry)
        # One listing memo for the whole pass: twenty keys under one home
        # directory would otherwise list it twenty times, which is most of what
        # resolution costs.
        listings: dict[Path, list[Path]] = {}
        groups = [self._project(key, rows, live, listings) for key, rows in by_key.items()]
        # Newest folder first — "what was I doing" order, not alphabetical.
        groups.sort(key=lambda group: group.updated_at, reverse=True)
        return groups

    def _project(
        self,
        key: str,
        entries: list[_Entry],
        live: Mapping[str, str],
        listings: dict[Path, list[Path]],
    ) -> ProjectGroup:
        folder = self._resolve(key, listings)
        relative = None if folder is None else self._workspace_relative(folder)
        conversations = [self._row(entry, live) for entry in entries]
        conversations.sort(key=lambda row: row.updated_at, reverse=True)
        return ProjectGroup(
            key=key,
            label=key if folder is None else str(folder),
            resolved=folder is not None,
            folder=None if folder is None else str(folder),
            workspace_relative=relative,
            openable=relative is not None,
            reason=self._reason(folder, relative),
            updated_at=max((row.updated_at for row in conversations), default=0.0),
            conversations=conversations,
        )

    def _workspace_relative(self, folder: Path) -> str | None:
        """Workspace-relative path, or None when the folder is outside the jail."""
        if folder == self._workspace:
            return ""
        if self._workspace in folder.parents:
            return folder.relative_to(self._workspace).as_posix()
        return None

    @staticmethod
    def _reason(folder: Path | None, relative: str | None) -> str | None:
        """Why a group cannot be opened — shown on the group, never used to hide it.

        Both refusals are half A's boundary drawn honestly. Opening a folder
        outside the workspace needs the multi-root jail (M5 item 5 / item 6),
        and until it lands the conversation is still worth *seeing*: knowing it
        exists is most of what the browser is for.
        """
        if folder is None:
            return (
                "No folder on this machine matches this project — it may have been "
                "moved or deleted. Claude Code's project names are not reversible, "
                "so this is the name as stored."
            )
        if relative is None:
            return (
                "Outside this workspace. Workbench opens conversations only from "
                "folders inside the workspace it was launched in."
            )
        return None

    # ---- resolving an encoded key back to a real directory -----------------

    def _resolve(self, key: str, listings: dict[Path, list[Path]]) -> Path | None:
        """The directory this project key was encoded from, or None.

        The encoding (every non-alphanumeric becomes ``-``) throws away which
        separator was which, so this cannot be inverted — it can only be
        *matched*. The walk starts at the folders a conversation is most likely
        to be under (the workspace first, so the common case is one listing) and
        descends only into children whose own encoded path still prefixes the
        key. Ambiguity is handled by trying every branch rather than by picking
        one: ``foo`` and ``foo-bar`` can both prefix ``foo-bar-baz``, and
        guessing between them is how you invent a path.
        """
        remembered = self._resolved.get(key)
        if remembered is not None:
            if remembered.is_dir():
                return remembered
            del self._resolved[key]  # the folder went away since the last scan
        found = self._search(key, listings)
        if found is not None:
            self._resolved[key] = found
        return found

    def _search(self, key: str, listings: dict[Path, list[Path]]) -> Path | None:
        stack: list[Path] = []
        for root in self._search_roots():
            encoded = encode_project_dir(root)
            if encoded == key:
                return root
            if _extends(encoded, key):
                stack.append(root)
        visits = 0
        while stack:
            directory = stack.pop()
            visits += 1
            if visits > MAX_RESOLVE_VISITS:
                log.debug("conversations.resolve_gave_up", key=key)
                return None
            for child in _child_dirs(directory, listings):
                encoded = encode_project_dir(child)
                if encoded == key:
                    return child
                if _extends(encoded, key):
                    stack.append(child)
        return None

    def _search_roots(self) -> list[Path]:
        """Where a match is looked for, most specific first.

        The workspace comes first so an in-workspace conversation — the only
        kind half A can open — resolves in one or two listings. The home
        directory and the filesystem anchors follow, which is what lets a
        conversation from *another* project still be shown with a real path.
        """
        roots: list[Path] = [self._workspace]
        try:
            home = Path.home()
        except (RuntimeError, OSError):  # no home on this account — not fatal
            home = self._workspace
        for candidate in (home, Path(self._workspace.anchor), Path(home.anchor)):
            if str(candidate) and candidate not in roots:
                roots.append(candidate)
        return roots


def _extends(encoded_folder: str, key: str) -> bool:
    """Could ``key`` name something *inside* the folder ``encoded_folder`` encodes?

    Full paths are encoded, so a child's encoding always starts with its
    parent's — the only question is whether the next character is a real
    boundary. It is when the parent's encoding already ends in one (a filesystem
    anchor: ``C:\\`` encodes to ``C--``, ``/`` to ``-``) or when the key's next
    character is the separator every other join produces. Without that
    distinction ``C--`` would never match ``C--Users-…`` and nothing outside the
    workspace would ever resolve.
    """
    if not key.startswith(encoded_folder) or len(key) == len(encoded_folder):
        return False
    return encoded_folder.endswith("-") or key[len(encoded_folder)] == "-"


def _child_dirs(directory: Path, listings: dict[Path, list[Path]]) -> list[Path]:
    """Sub-directories worth descending into, or nothing if we may not look.

    Build caches and dependency trees are skipped by the same rule the file tree
    uses (``services/ignore.py``): no Claude session ever ran inside
    ``node_modules``, and walking one to find that out is the difference between
    a listing and a crawl.
    """
    cached = listings.get(directory)
    if cached is not None:
        return cached
    children: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                path = Path(entry.path)
                if is_ignored_dir(path):
                    continue
                children.append(path)
    except OSError:
        children = []
    listings[directory] = children
    return children
