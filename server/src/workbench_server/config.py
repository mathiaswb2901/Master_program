"""Application settings.

Precedence (highest first): environment variables (WORKBENCH_*), an optional
``workbench.toml`` next to the workspace, then defaults. The workspace root is
the only setting most users ever touch.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKBENCH_", env_file=None, extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8787
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["console", "json"] = "console"

    workspace_root: Path | None = None

    # OnlyOffice Document Server (M2+). None = office editing disabled (degraded mode).
    onlyoffice_url: str | None = None
    onlyoffice_jwt_secret: str | None = None
    # Base URL the Document Server uses to reach us; default derives from host/port.
    public_base_url: str | None = None
    # Keep a .bak of every office file before an editor save overwrites it.
    office_backup: bool = True

    def resolved_public_base_url(self) -> str:
        return self.public_base_url or f"http://{self.host}:{self.port}"

    # Agent sessions
    max_concurrent_sessions: int = 4
    # Claude Code's per-project session storage; None = ~/.claude/projects
    claude_projects_dir: Path | None = None

    # Load Workbench's own skills into every session, as a session-scoped local
    # plugin (see services/skills_bundle.py). Nothing is written to ~/.claude.
    bundled_skills: bool = True
    # Also load the user's global ~/.claude settings and skills into sessions.
    # Off by default: a Workbench session sees project settings plus our bundle,
    # so what an agent can do is a property of the workspace, not of whatever
    # happens to be installed globally on this machine.
    skills_inherit_user: bool = False

    def resolved_projects_dir(self) -> Path:
        return self.claude_projects_dir or (Path.home() / ".claude" / "projects")

    def resolved_workspace(self) -> Path:
        """The workspace root the server operates on. Defaults to the CWD it was launched from."""
        root = self.workspace_root or Path.cwd()
        return root.resolve()


def load_settings() -> Settings:
    """Single construction point so tests can build Settings explicitly instead."""
    return Settings()
