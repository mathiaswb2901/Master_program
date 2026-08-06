"""Index of Claude Code session transcripts, grouped per folder.

Claude Code (and the Agent SDK, which drives it) persists every session as a
JSONL file under ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. We
read that storage directly — the workbench shows exactly the same per-folder
history the CLI would, and sessions started in either place appear in both.

**Read-only, always.** This is somebody else's storage: nothing here opens a
transcript for writing, renames one, or removes one, and nothing ever should.
A bug in our parser must cost a row in a list, never a conversation.

One line parser (:func:`parse_line`), two traversals over it, because they stop
at different places and the difference is the whole cost:

* :func:`first_user_text` reads until the first user message and stops — the
  per-folder listing needs a title and nothing else, and a 48 MB transcript
  would otherwise be read in full to learn its first sentence;
* :func:`scan_transcript` reads the whole file (up to a stated byte budget) to
  count turns as well. That is the conversation *browser's* pass
  (``services/conversations.py``), which caches it against the file's mtime.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import structlog

from workbench_server.models.agents import SessionInfo, TranscriptMessage
from workbench_server.services.titles import derive_title

log = structlog.get_logger()

#: Bytes of one transcript a traversal will read before giving up. Generous —
#: the largest transcript on the author's machine is 49 MB and the median is
#: 0.8 MB — but finite, so one pathological file cannot wedge a scan over
#: hundreds of them. A file that hits it reports ``truncated``; its turn count
#: is then a floor and says so on screen rather than being quietly wrong.
SCAN_BYTE_BUDGET = 64 * 1024 * 1024

#: Bytes of a *single* JSONL record a traversal will hold in memory. Generous
#: for the same reason: a real record regularly embeds a whole tool output (a
#: file dump, a long ``grep``), and that is not pathological, it is Tuesday.
#: What it rules out is the case where one record *is* the file — a corrupt
#: tail with no newline in it, or a tool result that ran away. A record past
#: this is dropped unparsed and the pass reports ``truncated``; an over-long
#: record is virtually always a ``tool_result``, which is never a turn, so the
#: count that follows is a floor rather than wrong.
MAX_LINE_BYTES = 8 * 1024 * 1024

#: Bytes pulled off disk at a time. Small enough that it is noise next to
#: ``MAX_LINE_BYTES``, big enough that a 49 MB transcript is not 49 million
#: syscalls.
_CHUNK_BYTES = 64 * 1024

#: Session ids are uuids. Anything else is rejected before a path is built from
#: it — this string arrives from the wire.
_SESSION_ID = re.compile(r"[A-Za-z0-9-]+")

#: Cheap pre-filter for :func:`scan_transcript`: a line that cannot be a user
#: record is skipped without parsing it, which is most of what a full pass over
#: a 398 MB store costs.
#:
#: Deliberately the *loose* marker rather than ``b'"type":"user"'``. A user
#: record always carries both ``"type": "user"`` and ``"role": "user"``, so this
#: matches whether the writer emitted compact JSON or spaced JSON — while the
#: tight marker silently misses every spaced record and reports a conversation
#: as having no turns. A filter may cost an extra parse (an assistant message
#: that quotes the word); it may never change the answer.
_USER_MARK = b'"user"'


def encode_project_dir(folder: Path | str) -> str:
    """Claude Code's project-dir encoding: non-alphanumerics become '-'.

    Takes a string too, because the conversation browser encodes candidate paths
    while matching a key back to a real directory and there must be exactly one
    copy of this rule.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(folder))


def valid_session_id(session_id: str) -> bool:
    """True if this is a name we will build a path from."""
    return _SESSION_ID.fullmatch(session_id) is not None


