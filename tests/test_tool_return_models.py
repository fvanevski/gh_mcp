"""Regression snapshot for public MCP tool return annotations."""

from __future__ import annotations

from typing import Any, get_type_hints

from mcp_gh_server import server
from mcp_gh_server.models import (
    BranchCreate,
    BranchCreateFromSha,
    CommentCreate,
    CommitFilesResult,
    GitCommitInfo,
    GitRefInfo,
    IssueCreate,
    IssueEdit,
    IssueInfo,
    LabelCreate,
    LabelEdit,
    MilestoneCreate,
    PullRequestChecks,
    PullRequestCommitsPage,
    PullRequestCreate,
    PullRequestDiff,
    PullRequestEdit,
    PullRequestFilesPage,
    PullRequestInfo,
    PullRequestMerge,
    PullRequestReviewSubmission,
    ReleaseCreate,
    ReleaseInfo,
    RepoCreate,
    RepoInfo,
    RepositoryFile,
    SearchResults,
    ServerInfo,
    WorkflowInfo,
    WorkflowJobsPage,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunFailedLogs,
    WorkflowRunsPage,
    WorkflowRunWatchResult,
)

EXPECTED_RETURN_MODELS: dict[str, object] = {
    "gh_server_info": ServerInfo,
    "gh_info": dict[str, Any],
    "gh_search_repos": SearchResults,
    "gh_search_issues": SearchResults,
    "gh_search_code": SearchResults,
    "gh_list_issues": SearchResults,
    "gh_get_issue": IssueInfo,
    "gh_create_issue": IssueCreate,
    "gh_list_prs": SearchResults,
    "gh_get_pr": PullRequestInfo,
    "gh_get_pr_diff": PullRequestDiff,
    "gh_list_pr_files": PullRequestFilesPage,
    "gh_list_pr_commits": PullRequestCommitsPage,
    "gh_get_pr_checks": PullRequestChecks,
    "gh_submit_pr_review": PullRequestReviewSubmission,
    "gh_merge_pr": PullRequestMerge,
    "gh_create_pr": PullRequestCreate,
    "gh_get_repo": RepoInfo,
    "gh_list_repos": SearchResults,
    "gh_get_file_contents": RepositoryFile,
    "gh_get_ref": GitRefInfo,
    "gh_get_commit": GitCommitInfo,
    "gh_commit_files": CommitFilesResult,
    "gh_create_repo": RepoCreate,
    "gh_list_releases": SearchResults,
    "gh_get_release": ReleaseInfo,
    "gh_create_release": ReleaseCreate,
    "gh_list_workflows": SearchResults,
    "gh_get_workflow": WorkflowInfo,
    "gh_run_workflow": WorkflowRunCreate,
    "gh_list_runs": WorkflowRunsPage,
    "gh_get_run": WorkflowRun,
    "gh_list_run_jobs": WorkflowJobsPage,
    "gh_get_failed_run_logs": WorkflowRunFailedLogs,
    "gh_watch_run": WorkflowRunWatchResult,
    "gh_edit_issue": IssueEdit,
    "gh_list_labels": SearchResults,
    "gh_create_label": LabelCreate,
    "gh_upsert_label": LabelCreate,
    "gh_edit_label": LabelEdit,
    "gh_list_milestones": SearchResults,
    "gh_create_milestone": MilestoneCreate,
    "gh_create_comment": CommentCreate,
    "gh_create_branch": BranchCreate,
    "gh_create_branch_from_sha": BranchCreateFromSha,
    "gh_edit_pr": PullRequestEdit,
}


def test_exact_tool_return_models() -> None:
    assert len(EXPECTED_RETURN_MODELS) == 46

    for name, expected in EXPECTED_RETURN_MODELS.items():
        function = getattr(server, name)
        assert get_type_hints(function)["return"] == expected
