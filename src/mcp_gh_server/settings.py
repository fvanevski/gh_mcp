"""Environment and .env-backed configuration."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import find_dotenv
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Normal settings use the MCP_GH_ prefix. GITHUB_TOKEN intentionally keeps
    its conventional unprefixed name.
    """

    model_config = SettingsConfigDict(
        env_prefix="MCP_GH_",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    github_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_TOKEN", "MCP_GH_GITHUB_TOKEN"),
    )

    allow_write_commands: bool = False
    allowed_repositories: str = ""
    allowed_owners: str = ""
    allow_repo_creation: bool = False
    allow_release_creation: bool = False
    allow_workflow_dispatch: bool = False
    allow_content_commits: bool = False
    allow_pr_merge: bool = False

    max_commit_files: int = Field(default=100, ge=1, le=1000)
    max_file_bytes: int = Field(default=1_000_000, ge=1)
    max_commit_bytes: int = Field(default=5_000_000, ge=1)
    max_pr_diff_bytes: int = Field(default=500_000, ge=1, le=1_000_000)
    max_pr_file_patch_bytes: int = Field(default=8_000, ge=1, le=100_000)
    max_pr_commit_message_bytes: int = Field(default=4_000, ge=1, le=100_000)
    max_failed_run_log_bytes: int = Field(default=500_000, ge=1, le=1_000_000)
    max_action_log_bytes: int = Field(default=500_000, ge=1, le=1_000_000)

    default_max_results: int = Field(default=30, ge=1)
    hard_max_results: int = Field(default=100, ge=1)
    command_timeout_seconds: float = Field(default=30, gt=0)

    transport: Literal["stdio", "streamable-http"] = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8766, ge=1, le=65_535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @model_validator(mode="after")
    def validate_limits(self) -> Settings:
        if self.default_max_results > self.hard_max_results:
            raise ValueError("MCP_GH_DEFAULT_MAX_RESULTS cannot exceed MCP_GH_HARD_MAX_RESULTS")
        return self


def locate_env_file() -> Path | None:
    """Find an explicit env file or the nearest .env above the current directory."""

    explicit = os.getenv("MCP_GH_ENV_FILE")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"MCP_GH_ENV_FILE does not exist or is not a file: {path}")
        return path

    discovered = find_dotenv(filename=".env", usecwd=True)
    return Path(discovered).resolve() if discovered else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once; process environment overrides values from .env."""

    env_file = locate_env_file()
    return Settings(_env_file=env_file)  # type: ignore[call-arg]
