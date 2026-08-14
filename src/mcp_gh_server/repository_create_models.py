"""Typed result model for canonical repository creation."""

from __future__ import annotations

from pydantic import Field

from .write_contracts import ExactWriteResult


class RepositoryCreateResult(ExactWriteResult):
    """Authoritative outcome of one exact-target repository creation attempt."""

    owner: str = Field(min_length=1, max_length=39)
    repo: str = Field(min_length=1, max_length=100)
    name_with_owner: str | None = None
    url: str | None = None
    is_private: bool | None = None
    description: str | None = None
    initialized: bool | None = None
