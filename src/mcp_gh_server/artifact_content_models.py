"""Typed result models for bounded GitHub Actions artifact-content evidence."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ArtifactArchiveEvidence(BaseModel):
    """Immutable artifact identity plus the exact downloaded archive fingerprint."""

    artifact_id: int = Field(ge=1)
    artifact_name: str = Field(min_length=1)
    artifact_size_in_bytes: int = Field(ge=0)
    artifact_digest: str | None = None
    artifact_expires_at: str
    workflow_run_id: int = Field(ge=1)
    workflow_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    archive_bytes: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactFileEntry(BaseModel):
    """One normalized regular-file entry in a validated artifact ZIP archive."""

    path: str = Field(min_length=1)
    size_in_bytes: int = Field(ge=0)
    compressed_size_in_bytes: int = Field(ge=0)


class ArtifactFilesPage(ArtifactArchiveEvidence):
    """One bounded page of normalized artifact file metadata."""

    total_count: int = Field(ge=0)
    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    has_more: bool
    truncated: bool
    warning: str | None = None
    files: list[ArtifactFileEntry]


class ArtifactFileContent(ArtifactArchiveEvidence):
    """Bounded UTF-8 contents for one exact normalized artifact file."""

    path: str = Field(min_length=1)
    encoding: str = Field(pattern=r"^utf-8$")
    content: str
    bytes_returned: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    truncated: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warning: str | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> ArtifactFileContent:
        actual_returned = len(self.content.encode("utf-8"))
        if self.bytes_returned != actual_returned:
            raise ValueError("bytes_returned must equal the UTF-8 byte length of content")
        if self.bytes_returned > self.total_bytes:
            raise ValueError("bytes_returned cannot exceed total_bytes")
        if self.truncated != (self.bytes_returned < self.total_bytes):
            raise ValueError("truncated must reflect whether returned bytes are incomplete")
        if self.truncated and not self.warning:
            raise ValueError("truncated artifact-file evidence requires an explicit warning")
        return self
