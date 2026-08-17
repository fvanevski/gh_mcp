"""Structured exact-head eligibility evidence for formal pull-request reviews."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReviewEligibilityReason = Literal[
    "eligible",
    "head_mismatch",
    "write_policy_denied",
    "ordinary_identity_unavailable",
    "reviewer_not_configured",
    "reviewer_identity_unavailable",
    "reviewer_is_pr_author",
]


class PullRequestReviewEligibility(BaseModel):
    """Advisory review eligibility bound to one exact pull-request head."""

    number: int = Field(ge=1)
    expected_head_sha: str
    current_head_sha: str
    head_matches_expected: bool
    pr_author_login: str | None = None
    ordinary_login: str | None = None
    reviewer_login: str | None = None
    reviewer_kind: Literal["github_app", "static_token"] | None = None
    approval_eligible: bool
    comment_review_available: bool
    reason: ReviewEligibilityReason
    warning: str | None = None
