"""Typed result model for exact workflow-dispatch writes."""

from __future__ import annotations

from pydantic import Field

from .write_contracts import ExactWriteResult


class WorkflowDispatchExactResult(ExactWriteResult):
    """Authoritative outcome of one exact-ref guarded workflow dispatch."""

    workflow_id: int = Field(ge=1)
    ref: str = Field(pattern=r"^(?:heads|tags)/.+$")
    expected_ref_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    resolved_ref_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: int | None = Field(default=None, ge=1)
    run_url: str | None = None
    run_status: str | None = None
    run_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
