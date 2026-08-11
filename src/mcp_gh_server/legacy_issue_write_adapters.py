"""Backward-compatible issue-domain write re-exports."""

from .legacy_comment_write_adapter import gh_create_comment
from .legacy_issue_core_write_adapter import gh_create_issue, gh_edit_issue
from .legacy_label_write_adapter import gh_create_label, gh_edit_label, gh_upsert_label
from .legacy_milestone_write_adapter import gh_create_milestone

__all__ = [
    "gh_create_comment",
    "gh_create_issue",
    "gh_create_label",
    "gh_create_milestone",
    "gh_edit_issue",
    "gh_edit_label",
    "gh_upsert_label",
]
