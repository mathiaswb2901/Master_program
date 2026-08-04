"""Session title derivation, shared by live sessions and the transcript index.

Lives in its own module so both ``agent_sessions`` (live titles) and
``session_index`` (stored-transcript titles) derive identical titles without
importing each other.
"""

MAX_TITLE_CHARS = 60
FALLBACK_TITLE = "new session"


def derive_title(text: str) -> str:
    """Session title from the first user message: collapsed, truncated to ~60 chars."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return FALLBACK_TITLE
    if len(collapsed) <= MAX_TITLE_CHARS:
        return collapsed
    return collapsed[: MAX_TITLE_CHARS - 1].rstrip() + "…"
