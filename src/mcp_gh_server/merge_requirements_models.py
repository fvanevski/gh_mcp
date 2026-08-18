"""Typed exact-head merge-requirement evidence models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import PullRequestCheck
from .pr_review_models import PullRequestReview, PullRequestReviewThread, ReviewDecision

MergeMethod = Literal["merge", "squash", "rebase"]
MergeEvidenceStatus = Literal[
    "complete",
    "present",
    "absent",
    "unavailable",
    "truncated",
    "head_mismatch",
]


class RequiredStatusCheck(BaseModel):
    """One required status-check identity from effective merge policy."""

    context: str = Field(min_length=1)
    integration_id: int | None = None


class MergeRequirementEvidenceSource(BaseModel):
    """Bounded diagnostic for one source contributing to merge-readiness evidence."""

    source: str = Field(min_length=1, max_length=80)
    status: MergeEvidenceStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    reason: str | None = Field(default=None, max_length=512)
    blocks_policy_evidence: bool = False
    blocks_checks_evidence: bool = False
    blocks_allowed_merge_methods: bool = False


class PullRequestMergeRequirements(BaseModel):
    """Conservative exact-head aggregate of merge-policy and readiness evidence."""

    number: int = Field(ge=1)
    base_ref: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_matches_expected: bool
    exact_head_evidence: bool
    mergeable: bool | None = None
    merge_state: str | None = None
    policy_evidence_complete: bool
    checks_evidence_complete: bool
    review_evidence_complete: bool
    up_to_date_evidence_complete: bool
    required_status_checks: list[RequiredStatusCheck] = Field(default_factory=list)
    current_required_checks: list[PullRequestCheck] = Field(default_factory=list)
    required_approvals: int | None = Field(default=None, ge=0)
    current_valid_approvals: list[PullRequestReview] = Field(default_factory=list)
    current_valid_approval_count: int | None = Field(default=None, ge=0)
    review_decision: ReviewDecision | None = None
    review_requirements_satisfied: bool | None = None
    code_owner_review_required: bool | None = None
    last_push_approval_required: bool | None = None
    conversation_resolution_required: bool | None = None
    unresolved_review_threads: list[PullRequestReviewThread] = Field(default_factory=list)
    up_to_date_required: bool | None = None
    up_to_date: bool | None = None
    allowed_merge_methods: list[MergeMethod] = Field(default_factory=list)
    allowed_merge_methods_complete: bool
    evidence_sources: list[MergeRequirementEvidenceSource] = Field(default_factory=list)
    warning: str | None = None
