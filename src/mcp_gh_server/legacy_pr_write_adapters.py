"""Backward-compatible pull-request write re-exports."""

from .legacy_pr_merge_write_adapter import gh_merge_pr
from .legacy_pr_metadata_write_adapter import gh_create_pr, gh_edit_pr
from .legacy_pr_review_write_adapter import gh_submit_pr_review

__all__ = ["gh_create_pr", "gh_edit_pr", "gh_merge_pr", "gh_submit_pr_review"]
