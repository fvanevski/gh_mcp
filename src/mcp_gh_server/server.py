"""MCP 2.0 composition root for GitHub CLI tool domains."""

from __future__ import annotations

from .tooling import AppContext, app_lifespan, mcp
from .tools.actions import (
    gh_get_failed_run_logs,
    gh_get_run,
    gh_get_workflow,
    gh_list_run_jobs,
    gh_list_runs,
    gh_list_workflows,
    gh_run_workflow,
    gh_watch_run,
)
from .tools.diagnostics import gh_info, gh_server_info
from .tools.discovery import gh_search_code, gh_search_issues, gh_search_repos
from .tools.git import gh_create_branch, gh_create_branch_from_sha
from .tools.issues import (
    gh_create_comment,
    gh_create_issue,
    gh_create_label,
    gh_create_milestone,
    gh_edit_issue,
    gh_edit_label,
    gh_get_issue,
    gh_list_issues,
    gh_list_labels,
    gh_list_milestones,
    gh_upsert_label,
)
from .tools.pull_requests import (
    gh_create_pr,
    gh_edit_pr,
    gh_get_pr,
    gh_get_pr_checks,
    gh_get_pr_diff,
    gh_list_pr_commits,
    gh_list_pr_files,
    gh_list_prs,
    gh_merge_pr,
    gh_submit_pr_review,
)
from .tools.releases import gh_create_release, gh_get_release, gh_list_releases
from .tools.repositories import (
    gh_commit_files,
    gh_create_repo,
    gh_get_file_contents,
    gh_get_repo,
    gh_list_repos,
)

__all__ = [
    "AppContext",
    "app_lifespan",
    "mcp",
    "gh_server_info",
    "gh_info",
    "gh_search_repos",
    "gh_search_issues",
    "gh_search_code",
    "gh_list_issues",
    "gh_get_issue",
    "gh_create_issue",
    "gh_list_prs",
    "gh_get_pr",
    "gh_get_pr_diff",
    "gh_list_pr_files",
    "gh_list_pr_commits",
    "gh_get_pr_checks",
    "gh_submit_pr_review",
    "gh_merge_pr",
    "gh_create_pr",
    "gh_get_repo",
    "gh_list_repos",
    "gh_get_file_contents",
    "gh_commit_files",
    "gh_create_repo",
    "gh_list_releases",
    "gh_get_release",
    "gh_create_release",
    "gh_list_workflows",
    "gh_get_workflow",
    "gh_run_workflow",
    "gh_list_runs",
    "gh_get_run",
    "gh_list_run_jobs",
    "gh_get_failed_run_logs",
    "gh_watch_run",
    "gh_edit_issue",
    "gh_list_labels",
    "gh_create_label",
    "gh_upsert_label",
    "gh_edit_label",
    "gh_list_milestones",
    "gh_create_milestone",
    "gh_create_comment",
    "gh_create_branch",
    "gh_create_branch_from_sha",
    "gh_edit_pr",
]
