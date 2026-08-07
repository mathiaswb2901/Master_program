"""Application settings.

Precedence (highest first): environment variables (WORKBENCH_*), an optional
``workbench.toml`` next to the workspace, then defaults. The workspace root is
the only setting most users ever touch.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from workbench_server.models.worktrees import MAX_LEASE_SECONDS, MIN_LEASE_SECONDS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKBENCH_", env_file=None, extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8787
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["console", "json"] = "console"

    # The workspace the server starts on. No longer the last word on it: M5's
    # switcher re-roots the running server (see services/workspaces.py), so this
    # is the *initial* root and the only thing that makes it an explicit choice
    # rather than "wherever the server happened to be launched".
    workspace_root: Path | None = None

    # Where machine-local Workbench state lives — the recent-workspaces list, and
    # anything else that is about the user rather than about a project. None = the
    # OS's own app data dir (services/app_data.py). Set by the E2E lane so a test
    # run never writes into the developer's real history; a user has no reason to.
    app_data_root: Path | None = None

    # OnlyOffice Document Server (M2+). None = office editing disabled (degraded mode).
    onlyoffice_url: str | None = None
    onlyoffice_jwt_secret: str | None = None
    # Base URL the Document Server uses to reach us; default derives from host/port.
    public_base_url: str | None = None
    # Keep a .bak of every office file before an editor save overwrites it.
    office_backup: bool = True

    # Native Office hosting (M4): open documents in the *real* installed
    # Word/Excel, reparented into a panel. "auto" currently resolves to NOT
    # hosting natively — the window-hosting backend does not ship yet and only
    # becomes the default once hang isolation is proven (owner decision,
    # 2026-08-05). "off" disables it outright, whatever else is configured.
    office_native: Literal["auto", "on", "off"] = "auto"
    # Replace the host backend with the in-process fake in
    # services/office_host/fake_backend.py: the whole lifecycle, deterministic
    # failures, and no Office, no windows, no real process anywhere. Same
    # precedent as fake_agent — off by default and loudly logged when on, since
    # a panel claiming to hold a document that is not really open would be a
    # worse lie than a panel that fails.
    office_fake: bool = False

    def resolved_public_base_url(self) -> str:
        return self.public_base_url or f"http://{self.host}:{self.port}"

    # Agent sessions
    max_concurrent_sessions: int = 4
    # Claude Code's per-project session storage; None = ~/.claude/projects
    claude_projects_dir: Path | None = None

    # Replace the Agent SDK with the scripted stand-in in services/fake_agent.py:
    # deterministic replies, plan cards and permission prompts, no Claude login
    # and no tokens. This is how the Playwright suite drives the real backend.
    # Off by default and loudly logged when on — a workbench that answers with
    # canned text instead of an agent must never look like a working one.
    fake_agent: bool = False

    # Load Workbench's own skills into every session, as a session-scoped local
    # plugin (see services/skills_bundle.py). Nothing is written to ~/.claude.
    bundled_skills: bool = True
    # Restores ALL of the user's global ~/.claude settings — hooks that run
    # arbitrary commands on session events and permission allow/deny rules, not
    # just skills and CLAUDE.md memory. Off by default: a session sees the
    # workspace's own settings (.claude/settings.json plus the machine-local
    # .claude/settings.local.json) and our bundle, so what an agent can do is a
    # property of the workspace, not of whatever happens to be installed
    # globally on this machine.
    inherit_user_settings: bool = False

    def resolved_projects_dir(self) -> Path:
        return self.claude_projects_dir or (Path.home() / ".claude" / "projects")

    # Managed worktree pool (M5): borrowed git worktrees so parallel agents get
    # one writer per checkout. The pool root is deliberately NOT under the
    # workspace — see services/worktrees.py — so the file tree and the watcher
    # never see a slot. None = the machine-local app data dir.
    worktree_root: Path | None = None
    # How many slots the pool may grow to. Slots are created on demand and never
    # destroyed, so this is the ceiling on borrowed checkouts, not a count that
    # is allocated up front.
    worktree_pool_size: int = 4
    # Default lease, in seconds. One of the two idle signals: a slot is
    # reclaimable only when the lease has expired *and* its owner process is
    # gone. Long enough that an agent working unattended keeps its slot.
    #
    # Bounded by the *same* constants as AcquireWorktreeRequest.ttl_seconds and
    # RenewWorktreeRequest.ttl_seconds, and for the same reason: a floor keeps
    # the next sweep from reclaiming a slot out from under its holder, and a
    # ceiling keeps a typo from parking one for a year. Bounding only the
    # explicit override would have protected the rare path and left the common
    # one open — most callers never send a ttl_seconds and land here.
    worktree_lease_seconds: float = Field(
        default=3600.0, ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS
    )

    # Mission Control (M5 item 7): the ceilings an orchestrator session's crew
    # works under. Enforced in `services/orchestrator.py` *before* a worker is
    # spawned, and every refusal names the variable below that raises it — a cap
    # a user cannot see the way out of is the dead button the pane rules forbid.
    #
    # Defaults are deliberately modest. An orchestrator is the one capability
    # here that can spend money without a human in the loop for each step, so
    # the out-of-the-box numbers are ones a mistake can survive: four workers is
    # the worktree pool's own default, and the dollar ceilings are roughly a
    # morning's work rather than a plan's weekly limit.
    orchestrator_max_workers: int = Field(default=4, ge=1, le=16)
    orchestrator_worker_turns: int = Field(default=40, ge=1)
    orchestrator_worker_cost_usd: float = Field(default=5.0, gt=0.0)
    orchestrator_fleet_turns: int = Field(default=200, ge=1)
    orchestrator_fleet_cost_usd: float = Field(default=20.0, gt=0.0)

    # Local-API security hardening (M5 item 8 / OSS-bar item 1). The server
    # binds loopback, but the attacker is remote web content (drive-by/CSRF and
    # DNS-rebinding) that can still send REST/WS requests to 127.0.0.1. Two
    # defenses, wired in services/local_auth.py: a per-launch token required on
    # REST + WS, and a strict WS Origin allowlist. None = mint a fresh
    # `secrets.token_urlsafe(32)` per launch (the common case; an explicit value
    # lets an attaching desktop shell be told the token out of band).
    auth_token: str | None = None
    # The rollout flag, now ON (M5 item 8, PR4). The token is required on REST +
    # WS and the WS handshake is gated on Origin. Kept as a setting so it can be
    # forced back OFF for debugging — `WORKBENCH_ENFORCE_AUTH=0` — without editing
    # code; the whole client (`ui/src/token.ts`, `api.ts`, `ws.ts`) and both test
    # harnesses present the token, so the shipped default is enforcement on.
    enforce_auth: bool = True

    def resolved_workspace(self) -> Path:
        """The workspace root the server operates on. Defaults to the CWD it was launched from."""
        root = self.workspace_root or Path.cwd()
        return root.resolve()


def load_settings() -> Settings:
    """Single construction point so tests can build Settings explicitly instead."""
    return Settings()
