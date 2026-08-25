"""Canonical current public write-tool surface.

Non-review writes come from ``write_tool_schema`` plus the exact-context patch facade.
Formal pull-request review authority is defined only by the action-specific review
facade in this module's composed public inventory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .patch_write_schema import PATCH_WRITE_METADATA, gh_patch_files
from .pr_review_tool_schema import (
    PR_REVIEW_WRITE_METADATA,
    PR_REVIEW_WRITE_TOOLS,
    gh_approve_pr,
    gh_comment_pr_review,
    gh_request_pr_changes,
)
from .write_tool_schema import (
    WRITE_TOOL_METADATA as _NON_REVIEW_WRITE_METADATA,
)
from .write_tool_schema import (
    WriteToolMetadata,
    gh_commit_files,
    gh_create_branch,
    gh_create_branch_from_sha,
    gh_create_comment,
    gh_create_issue,
    gh_create_label,
    gh_create_milestone,
    gh_create_pr,
    gh_create_release_exact,
    gh_create_repo,
    gh_edit_issue,
    gh_edit_label,
    gh_edit_pr,
    gh_merge_pr,
    gh_run_workflow_exact,
    gh_set_issue_state,
    gh_set_pr_draft_state,
)

PublicWriteTool = Callable[..., Awaitable[object]]
_NON_REVIEW_WRITE_TOOLS: tuple[PublicWriteTool, ...] = (
    gh_create_issue,
    gh_edit_issue,
    gh_set_issue_state,
    gh_create_label,
    gh_edit_label,
    gh_create_milestone,
    gh_create_comment,
    gh_create_pr,
    gh_edit_pr,
    gh_set_pr_draft_state,
    gh_merge_pr,
    gh_create_repo,
    gh_commit_files,
    gh_patch_files,
    gh_create_release_exact,
    gh_run_workflow_exact,
    gh_create_branch,
    gh_create_branch_from_sha,
)
PUBLIC_WRITE_TOOLS: tuple[PublicWriteTool, ...] = (
    *_NON_REVIEW_WRITE_TOOLS,
    *PR_REVIEW_WRITE_TOOLS,
)

WRITE_TOOL_METADATA: dict[str, WriteToolMetadata] = {
    tool.__name__: _NON_REVIEW_WRITE_METADATA[tool.__name__]
    for tool in _NON_REVIEW_WRITE_TOOLS
    if tool.__name__ != "gh_patch_files"
}
WRITE_TOOL_METADATA["gh_patch_files"] = PATCH_WRITE_METADATA
WRITE_TOOL_METADATA.update(PR_REVIEW_WRITE_METADATA)

__all__ = [
    "PUBLIC_WRITE_TOOLS",
    "WRITE_TOOL_METADATA",
    "gh_approve_pr",
    "gh_comment_pr_review",
    "gh_commit_files",
    "gh_create_branch",
    "gh_create_branch_from_sha",
    "gh_create_comment",
    "gh_create_issue",
    "gh_create_label",
    "gh_create_milestone",
    "gh_create_pr",
    "gh_create_release_exact",
    "gh_create_repo",
    "gh_edit_issue",
    "gh_edit_label",
    "gh_edit_pr",
    "gh_merge_pr",
    "gh_patch_files",
    "gh_request_pr_changes",
    "gh_run_workflow_exact",
    "gh_set_issue_state",
    "gh_set_pr_draft_state",
]
