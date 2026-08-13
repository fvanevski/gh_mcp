"""Typed GitHub API rate-limit and local governor diagnostic models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GitHubPrimaryRateLimitState(BaseModel):
    """Primary REST API state reported by GitHub's ``resources.core`` object."""

    resource: Literal["core"] = "core"
    limit: int | None = Field(default=None, ge=0)
    remaining: int | None = Field(default=None, ge=0)
    used: int | None = Field(default=None, ge=0)
    reset_epoch: int | None = Field(default=None, ge=0)


class GitHubRateLimitResponseHeaders(BaseModel):
    """Rate-limit metadata already captured from the current GitHub response headers."""

    remaining: int | None = Field(default=None, ge=0)
    reset_epoch: int | None = Field(default=None, ge=0)
    retry_after_seconds: float | None = Field(default=None, ge=0)


class GitHubApiRateObservation(BaseModel):
    """GitHub-provided state from one governed ``GET /rate_limit`` attempt."""

    request_performed: bool
    response_body_available: bool
    observed_at_epoch: float = Field(ge=0)
    request_id: str | None = None
    headers: GitHubRateLimitResponseHeaders
    primary: GitHubPrimaryRateLimitState | None = None


class GitHubGovernorRateStatus(BaseModel):
    """Local request-governor state, kept separate from GitHub-provided observations."""

    observed_at_epoch: float = Field(ge=0)
    reads_blocked: bool
    writes_blocked: bool
    writes_delayed: bool
    write_delay_seconds: float = Field(ge=0)
    blocked_until_epoch: float | None = Field(default=None, ge=0)
    retry_after_seconds: float | None = Field(default=None, ge=0)
    block_reason: Literal["retry_after", "primary_reset", "fallback"] | None = None
    last_rate_event_at_epoch: float | None = Field(default=None, ge=0)
    last_request_id: str | None = None
    last_warning: str | None = None


class ApiRateStatus(BaseModel):
    """GitHub API rate-limit evidence plus separate local governor policy state."""

    github: GitHubApiRateObservation
    governor: GitHubGovernorRateStatus
