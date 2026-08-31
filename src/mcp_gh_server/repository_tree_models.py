"""Typed request and result models for exact repository-tree reads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RepositoryTreeObjectType = Literal["blob", "tree", "commit"]
RepositoryTreeMode = Literal["100644", "100755", "120000", "040000", "160000"]


class RepositoryTreeRequest(BaseModel):
    """Validated request for one exact repository directory tree."""

    owner: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
    )
    repo: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_.-]{1,100}$",
    )
    commit_sha: str = Field(
        description="Exact 40-character hexadecimal Git commit SHA.",
        pattern=r"^[0-9A-Fa-f]{40}$",
    )
    path: str = Field(
        default="",
        description="Normalized repository-relative directory path; empty means repository root.",
        max_length=4096,
    )
    recursive: bool = False
    max_entries: int | None = Field(
        default=None,
        ge=1,
        description="Requested result-entry cap, additionally bounded by server policy.",
    )


class RepositoryTreeEntry(BaseModel):
    """One exact Git object entry in a repository tree listing."""

    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1)
    type: RepositoryTreeObjectType
    mode: RepositoryTreeMode
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    size: int | None = Field(default=None, ge=0)


class RepositoryTreeResult(BaseModel):
    """Bounded repository-tree evidence pinned to one exact commit and directory tree."""

    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    root_tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    directory_tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    recursive: bool
    entries: list[RepositoryTreeEntry]
    entries_returned: int = Field(ge=0)
    truncated: bool
    evidence_complete: bool
    warning: str | None = None
