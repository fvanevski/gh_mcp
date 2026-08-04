"""Model validation tests."""

from __future__ import annotations

from mcp_gh_server.models import (
    CommandApproval,
    IssueCreate,
    IssueInfo,
    PullRequestCreate,
    PullRequestInfo,
    ReleaseInfo,
    RepoCreate,
    RepoInfo,
    SearchResults,
    WorkflowInfo,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunWatchResult,
)


class TestModels:
    """Test that Pydantic models validate correctly."""

    def test_command_approval_approved(self) -> None:
        approval = CommandApproval(approved=True)
        assert approval.approved is True

    def test_command_approval_denied(self) -> None:
        approval = CommandApproval(approved=False)
        assert approval.approved is False

    def test_search_results(self) -> None:
        results = SearchResults(
            total_count=10,
            items=[{"title": "test", "url": "https://example.com"}],
            truncated=False,
            query="test",
        )
        assert results.total_count == 10
        assert len(results.items) == 1
        assert results.truncated is False

    def test_issue_info(self) -> None:
        issue = IssueInfo(
            number=42,
            title="Test issue",
            state="open",
            url="https://github.com/test/repo/issues/42",
        )
        assert issue.number == 42
        assert issue.labels == []
        assert issue.comments == 0

    def test_pull_request_info(self) -> None:
        pr = PullRequestInfo(
            number=100,
            title="Test PR",
            state="open",
            url="https://github.com/test/repo/pull/100",
            head_ref="feature-branch",
            base_ref="main",
            is_merged=False,
            is_draft=False,
        )
        assert pr.head_ref == "feature-branch"
        assert pr.additions == 0

    def test_repo_info(self) -> None:
        repo = RepoInfo(
            nameWithOwner="test/repo",
            name="repo",
            owner="test",
            description="Test repo",
            url="https://github.com/test/repo",
            isPrivate=False,
            isFork=False,
            stargazerCount=100,
            forkCount=50,
            primaryLanguage="Python",
            createdAt="2023-01-01T00:00:00Z",
            updatedAt="2023-01-02T00:00:00Z",
            defaultBranchRef="main",
            licenseInfo={"key": "mit"},
        )
        assert repo.name_with_owner == "test/repo"
        assert repo.name == "repo"
        assert repo.owner == "test"
        assert repo.is_private is False

    def test_issue_create(self) -> None:
        create = IssueCreate(
            number=42,
            title="New issue",
            url="https://github.com/test/repo/issues/42",
            message="Issue created successfully.",
        )
        assert create.number == 42

    def test_pull_request_create(self) -> None:
        create = PullRequestCreate(
            number=100,
            title="New PR",
            url="https://github.com/test/repo/pull/100",
            message="Pull request created successfully.",
        )
        assert create.number == 100

    def test_repo_create(self) -> None:
        repo = RepoCreate(
            name="test/new-repo",
            url="https://github.com/test/new-repo",
            message="Repository created successfully.",
        )
        assert repo.name == "test/new-repo"

    def test_release_info(self) -> None:
        release = ReleaseInfo(
            tag_name="v1.0.0",
            name="Version 1.0.0",
            url="https://github.com/test/repo/releases/tag/v1.0.0",
        )
        assert release.is_draft is False
        assert release.is_prerelease is False

    def test_workflow_info(self) -> None:
        workflow = WorkflowInfo(
            id=12345,
            name="CI",
            path=".github/workflows/ci.yml",
            state="active",
        )
        assert workflow.id == 12345
        assert workflow.state == "active"

    def test_workflow_run(self) -> None:
        run = WorkflowRun(
            id=67890,
            name="Build",
            display_title="Build #123",
            head_branch="main",
            head_sha="abc123",
            status="completed",
            conclusion="success",
            event="push",
            url="https://github.com/test/repo/actions/runs/67890",
            workflow_name="CI",
        )
        assert run.id == 67890
        assert run.conclusion == "success"

    def test_workflow_run_create(self) -> None:
        create = WorkflowRunCreate(
            run_id=67890,
            url="https://github.com/test/repo/actions/runs/67890",
            message="Workflow dispatch triggered successfully.",
        )
        assert create.run_id == 67890

    def test_workflow_run_watch_result(self) -> None:
        result = WorkflowRunWatchResult(
            run_id=67890,
            conclusion="success",
            status="completed",
            url="https://github.com/test/repo/actions/runs/67890",
            message="Run #67890 completed with conclusion: success",
        )
        assert result.run_id == 67890
        assert result.conclusion == "success"
