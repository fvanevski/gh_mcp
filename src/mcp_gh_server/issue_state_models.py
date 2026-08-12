"""Typed result model for exact issue lifecycle state transitions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .write_contracts import ExactWriteResult

IssueState = Literal["open", "closed"]
IssueStateReason = Literal["completed", "not_planned", "duplicate", "reopened"]


class IssueStateTransitionResult(ExactWriteResult):
    """Authoritative outcome of one guarded issue state transition."""

    number: int = Field(ge=1)
    previous_state: IssueState
    new_state: IssueState | None = None
    state_reason: IssueStateReason | None = None
    closed_at: str | None = None
    reopened_at: str | None = Field(
        default=None,
        description=(
            "GitHub updated_at timestamp associated with a verified reopen transition. "
            "A confirmed mutation response is preferred; authoritative readback is the "
            "fallback because GitHub issue objects expose no dedicated reopened_at field."
        ),
    )
    url: str
