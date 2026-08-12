"""Typed result model for exact release creation."""

from __future__ import annotations

from pydantic import Field

from .write_contracts import ExactWriteResult


class ReleaseExactResult(ExactWriteResult):
    """Authoritative outcome of one exact-target guarded release creation."""

    tag_name: str = Field(min_length=1, max_length=1019)
    expected_target_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    resolved_target_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    release_id: int | None = Field(default=None, ge=1)
    release_url: str | None = None
    tag_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    release_name: str | None = None
    is_draft: bool | None = None
    is_prerelease: bool | None = None
    make_latest: bool
    is_latest: bool | None = None
