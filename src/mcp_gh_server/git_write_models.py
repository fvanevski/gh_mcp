"""Canonical exact-outcome models for Git reference and content writes."""

from __future__ import annotations

from pydantic import Field

from .write_contracts import ExactWriteResult


class BranchCreate(ExactWriteResult):
    """Result of creating and linking one issue development branch."""

    name: str
    ref: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    linked_branch_id: str | None = None
    created: bool
    message: str


class BranchCreateFromSha(ExactWriteResult):
    """Result of creating one branch at an exact commit SHA."""

    name: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str
    created: bool
    message: str


class CommitFilesResult(ExactWriteResult):
    """Result of one exact-head atomic repository content commit."""

    branch: str
    previous_head_sha: str
    commit_sha: str | None = None
    tree_sha: str | None = None
    ref_updated: bool | None = False
    observed_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    readback_attempts: int = Field(default=0, ge=0)
    files_committed: int = 0
    url: str = ""
    message: str
