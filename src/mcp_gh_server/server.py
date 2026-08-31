"MCP 2.0 composition root for GitHub CLI tool domains."

from __future__ import annotations

from .current_write_tool_schema import (
    PUBLIC_WRITE_TOOLS,
    WRITE_TOOL_METADATA,
    gh_approve_pr,
    gh_comment_pr_review,
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
    gh_patch_files,
    gh_request_pr_changes,
    gh_run_workflow_exact,
    gh_set_issue_state,
    gh_set_pr_draft_state,
)
from .tooling import AppContext, app_lifespan, mcp
from .tools.action_logs import gh_get_job_logs, gh_get_run_logs
from .tools.actions import (
    gh_get_failed_run_logs,
    gh_get_run,
    gh_get_workflow,
    gh_list_run_jobs,
    gh_list_runs,
    gh_list_workflows,
    gh_watch_run,
)
from .tools.artifact_content import gh_list_artifact_files, gh_read_artifact_file
from .tools.artifacts import gh_get_artifact, gh_list_run_artifacts
from .tools.compare_commits import gh_compare_commits
from .tools.diagnostics import gh_get_api_rate_status, gh_info, gh_server_info
from .tools.discovery import gh_search_code, gh_search_issues, gh_search_repos
from .tools.git import gh_get_commit, gh_get_ref
from .tools.issues import (
    gh_get_issue,
    gh_list_issues,
    gh_list_labels,
    gh_list_milestones,
)
from .tools.merge_requirements import gh_get_merge_requirements
from .tools.pr_review_eligibility import gh_get_pr_review_eligibility
from .tools.pr_reviews import gh_get_pr_review_state, gh_get_pr_review_thread, gh_list_pr_reviews
from .tools.pull_requests import (
    gh_get_pr,
    gh_get_pr_checks,
    gh_get_pr_diff,
    gh_list_pr_commits,
    gh_list_pr_files,
    gh_list_prs,
)
from .tools.releases import gh_get_release, gh_list_releases
from .tools.repositories import (
    gh_get_file_contents,
    gh_get_repo,
    gh_list_repos,
    gh_list_repository_tree,
)

# Public writes are registered exactly once from the canonical current host-facing
# schema. Domain implementation modules do not self-register these names.
for _facade in PUBLIC_WRITE_TOOLS:
    _metadata = WRITE_TOOL_METADATA[_facade.__name__]
    mcp.add_tool(
        _facade,
        title=_metadata.title,
        description=_metadata.description,
        annotations=_metadata.annotations,
    )

__all__ = [
    "AppContext",
    "app_lifespan",
    "gh_approve_pr",
    "gh_comment_pr_review",
    "gh_commit_files",
    "gh_compare_commits",
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
    "gh_get_api_rate_status",
    "gh_get_artifact",
    "gh_get_commit",
    "gh_get_failed_run_logs",
    "gh_get_file_contents",
    "gh_get_issue",
    "gh_get_job_logs",
    "gh_get_merge_requirements",
    "gh_get_pr",
    "gh_get_pr_checks",
    "gh_get_pr_diff",
    "gh_get_pr_review_eligibility",
    "gh_get_pr_review_state",
    "gh_get_pr_review_thread",
    "gh_get_ref",
    "gh_get_release",
    "gh_get_repo",
    "gh_get_run",
    "gh_get_run_logs",
    "gh_get_workflow",
    "gh_info",
    "gh_list_artifact_files",
    "gh_list_issues",
    "gh_list_labels",
    "gh_list_milestones",
    "gh_list_pr_commits",
    "gh_list_pr_files",
    "gh_list_pr_reviews",
    "gh_list_prs",
    "gh_list_releases",
    "gh_list_repos",
    "gh_list_repository_tree",
    "gh_list_run_artifacts",
    "gh_list_run_jobs",
    "gh_list_runs",
    "gh_list_workflows",
    "gh_merge_pr",
    "gh_patch_files",
    "gh_read_artifact_file",
    "gh_request_pr_changes",
    "gh_run_workflow_exact",
    "gh_search_code",
    "gh_search_issues",
    "gh_search_repos",
    "gh_server_info",
    "gh_set_issue_state",
    "gh_set_pr_draft_state",
    "gh_watch_run",
    "mcp",
]
