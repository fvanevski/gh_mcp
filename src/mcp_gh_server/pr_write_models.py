"""Canonical result models for pull-request writes."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .write_contracts import ExactWriteResult


class _PullRequestReviewSubmissionBase(ExactWriteResult):
    """Shared authoritative outcome fields for one formal pull-request review attempt."""

    number: int
    review_id: int = Field(ge=0)
    state: str
    body: str
    author: str | None = None
    submitted_at: str | None = None
    commit_sha: str
    url: str
    message: str


class PullRequestReviewSubmission(_PullRequestReviewSubmissionBase):
    """Internal review result whose action is selected by the guarded write implementation."""

    action: Literal["approve", "request_changes", "comment"]


class PullRequestApproval(_PullRequestReviewSubmissionBase):
    """Outcome of one action-specific GitHub APPROVED review attempt."""

    action: Literal["approve"] = "approve"


class PullRequestChangesRequested(_PullRequestReviewSubmissionBase):
    """Outcome of one action-specific GitHub CHANGES_REQUESTED review attempt."""

    action: Literal["request_changes"] = "request_changes"


class PullRequestCommentReview(_PullRequestReviewSubmissionBase):
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
