"""REST response models shared across routers."""

from pathlib import Path

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    workspace: Path
