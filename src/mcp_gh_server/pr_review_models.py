"""Typed result models for pull-request review-state evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .evidence import PaginationEvidence


class PullRequestReview(BaseModel):
    """One submitted or pending pull-request review with commit provenance."""

    id: int = Field(ge=1)
    reviewer: str | None = None
    state: str = Field(min_length=1)
    commit_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    submitted_at: str | None = None
    body: str
    author_association: str | None = None


class PullRequestReviewsPage(BaseModel):
    """One bounded page of reviews for an unchanged pull-request head snapshot."""

    number: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    page: int = Field(ge=1)
    per_page: int = Field(ge=1, le=100)
    returned_count: int = Field(ge=0)
    has_more: bool
    truncated: bool
    warning: str | None = None
    reviews: list[PullRequestReview]


class PullRequestReviewThread(BaseModel):
    """One unresolved review thread retained from the bounded thread connection."""

    id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    line: int | None = None
    original_line: int | None = None
    is_outdated: bool
    comment_count: int = Field(ge=0)


class PullRequestReviewThreadDetail(BaseModel):
    """Exact review-thread identity, location, and lifecycle state."""

    id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    line: int | None = None
    original_line: int | None = None
    is_resolved: bool
    is_outdated: bool
    comment_count: int = Field(ge=0)


class PullRequestReviewThreadComment(BaseModel):
    """One review comment with bounded body evidence and provenance."""

    id: str = Field(min_length=1)
    database_id: int | None = Field(default=None, ge=1)
    author: str | None = None
    author_association: str | None = None
    created_at: str = Field(min_length=1)
    updated_at: str | None = None
    url: str | None = None
    reply_to_id: str | None = None
    body: str
    body_bytes_returned: int = Field(ge=0)
    body_total_bytes: int = Field(ge=0)
    body_truncated: bool
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_warning: str | None = None


class PullRequestReviewThreadResult(BaseModel):
    """Bounded thread-detail evidence bound to one exact pull-request head."""

    number: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_matches_expected: bool
    exact_head_evidence: bool
    thread: PullRequestReviewThreadDetail | None = None
    comments_evidence: PaginationEvidence | None = None
    comments: list[PullRequestReviewThreadComment] = Field(default_factory=list)
    warning: str | None = None


ReviewDecision = Literal["APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"]


class PullRequestReviewState(BaseModel):
    """Conservative exact-head aggregate of review evidence for one pull request."""

    number: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_matches_expected: bool
    exact_head_evidence: bool
    reviews_evidence: PaginationEvidence | None = None
    review_threads_evidence: PaginationEvidence | None = None
    current_head_approvals: list[PullRequestReview] = Field(default_factory=list)
    current_head_change_requests: list[PullRequestReview] = Field(default_factory=list)
    current_head_comments: list[PullRequestReview] = Field(default_factory=list)
    stale_approvals: list[PullRequestReview] = Field(default_factory=list)
    stale_change_requests: list[PullRequestReview] = Field(default_factory=list)
    requested_reviewers: list[str] = Field(default_factory=list)
    requested_teams: list[str] = Field(default_factory=list)
    unresolved_review_threads: list[PullRequestReviewThread] = Field(default_factory=list)
    review_decision: ReviewDecision | None = None
    requirements_satisfied: bool | None = None
    requirements_reason: str
    warning: str | None = None
