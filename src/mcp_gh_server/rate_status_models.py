"""Typed GitHub API rate-limit and local governor diagnostic models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GitHubPrimaryRateLimitState(BaseModel):
    """One primary REST/API rate resource reported by GitHub."""

    resource: str = Field(min_length=1)
    limit: int | None = Field(default=None, ge=0)
    remaining: int | None = Field(default=None, ge=0)
    used: int | None = Field(default=None, ge=0)
    reset_epoch: int | None = Field(default=None, ge=0)


class GitHubRateLimitResponseHeaders(BaseModel):
    """Rate-limit metadata captured from the source GitHub response headers."""

    resource: str | None = None
    limit: int | None = Field(default=None, ge=0)
    remaining: int | None = Field(default=None, ge=0)
    used: int | None = Field(default=None, ge=0)
    reset_epoch: int | None = Field(default=None, ge=0)
    retry_after_seconds: float | None = Field(default=None, ge=0)


class GitHubApiRateObservation(BaseModel):
    """GitHub-provided state from the most recent governed ``GET /rate_limit`` observation."""

    request_performed: bool
    cached: bool
    cache_age_seconds: float | None = Field(default=None, ge=0)
    response_body_available: bool
    observed_at_epoch: float | None = Field(default=None, ge=0)
    request_id: str | None = None
    headers: GitHubRateLimitResponseHeaders
    primary: GitHubPrimaryRateLimitState | None = None
    primary_resources: list[GitHubPrimaryRateLimitState] = Field(default_factory=list)


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
    last_rate_request_id: str | None = None
    last_rate_warning: str | None = None


class ApiRateStatus(BaseModel):
    """GitHub API rate-limit evidence plus separate local governor policy state."""

    github: GitHubApiRateObservation
    governor: GitHubGovernorRateStatus
