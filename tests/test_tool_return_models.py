"""Regression snapshot for public MCP tool return annotations."""

from __future__ import annotations

from typing import Any, get_type_hints

from mcp_gh_server import server
from mcp_gh_server.action_log_models import WorkflowJobLogs, WorkflowRunLogs
from mcp_gh_server.artifact_content_models import ArtifactFileContent, ArtifactFilesPage
from mcp_gh_server.compare_commits_models import CommitComparisonResult
from mcp_gh_server.git_write_models import BranchCreate, BranchCreateFromSha, CommitFilesResult
from mcp_gh_server.issue_state_models import IssueStateTransitionResult
from mcp_gh_server.issue_write_models import (
    IssueCreateResult,
    IssueEditResult,
    LabelCreateResult,
    LabelEditResult,
    MilestoneCreateResult,
)
from mcp_gh_server.merge_requirements_models import PullRequestMergeRequirements
from mcp_gh_server.models import (
    CommentCreate,
    GitCommitInfo,
    GitRefInfo,
    IssueInfo,
    PullRequestChecks,
    PullRequestCommitsPage,
    PullRequestDiff,
    PullRequestFilesPage,
    PullRequestInfo,
    ReleaseInfo,
    RepoInfo,
    RepositoryFile,
    SearchResults,
    ServerInfo,
    WorkflowArtifact,
    WorkflowArtifactsPage,
    WorkflowInfo,
    WorkflowJobsPage,
    WorkflowRun,
    WorkflowRunFailedLogs,
    WorkflowRunsPage,
    WorkflowRunWatchResult,
)
from mcp_gh_server.pr_draft_state_models import PullRequestDraftStateTransitionResult
from mcp_gh_server.pr_review_eligibility_models import PullRequestReviewEligibility
from mcp_gh_server.pr_review_models import PullRequestReviewsPage, PullRequestReviewState
from mcp_gh_server.pr_write_models import (
    PullRequestApproval,
    PullRequestChangesRequested,
    PullRequestCommentReview,
    PullRequestCreate,
    PullRequestEdit,
    PullRequestMerge,
)
from mcp_gh_server.rate_status_models import ApiRateStatus
from mcp_gh_server.release_exact_models import ReleaseExactResult
from mcp_gh_server.repository_create_models import RepositoryCreateResult
from mcp_gh_server.repository_tree_models import RepositoryTreeResult
from mcp_gh_server.workflow_dispatch_models import WorkflowDispatchExactResult

EXPECTED_RETURN_MODELS: dict[str, object] = {
    "gh_server_info": ServerInfo,
    "gh_info": dict[str, Any],
    "gh_get_api_rate_status": ApiRateStatus,
    "gh_search_repos": SearchResults,
    "gh_search_issues": SearchResults,
    "gh_search_code": SearchResults,
    "gh_list_issues": SearchResults,
    "gh_get_issue": IssueInfo,
    "gh_create_issue": IssueCreateResult,
    "gh_set_issue_state": IssueStateTransitionResult,
    "gh_set_pr_draft_state": PullRequestDraftStateTransitionResult,
    "gh_list_prs": SearchResults,
    "gh_get_pr": PullRequestInfo,
    "gh_get_pr_diff": PullRequestDiff,
    "gh_list_pr_files": PullRequestFilesPage,
    "gh_list_pr_commits": PullRequestCommitsPage,
    "gh_list_pr_reviews": PullRequestReviewsPage,
    "gh_get_pr_review_state": PullRequestReviewState,
    "gh_get_pr_review_eligibility": PullRequestReviewEligibility,
    "gh_get_merge_requirements": PullRequestMergeRequirements,
    "gh_get_pr_checks": PullRequestChecks,
    "gh_approve_pr": PullRequestApproval,
    "gh_request_pr_changes": PullRequestChangesRequested,
    "gh_comment_pr_review": PullRequestCommentReview,
    "gh_merge_pr": PullRequestMerge,
    "gh_create_pr": PullRequestCreate,
    "gh_get_repo": RepoInfo,
    "gh_list_repos": SearchResults,
    "gh_get_file_contents": RepositoryFile,
    "gh_list_repository_tree": RepositoryTreeResult,
    "gh_get_ref": GitRefInfo,
    "gh_get_commit": GitCommitInfo,
    "gh_compare_commits": CommitComparisonResult,
    "gh_commit_files": CommitFilesResult,
    "gh_create_repo": RepositoryCreateResult,
    "gh_list_releases": SearchResults,
    "gh_get_release": ReleaseInfo,
    "gh_create_release_exact": ReleaseExactResult,
    "gh_list_workflows": SearchResults,
    "gh_get_workflow": WorkflowInfo,
    "gh_run_workflow_exact": WorkflowDispatchExactResult,
    "gh_list_runs": WorkflowRunsPage,
    "gh_get_run": WorkflowRun,
    "gh_list_run_artifacts": WorkflowArtifactsPage,
    "gh_get_artifact": WorkflowArtifact,
    "gh_list_artifact_files": ArtifactFilesPage,
    "gh_read_artifact_file": ArtifactFileContent,
    "gh_list_run_jobs": WorkflowJobsPage,
    "gh_get_failed_run_logs": WorkflowRunFailedLogs,
    "gh_get_job_logs": WorkflowJobLogs,
    "gh_get_run_logs": WorkflowRunLogs,
    "gh_watch_run": WorkflowRunWatchResult,
    "gh_edit_issue": IssueEditResult,
    "gh_list_labels": SearchResults,
    "gh_create_label": LabelCreateResult,
    "gh_edit_label": LabelEditResult,
    "gh_list_milestones": SearchResults,
    "gh_create_milestone": MilestoneCreateResult,
    "gh_create_comment": CommentCreate,
    "gh_create_branch": BranchCreate,
    "gh_create_branch_from_sha": BranchCreateFromSha,
    "gh_edit_pr": PullRequestEdit,
}


def test_exact_tool_return_models() -> None:
    assert len(EXPECTED_RETURN_MODELS) == 62
    for name, expected in EXPECTED_RETURN_MODELS.items():
        function = getattr(server, name)
        assert get_type_hints(function)["return"] == expected
