"""Typed result model for exact-head pull-request draft-state transitions."""

from __future__ import annotations

from pydantic import Field

from .write_contracts import ExactWriteResult


class PullRequestDraftStateTransitionResult(ExactWriteResult):
    """Authoritative outcome of one guarded pull-request draft-state transition."""

    number: int = Field(ge=1)
    previous_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    previous_is_draft: bool
    current_is_draft: bool | None = None
    url: str
