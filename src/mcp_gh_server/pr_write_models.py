"""Canonical result models for pull-request writes."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .write_contracts import ExactWriteResult


class PullRequestReviewSubmission(ExactWriteResult):
    """Shared authoritative outcome fields for one formal pull-request review attempt."""

    number: int
    review_id: int = Field(ge=0)
    action: Literal["approve", "request_changes", "comment"]
    state: str
    body: str
    author: str | None = None
    submitted_at: str | None = None
    commit_sha: str
    url: str
    message: str


class PullRequestApproval(PullRequestReviewSubmission):
    """Outcome of one action-specific GitHub APPROVED review attempt."""

    action: Literal["approve"] = "approve"


class PullRequestChangesRequested(PullRequestReviewSubmission):
    """Outcome of one action-specific GitHub CHANGES_REQUESTED review attempt."""

    action: Literal["request_changes"] = "request_changes"


class PullRequestCommentReview(PullRequestReviewSubmission):
    """Outcome of one action-specific GitHub COMMENTED review attempt."""

    action: Literal["comment"] = "comment"


class PullRequestMerge(ExactWriteResult):
    """Authoritative outcome of one exact-head pull-request merge attempt."""

    number: int
    method: Literal["merge", "squash", "rebase"]
    head_sha: str
    state: str
    merged: bool
    merge_queued: bool = False
    auto_merge_enabled: bool = False
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    merge_state_status: str | None = None
    url: str
    message: str


class PullRequestCreate(ExactWriteResult):
    """Authoritative outcome of one pull-request creation attempt."""

    number: int
    title: str
    url: str
    message: str


class PullRequestEdit(ExactWriteResult):
    """Authoritative outcome of one pull-request metadata edit attempt."""

    number: int
    title: str
    url: str
    message: str
