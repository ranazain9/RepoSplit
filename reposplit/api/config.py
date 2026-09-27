"""Configuration settings for the RepoSplit Telemetry and API platform."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class ApiSettings(BaseModel):
    title: str = "RepoSplit 2.0 telemetry"
    version: str = "2.0.0"
    max_upload_size_mb: int = Field(default_factory=lambda: int(os.environ.get("REPOSPLIT_MAX_UPLOAD_MB", "100")))
    cors_origins: list[str] = Field(
        default_factory=lambda: [o.strip() for o in os.environ.get("REPOSPLIT_CORS_ORIGINS", "*").split(",") if o.strip()]
    )
    storage_dir: Path = Field(default_factory=lambda: Path(os.environ.get("REPOSPLIT_STORAGE_DIR", ".reposplit")))
    api_key: str | None = Field(default_factory=lambda: os.environ.get("REPOSPLIT_API_KEY"))


settings = ApiSettings()
