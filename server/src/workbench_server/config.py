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

    # Agent sessions
    max_concurrent_sessions: int = 4
    # Claude Code's per-project session storage; None = ~/.claude/projects
    claude_projects_dir: Path | None = None

    def resolved_projects_dir(self) -> Path:
        return self.claude_projects_dir or (Path.home() / ".claude" / "projects")

    def resolved_workspace(self) -> Path:
        """The workspace root the server operates on. Defaults to the CWD it was launched from."""
        root = self.workspace_root or Path.cwd()
        return root.resolve()


def load_settings() -> Settings:
    """Single construction point so tests can build Settings explicitly instead."""
    return Settings()