def _extract_text(content: object) -> str:
    """Message content can be a plain string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def parse_line(line: str | bytes) -> TranscriptMessage | None:
    """One JSONL record as a chat message, or None if it is not one.

    None covers every "not a message" case with the same answer, which is what
    makes a truncated or corrupt transcript degrade to *fewer rows* instead of
    an error: a half-written last line fails to parse, a ``tool_result`` carries
    no text block, and a summary record is neither ``user`` nor ``assistant``.
    """
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    text = _extract_text(message.get("content"))
    if not text.strip():
        return None
    role = "user" if record["type"] == "user" else "assistant"
    return TranscriptMessage(role=role, text=text)


@dataclass(frozen=True)
class TranscriptFacts:
    """What one full pass over a transcript can honestly say about it."""

    #: First user message, for :func:`~workbench_server.services.titles.derive_title`.
    first_user_text: str | None
    #: User messages carrying text. Tool results are stored as ``user`` records
    #: too, and they are *not* turns — :func:`parse_line` drops them because
    #: they carry no text block, which is what keeps this number the one a
    #: person recognises ("I said twelve things") rather than a file statistic.
    turns: int
    #: Something was not looked at — the byte budget ran out, or one record was
    #: too large to hold — so ``turns`` is a floor, not a count.
    truncated: bool
    #: Why this transcript is not fully knowable, in words, or None.
    problem: str | None


class _BoundedRead:
    """A transcript walked in chunks, so neither bound is checked too late.

    ``for line in fh`` buffers everything up to the next newline *before* the
    loop body runs, which makes a budget checked in that body useless against
    the case it exists for: a record megabytes long (a ``tool_result`` carrying
    a whole file dump — ordinary, not adversarial), or a corrupt tail with no
    newline in it at all. Either is read whole first, and
    :meth:`~workbench_server.services.conversations.ConversationBrowser.browse`
    holds one process-wide lock for its entire scan, so every other pane's
    request queues behind that read.

    So the chunking is ours. A fixed ``_CHUNK_BYTES`` read, split here, with the
    total budget bounding what is *asked for* and the size of one record checked
    as it accumulates: memory is bounded by ``max_line_bytes`` plus a chunk no
    matter what is on disk, and so is the time to find that out.
    """

    __slots__ = ("_budget", "_fh", "_max_line", "truncated")

    def __init__(self, fh: IO[bytes], byte_budget: int, max_line_bytes: int) -> None:
        self._fh = fh
        self._budget = byte_budget
        self._max_line = max_line_bytes
        #: Something was skipped: the budget ran out, or a record was dropped
        #: for being too long. A count taken from this read is then a floor.
        self.truncated = False

    def lines(self) -> Iterator[bytes]:
        read = 0
        line = bytearray()
        dropping = False  # inside a record already known to be too long
        while read < self._budget:
            chunk = self._fh.read(min(_CHUNK_BYTES, self._budget - read))
            if not chunk:
                # EOF. A last record with no newline after it is still a record
                # — and a transcript being appended to right now ends that way.
                if line and not dropping:
                    yield bytes(line)
                return
            read += len(chunk)
            start = 0
            while (newline := chunk.find(b"\n", start)) >= 0:
                if dropping:
                    dropping = False  # the over-long record ends here
                else:
                    line += chunk[start:newline]
                    yield bytes(line)
                line.clear()
                start = newline + 1
            if dropping:
                continue
            line += chunk[start:]
            if len(line) > self._max_line:
                self.truncated = True
                dropping = True
                line.clear()
        # The budget ran out. Whether that actually hid anything is one byte
        # away, and the difference is "120+ turns" versus "120 turns".
        if self._fh.read(1):
            self.truncated = True
        elif line and not dropping:
            yield bytes(line)


def first_user_text(
    transcript: Path,
    byte_budget: int = SCAN_BYTE_BUDGET,
    max_line_bytes: int = MAX_LINE_BYTES,
) -> str | None:
    """First user message of a stored transcript; None when absent/unreadable."""
    try:
        with transcript.open("rb") as fh:
            for line in _BoundedRead(fh, byte_budget, max_line_bytes).lines():
                entry = parse_line(line)
                if entry is not None and entry.role == "user":
                    return entry.text
    except OSError:
        log.warning("session_index.unreadable", path=str(transcript))
    return None


def scan_transcript(
    transcript: Path,
    byte_budget: int = SCAN_BYTE_BUDGET,
    max_line_bytes: int = MAX_LINE_BYTES,
) -> TranscriptFacts:
    """Title and turn count in one pass, with everything it cannot know named.

    Deliberately tolerant: a line that does not parse is skipped rather than
    failing the file, because the *last* line of a transcript being written
    right now is regularly half-flushed. A file that yields no messages at all
    is the case worth saying out loud, and it does.
    """
    first: str | None = None
    turns = 0
    truncated = False
    try:
        with transcript.open("rb") as fh:
            bounded = _BoundedRead(fh, byte_budget, max_line_bytes)
            for line in bounded.lines():
                if _USER_MARK not in line:
                    continue
                entry = parse_line(line)
                if entry is None or entry.role != "user":
                    continue
                turns += 1
                if first is None:
                    first = entry.text
            truncated = bounded.truncated
    except OSError:
        log.warning("session_index.unreadable", path=str(transcript))
        return TranscriptFacts(None, 0, False, "This transcript could not be read.")
    if first is None:
        return TranscriptFacts(
            None, 0, truncated, "No readable messages — the transcript may be empty or damaged."
        )
    return TranscriptFacts(first, turns, truncated, None)


class SessionIndex:
    def __init__(self, projects_root: Path) -> None:
        self._root = projects_root

    def project_dir(self, folder: Path) -> Path:
        return self._root / encode_project_dir(folder)

    def list_sessions(self, folder: Path, workspace_relative: str) -> list[SessionInfo]:
        project = self.project_dir(folder)
        if not project.is_dir():
            return []
        sessions: list[SessionInfo] = []
        for transcript in project.glob("*.jsonl"):
            first = first_user_text(transcript)
            # derive_title, same as live sessions — a conversation must not
            # visibly retitle when it transitions from live to on-disk.
            title = derive_title(first) if first is not None else "(no messages)"
            sessions.append(
                SessionInfo(
                    session_id=transcript.stem,
                    folder=workspace_relative,
                    state="idle",
                    live=False,
                    title=title,
                    updated_at=transcript.stat().st_mtime,
                )
            )
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def read_transcript(self, folder: Path, session_id: str) -> list[TranscriptMessage]:
        if not valid_session_id(session_id):
            raise FileNotFoundError(session_id)
        transcript = self.project_dir(folder) / f"{session_id}.jsonl"
        if not transcript.is_file():
            raise FileNotFoundError(session_id)
        messages: list[TranscriptMessage] = []
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            entry = parse_line(line)
            if entry is not None:
                messages.append(entry)
        return messages

    def first_user_text(self, folder: Path, session_id: str) -> str | None:
        """First user message of a stored transcript; None when absent/unreadable."""
        if not valid_session_id(session_id):
            return None
        transcript = self.project_dir(folder) / f"{session_id}.jsonl"
        if not transcript.is_file():
            return None
        return first_user_text(transcript)
