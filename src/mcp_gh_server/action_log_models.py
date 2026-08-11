"""Typed result models for bounded GitHub Actions log evidence."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class WorkflowRunLogs(BaseModel):
    """Bounded logs for one exact workflow-run attempt."""

    run_id: int = Field(ge=1)
    attempt: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: str
    conclusion: str | None = None
    url: str | None = None
    text: str
    truncated: bool
    bytes_returned: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warning: str | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> WorkflowRunLogs:
        actual_returned = len(self.text.encode("utf-8"))
        if self.bytes_returned != actual_returned:
            raise ValueError("bytes_returned must equal the UTF-8 byte length of text")
        if self.bytes_returned > self.total_bytes:
            raise ValueError("bytes_returned cannot exceed total_bytes")
        if self.truncated != (self.bytes_returned < self.total_bytes):
            raise ValueError("truncated must reflect whether returned bytes are incomplete")
        if self.truncated and not self.warning:
            raise ValueError("truncated log evidence requires an explicit warning")
        return self


class WorkflowJobLogs(WorkflowRunLogs):
    """Bounded logs for one exact job in one exact workflow-run attempt."""

    job_id: int = Field(ge=1)
