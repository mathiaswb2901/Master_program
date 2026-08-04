"""Index of Claude Code session transcripts, grouped per folder.

Claude Code (and the Agent SDK, which drives it) persists every session as a
JSONL file under ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. We
read that storage directly — the workbench shows exactly the same per-folder
history the CLI would, and sessions started in either place appear in both.
"""

import json
import re
from pathlib import Path

import structlog

from workbench_server.models.agents import SessionInfo, TranscriptMessage

log = structlog.get_logger()

_MAX_TITLE = 80


def encode_project_dir(folder: Path) -> str:
    """Claude Code's project-dir encoding: non-alphanumerics become '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(folder))


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
            title = self._first_user_text(transcript) or "(no messages)"
            sessions.append(
                SessionInfo(
                    session_id=transcript.stem,
                    folder=workspace_relative,
                    state="idle",
                    live=False,
                    title=title[:_MAX_TITLE],
                    updated_at=transcript.stat().st_mtime,
                )
            )
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def read_transcript(self, folder: Path, session_id: str) -> list[TranscriptMessage]:
        # session ids are uuids; reject anything path-like before touching disk
        if not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            raise FileNotFoundError(session_id)
        transcript = self.project_dir(folder) / f"{session_id}.jsonl"
        if not transcript.is_file():
            raise FileNotFoundError(session_id)
        messages: list[TranscriptMessage] = []
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            entry = self._parse_line(line)
            if entry is not None:
                messages.append(entry)
        return messages

    def _parse_line(self, line: str) -> TranscriptMessage | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
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

    def first_user_text(self, folder: Path, session_id: str) -> str | None:
        """First user message of a stored transcript; None when absent/unreadable."""
        if not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            return None
        transcript = self.project_dir(folder) / f"{session_id}.jsonl"
        if not transcript.is_file():
            return None
        return self._first_user_text(transcript)

    def _first_user_text(self, transcript: Path) -> str | None:
        try:
            with transcript.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    entry = self._parse_line(line)
                    if entry is not None and entry.role == "user":
                        return entry.text
        except OSError:
            log.warning("session_index.unreadable", path=str(transcript))
        return None
