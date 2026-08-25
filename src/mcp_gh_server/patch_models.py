"Exact-context repository patch input and result models."

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .write_contracts import ExactWriteResult


class PatchEdit(BaseModel):
    """One exact-context replacement resolved against an original file snapshot."""

    old_text: str = Field(
        description="Exact non-empty UTF-8 text that must occur exactly once in the original file.",
        min_length=1,
        max_length=5_000_000,
    )
    new_text: str = Field(
        description="Replacement UTF-8 text; may be empty to remove the matched text.",
        max_length=5_000_000,
    )


class FilePatch(BaseModel):
    """One existing UTF-8 repository file plus one or more exact-context edits."""

    path: str = Field(
        description="Repository-relative path of an existing regular or executable text file.",
        min_length=1,
        max_length=4096,
    )
    edits: list[PatchEdit] = Field(min_length=1, max_length=1000)


class PatchFileEvidence(BaseModel):
    """Immutable before/after blob evidence for one materialized patch target."""

    path: str
    mode: Literal["100644", "100755"]
    before_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    after_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class PatchFilesResult(ExactWriteResult):
    """Result of one exact-context multi-file patch commit."""

    branch: str
    previous_head_sha: str
    commit_sha: str | None = None
    tree_sha: str | None = None
    ref_updated: bool | None = False
    observed_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    readback_attempts: int = Field(default=0, ge=0)
    changed_file_count: int = Field(default=0, ge=0)
    applied_edit_count: int = Field(default=0, ge=0)
    changed_paths: list[str] = Field(default_factory=list)
    files: list[PatchFileEvidence] = Field(default_factory=list)
    url: str = ""
    message: str
