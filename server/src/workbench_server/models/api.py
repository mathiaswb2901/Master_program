"""REST response models shared across routers."""

from pathlib import Path

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    workspace: Path


class AuthTokenResponse(BaseModel):
    """The per-launch token, handed out at GET /api/auth/token.

    The one endpoint exempt from the token requirement (chicken-and-egg: a
    client with no token has to be able to fetch one), so the router guards it
    on Origin *and* Host being local instead — the anti-rebind guard for the
    handout itself.
    """

    token: str
