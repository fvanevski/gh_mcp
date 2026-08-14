"""Canonical result models for issue-domain writes."""

from __future__ import annotations

from pydantic import Field

from .write_contracts import ExactWriteResult


class IssueCreateResult(ExactWriteResult):
    """Authoritative outcome of one issue creation attempt."""

    number: int = Field(ge=0)
    title: str
    url: str
    message: str


class IssueEditResult(ExactWriteResult):
    """Authoritative outcome of one issue metadata edit attempt."""

    number: int = Field(ge=1)
    title: str
    state: str
    url: str
    message: str


class LabelCreateResult(ExactWriteResult):
    """Authoritative outcome of one label creation attempt."""

    name: str
    color: str = ""
    description: str | None = None
    url: str
    message: str


class LabelEditResult(ExactWriteResult):
    """Authoritative outcome of one label edit attempt."""

    name: str
    color: str = ""
    description: str | None = None
    url: str
    message: str


class MilestoneCreateResult(ExactWriteResult):
    """Authoritative outcome of one milestone creation attempt."""

    number: int = Field(ge=0)
    title: str
    url: str
    message: str
