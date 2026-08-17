"""Canonical current public write-tool surface.

The 0.8.x facade remains the implementation source for non-review writes. Formal
pull-request review authority is replaced here rather than retained as a public alias.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .pr_review_tool_schema import (
    PR_REVIEW_WRITE_METADATA,
    PR_REVIEW_WRITE_TOOLS,
    gh_approve_pr,
    gh_comment_pr_review,
    gh_request_pr_changes,
)
from .write_tool_schema import (
    PUBLIC_WRITE_TOOLS as _PRE_REVIEW_SPLIT_TOOLS,
    WRITE_TOOL_METADATA as _PRE_REVIEW_SPLIT_METADATA,
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

_RETIRED_PUBLIC_REVIEW_TOOL = "gh_submit_pr_review"

PublicWriteTool = Callable[..., Awaitable[object]]
_NON_REVIEW_WRITE_TOOLS: tuple[PublicWriteTool, ...] = tuple(
    tool
    for tool in _PRE_REVIEW_SPLIT_TOOLS
    if tool.__name__ != _RETIRED_PUBLIC_REVIEW_TOOL
)
PUBLIC_WRITE_TOOLS: tuple[PublicWriteTool, ...] = (
    *_NON_REVIEW_WRITE_TOOLS,
    *PR_REVIEW_WRITE_TOOLS,
)

WRITE_TOOL_METADATA: dict[str, WriteToolMetadata] = {
    tool.__name__: _PRE_REVIEW_SPLIT_METADATA[tool.__name__]
    for tool in _NON_REVIEW_WRITE_TOOLS
}
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
    "gh_request_pr_changes",
    "gh_run_workflow_exact",
    "gh_set_issue_state",
    "gh_set_pr_draft_state",
]
