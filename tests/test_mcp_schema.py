"""Model validation tests."""

from __future__ import annotations

from mcp_gh_server.models import (
    IssueCreate,
    IssueInfo,
    PullRequestCheck,
    PullRequestChecks,
    PullRequestCreate,
    PullRequestDiff,
    PullRequestInfo,
    PullRequestMerge,
    PullRequestReviewSubmission,
    ReleaseInfo,
    RepoCreate,
    RepoInfo,
    SearchResults,
    ServerInfo,
    WorkflowInfo,
    WorkflowJob,
    WorkflowJobsPage,
    WorkflowJobStep,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunFailedLogs,
    WorkflowRunWatchResult,
)


class TestModels:
    """Test that Pydantic models validate correctly."""

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

    def test_server_info(self) -> None:
        info = ServerInfo(
            server_version="0.7.0",
            tool_schema_version="0.7.0",
            transport="streamable-http",
            tool_count=58,
            write_commands_enabled=False,
            content_commits_enabled=False,
            pr_merge_enabled=False,
        )
        assert info.server_name == "mcp-gh-server"
        assert info.server_version == "0.7.0"
        assert info.tool_schema_version == "0.7.0"
        assert info.tool_count == 58

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
            head_sha="a" * 40,
            base_sha="b" * 40,
            is_merged=False,
            is_draft=False,
        )
        assert pr.head_ref == "feature-branch"
        assert pr.additions == 0

    def test_pull_request_diff(self) -> None:
        result = PullRequestDiff(
            number=224,
            base_sha="a" * 40,
            head_sha="b" * 40,
            format="diff",
            content="diff --git a/a b/a",
            truncated=False,
            bytes_returned=20,
            total_bytes=20,
            sha256="c" * 64,
        )
        assert result.number == 224
        assert result.truncated is False

    def test_pull_request_review_submission(self) -> None:
        review = PullRequestReviewSubmission(
            number=224,
            review_id=91,
            action="approve",
            state="APPROVED",
            body="Reviewed.",
            commit_sha="a" * 40,
            url="https://github.com/test/repo/pull/224#pullrequestreview-91",
            message="Formal review submitted.",
        )
        assert review.state == "APPROVED"

    def test_pull_request_merge(self) -> None:
        merge = PullRequestMerge(
            number=224,
            method="squash",
            head_sha="a" * 40,
            state="MERGED",
            merged=True,
            merge_commit_sha="b" * 40,
            url="https://github.com/test/repo/pull/224",
            message="Merged.",
        )
        assert merge.merged is True

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

    def test_ci_diagnostic_models(self) -> None:
        head_sha = "a" * 40
        checks = PullRequestChecks(
            number=224,
            base_sha="b" * 40,
            head_sha=head_sha,
            total_count=1,
            truncated=False,
            checks=[PullRequestCheck(name="tests", state="FAILURE", bucket="fail")],
        )
        jobs = WorkflowJobsPage(
            run_id=123,
            attempt=1,
            head_sha=head_sha,
            page=1,
            per_page=30,
            total_count=1,
            has_more=False,
            jobs=[
                WorkflowJob(
                    id=456,
                    name="tests",
                    status="completed",
                    conclusion="failure",
                    steps=[
                        WorkflowJobStep(
                            number=1,
                            name="pytest",
                            status="completed",
                            conclusion="failure",
                        )
                    ],
                )
            ],
        )
        logs = WorkflowRunFailedLogs(
            run_id=123,
            attempt=1,
            head_sha=head_sha,
            status="completed",
            conclusion="failure",
            content="assertion failed",
            truncated=False,
            bytes_returned=16,
            total_bytes=16,
            sha256="c" * 64,
        )

        assert checks.checks[0].bucket == "fail"
        assert jobs.jobs[0].steps[0].name == "pytest"
        assert logs.sha256 == "c" * 64
