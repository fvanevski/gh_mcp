"""Typed models for exact bounded commit-comparison evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ComparisonStatus = Literal["identical", "ahead", "behind", "diverged"]


class ComparedCommit(BaseModel):
    """One bounded commit record returned by an exact comparison."""

    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    message: str
    message_truncated: bool = False
    message_bytes_returned: int = Field(default=0, ge=0)
    message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    author_login: str | None = None
    author_name: str | None = None
    authored_at: str | None = None
    committer_login: str | None = None
    committer_name: str | None = None
    committed_at: str | None = None
    url: str


class ComparedFile(BaseModel):
    """One changed-file metadata record; patch bodies are intentionally excluded."""

    filename: str
    status: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changes: int = Field(ge=0)
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    previous_filename: str | None = None
    blob_url: str | None = None
    raw_url: str | None = None
    contents_url: str | None = None


class ComparisonCollectionEvidence(BaseModel):
    """Completeness and digest metadata for one independently bounded collection."""

    returned_count: int = Field(ge=0)
    total_count: int | None = Field(default=None, ge=0)
    complete: bool
    truncated: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warning: str | None = None

    @model_validator(mode="after")
    def validate_completeness(self) -> ComparisonCollectionEvidence:
        if self.total_count is not None and self.returned_count > self.total_count:
            raise ValueError("returned_count cannot exceed total_count")
        if self.truncated and self.complete:
            raise ValueError("truncated collection evidence cannot be complete")
        if (self.truncated or not self.complete) and not self.warning:
            raise ValueError("incomplete collection evidence requires an explicit warning")
        return self


class CommitComparisonResult(BaseModel):
    """Exact immutable comparison identity plus independently bounded evidence."""

    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_found: bool
    head_found: bool
    comparison_available: bool
    merge_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    status: ComparisonStatus | None = None
    ahead_by: int | None = Field(default=None, ge=0)
    behind_by: int | None = Field(default=None, ge=0)
    total_commits: int | None = Field(default=None, ge=0)
    commits: list[ComparedCommit]
    commits_evidence: ComparisonCollectionEvidence
    files: list[ComparedFile]
    files_evidence: ComparisonCollectionEvidence
    truncated: bool
    evidence_complete: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warning: str | None = None

    @model_validator(mode="after")
    def validate_comparison_state(self) -> CommitComparisonResult:
        if len(self.commits) != self.commits_evidence.returned_count:
            raise ValueError("commits_evidence.returned_count must match commits")
        if len(self.files) != self.files_evidence.returned_count:
            raise ValueError("files_evidence.returned_count must match files")
        if self.truncated != (self.commits_evidence.truncated or self.files_evidence.truncated):
            raise ValueError("truncated must reflect independently bounded collections")

        expected_complete = (
            self.comparison_available
            and self.commits_evidence.complete
            and self.files_evidence.complete
        )
        if self.evidence_complete != expected_complete:
            raise ValueError("evidence_complete conflicts with comparison completeness")

        comparison_fields = (
            self.merge_base_sha,
            self.status,
            self.ahead_by,
            self.behind_by,
            self.total_commits,
        )
        if self.comparison_available:
            if not (self.base_found and self.head_found):
                raise ValueError("available comparison requires both exact commits")
            if any(value is None for value in comparison_fields):
                raise ValueError("available comparison requires complete identity/count fields")
            if self.commits_evidence.total_count != self.total_commits:
                raise ValueError("commit evidence total must equal total_commits")
        else:
            if self.base_found and self.head_found:
                raise ValueError("unavailable comparison must identify a missing exact commit")
            if any(value is not None for value in comparison_fields):
                raise ValueError("missing-commit result cannot report comparison facts")
            if self.commits or self.files:
                raise ValueError("missing-commit result cannot report comparison collections")
            if self.evidence_complete:
                raise ValueError("missing-commit result cannot claim complete comparison evidence")
            if not self.warning:
                raise ValueError("missing-commit result requires an explicit warning")
        return self
