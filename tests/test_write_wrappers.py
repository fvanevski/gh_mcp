"""Regression tests for exact write-command execution and structured readback."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import (
    AppContext,
    gh_commit_files,
    gh_create_branch,
    gh_create_branch_from_sha,
    gh_create_issue,
    gh_create_label,
    gh_create_milestone,
    gh_create_pr,
    gh_edit_issue,
    gh_edit_label,
    gh_edit_pr,
    gh_get_failed_run_logs,
    gh_get_file_contents,
    gh_get_pr,
    gh_get_pr_checks,
    gh_get_pr_diff,
    gh_list_milestones,
    gh_list_pr_commits,
    gh_list_pr_files,
    gh_list_run_jobs,
    gh_merge_pr,
    gh_run_workflow_exact,
    gh_server_info,
    gh_watch_run,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.write_tool_schema import PublicCommitFile, gh_submit_pr_review


@dataclass
class FakeGhClient:
    """Record gh invocations and return queued results."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if "--input" in args:
            payload_path = Path(args[args.index("--input") + 1])
            self.payloads.append(json.loads(payload_path.read_text()))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        result = await self.run(*args, **kwargs)
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(client: FakeGhClient) -> Any:
    settings = Settings(
        allow_write_commands=True,
        allow_repo_creation=True,
        allow_release_creation=True,
        allow_workflow_dispatch=True,
        allow_content_commits=True,
        allow_pr_merge=True,
    )
    app = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=app),
    )


@pytest.mark.asyncio
async def test_server_info_is_local_bounded_and_subprocess_free() -> None:
    client = FakeGhClient([])

    result = await gh_server_info(ctx=_context(client))

    assert result.server_name == "mcp-gh-server"
    assert result.server_version == "0.9.0"
    assert result.tool_schema_version == "0.9.0"
    assert result.transport == "stdio"
    assert result.tool_count == 62
    assert result.write_commands_enabled is True
    assert result.content_commits_enabled is True
    assert result.pr_merge_enabled is True
    assert result.repo_creation_enabled is True
    assert result.release_creation_enabled is True
    assert result.workflow_dispatch_enabled is True
    assert client.calls == []


@pytest.mark.asyncio
async def test_get_pr_uses_one_explicit_get_and_returns_typed_bounded_snapshot() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    client = FakeGhClient(
        [
            {
                "number": 224,
                "title": "Strict connector contract",
                "state": "open",
                "html_url": "https://github.com/octo/repo/pull/224",
                "user": {"login": "author"},
                "created_at": "2026-08-04T17:00:00Z",
                "updated_at": "2026-08-04T17:30:00Z",
                "closed_at": None,
                "labels": [{"name": "review"}, {"name": "mcp"}, {"color": "fff"}],
                "comments": 2,
                "review_comments": 3,
                "head": {"ref": "feature", "sha": head_sha},
                "base": {"ref": "main", "sha": base_sha},
                "draft": False,
                "additions": 10,
                "deletions": 4,
                "changed_files": 2,
            }
        ]
    )

    result = await gh_get_pr("octo", "repo", 224, ctx=_context(client))

    assert client.calls == [(("api", "repos/octo/repo/pulls/224", "-X", "GET"), {})]
    assert result.head_sha == head_sha
    assert result.base_sha == base_sha
    assert result.labels == ["review", "mcp"]
    assert result.comments == 5


@pytest.mark.asyncio
async def test_get_pr_rejects_invalid_repository_before_client_execution() -> None:
    client = FakeGhClient([])

    with pytest.raises(ValueError, match="canonical GitHub names"):
        await gh_get_pr("octo", "../repo", 224, ctx=_context(client))

    assert client.calls == []


@pytest.mark.asyncio
async def test_create_issue_executes_then_reads_url() -> None:
    url = "https://github.com/octo/repo/issues/42"
    client = FakeGhClient(
        [
            {"stdout": f"{url}\n"},
            {"number": 42, "title": "Fix wrappers", "url": url},
        ]
    )

    result = await gh_create_issue(
        "octo",
        "repo",
        "Fix wrappers",
        ctx=_context(client),
    )

    assert result.number == 42
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    create_args, create_kwargs = client.calls[0]
    assert create_args[:2] == ("issue", "create")
    assert "--json" not in create_args
    assert create_kwargs == {"json_output": False, "stdin_text": ""}
    assert create_args[-2:] == ("--body-file", "-")
    assert client.calls[1][0] == (
        "issue",
        "view",
        url,
        "--repo",
        "octo/repo",
        "--json",
        "title,number,url",
    )


@pytest.mark.asyncio
async def test_create_label_executes_then_reads_exact_label() -> None:
    client = FakeGhClient(
        [
            {"stdout": ""},
            {
                "name": "needs triage",
                "color": "ff0000",
                "description": "Review needed",
                "url": "https://api.github.com/repos/octo/repo/labels/needs%20triage",
            },
        ]
    )

    result = await gh_create_label(
        "octo",
        "repo",
        "needs triage",
        "ff0000",
        ctx=_context(client),
        description="Review needed",
    )

    assert result.name == "needs triage"
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    create_args, create_kwargs = client.calls[0]
    assert create_args[:3] == ("label", "create", "needs triage")
    assert "--json" not in create_args
    assert "--force" not in create_args
    assert create_kwargs == {"json_output": False}
    assert client.calls[1][0] == (
        "api",
        "repos/octo/repo/labels/needs%20triage",
    )


@pytest.mark.asyncio
async def test_edit_label_uses_supported_name_flag_then_reads_label() -> None:
    client = FakeGhClient(
        [
            {"stdout": ""},
            {
                "name": "renamed",
                "color": "00ff00",
                "description": None,
                "url": "https://api.github.com/repos/octo/repo/labels/renamed",
            },
        ]
    )

    result = await gh_edit_label(
        "octo",
        "repo",
        "old",
        ctx=_context(client),
        new_name="renamed",
    )

    assert result.name == "renamed"
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    edit_args = client.calls[0][0]
    assert "--name" in edit_args
    assert "--new-name" not in edit_args
    assert "--json" not in edit_args
    assert client.calls[1][0] == ("api", "repos/octo/repo/labels/renamed")


@pytest.mark.asyncio
async def test_create_milestone_uses_input_then_reads_by_number() -> None:
    client = FakeGhClient(
        [
            {"number": 7, "title": "v1", "state": "open", "url": "api-url"},
            {"number": 7, "title": "v1", "state": "open", "url": "api-url"},
        ]
    )

    result = await gh_create_milestone(
        "octo",
        "repo",
        "v1",
        ctx=_context(client),
    )

    assert result.number == 7
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    create_args, create_kwargs = client.calls[0]
    assert create_args[:4] == (
        "api",
        "repos/octo/repo/milestones",
        "-X",
        "POST",
    )
    assert "--input" in create_args
    assert "-i" not in create_args
    assert create_kwargs == {}
    assert client.calls[1][0] == ("api", "repos/octo/repo/milestones/7")


@pytest.mark.asyncio
async def test_list_milestones_forces_get_for_query_fields() -> None:
    client = FakeGhClient([[{"number": 7, "title": "v1", "state": "open", "url": "api-url"}]])

    result = await gh_list_milestones(
        "octo",
        "repo",
        ctx=_context(client),
        state="all",
        per_page=20,
    )

    assert result.total_count == 1
    assert client.calls[0][0] == (
        "api",
        "repos/octo/repo/milestones",
        "-X",
        "GET",
        "-f",
        "per_page=20",
        "-f",
        "state=all",
    )


@pytest.mark.asyncio
async def test_create_pr_executes_then_reads_url() -> None:
    url = "https://github.com/octo/repo/pull/9"
    client = FakeGhClient(
        [
            {"stdout": url},
            {"number": 9, "title": "Ship it", "url": url},
        ]
    )

    result = await gh_create_pr(
        "octo",
        "repo",
        "Ship it",
        "Body",
        "feature",
        "main",
        ctx=_context(client),
    )

    assert result.number == 9
    assert client.calls[0][1] == {"json_output": False, "stdin_text": "Body"}
    assert "--body-file" in client.calls[0][0]
    assert "--json" not in client.calls[0][0]
    assert client.calls[1][0][:3] == ("pr", "view", url)


@pytest.mark.asyncio
async def test_edit_issue_resolves_milestone_then_reads_issue() -> None:
    url = "https://github.com/octo/repo/issues/4"
    client = FakeGhClient(
        [
            {"title": "Version 1"},
            {"stdout": ""},
            {
                "number": 4,
                "title": "Updated",
                "state": "OPEN",
                "url": url,
                "milestone": {"number": 7},
            },
        ]
    )

    result = await gh_edit_issue(
        "octo",
        "repo",
        4,
        ctx=_context(client),
        title="Updated",
        milestone=7,
    )

    assert result.title == "Updated"
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert client.calls[0][0] == ("api", "repos/octo/repo/milestones/7")
    edit_args, edit_kwargs = client.calls[1]
    assert edit_args[-2:] == ("--milestone", "Version 1")
    assert "--json" not in edit_args
    assert edit_kwargs == {"json_output": False, "stdin_text": None}
    assert client.calls[2][0][:3] == ("issue", "view", "4")


@pytest.mark.asyncio
async def test_watch_run_polls_instead_of_starting_blocking_gh_watch() -> None:
    client = FakeGhClient(
        [
            {
                "status": "completed",
                "conclusion": "success",
                "url": "https://github.com/octo/repo/actions/runs/123",
            }
        ]
    )

    result = await gh_watch_run("octo", "repo", 123, ctx=_context(client))

    assert result.conclusion == "success"
    assert client.calls[0][0][:3] == ("run", "view", "123")
    assert "watch" not in client.calls[0][0]


@pytest.mark.asyncio
async def test_get_pr_checks_returns_failed_checks_without_treating_status_as_command_error() -> (
    None
):
    base_sha = "a" * 40
    head_sha = "b" * 40
    client = FakeGhClient(
        [
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
            [
                {
                    "name": "test",
                    "state": "FAILURE",
                    "bucket": "fail",
                    "workflow": "CI",
                    "description": "Tests failed",
                    "link": "https://github.com/octo/repo/actions/runs/123",
                }
            ],
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
        ]
    )

    result = await gh_get_pr_checks("octo", "repo", 224, ctx=_context(client))

    assert result.head_sha == head_sha
    assert result.checks[0].bucket == "fail"
    assert result.checks[0].name == "test"
    assert client.calls[1][1] == {"expected_returncode": {0, 1, 8}}
    assert "--watch" not in client.calls[1][0]


@pytest.mark.asyncio
async def test_list_run_jobs_returns_bounded_exact_attempt_page() -> None:
    head_sha = "c" * 40
    snapshot = {
        "attempt": 2,
        "headSha": head_sha,
        "status": "completed",
        "conclusion": "failure",
        "url": "https://github.com/octo/repo/actions/runs/123",
    }
    client = FakeGhClient(
        [
            snapshot,
            {
                "total_count": 1,
                "jobs": [
                    {
                        "id": 456,
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://github.com/octo/repo/actions/runs/123/job/456",
                        "runner_name": "runner-1",
                        "steps": [
                            {
                                "number": 1,
                                "name": "pytest",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    }
                ],
            },
            snapshot,
        ]
    )

    result = await gh_list_run_jobs(
        "octo", "repo", 123, ctx=_context(client), attempt=2, page=1, per_page=25
    )

    assert result.attempt == 2
    assert result.head_sha == head_sha
    assert result.jobs[0].id == 456
    assert result.jobs[0].steps[0].name == "pytest"
    assert client.calls[1][0] == (
        "api",
        "repos/octo/repo/actions/runs/123/attempts/2/jobs",
        "-X",
        "GET",
        "-f",
        "page=1",
        "-f",
        "per_page=25",
    )


@pytest.mark.asyncio
async def test_get_failed_run_logs_is_noninteractive_bounded_and_attempt_pinned() -> None:
    head_sha = "d" * 40
    snapshot = {
        "attempt": 1,
        "headSha": head_sha,
        "status": "completed",
        "conclusion": "failure",
        "url": "https://github.com/octo/repo/actions/runs/123",
    }
    logs = "job\tstep\tfirst failure\njob\tstep\tsecond failure"
    client = FakeGhClient([snapshot, {"stdout": logs}, snapshot])

    result = await gh_get_failed_run_logs(
        "octo", "repo", 123, ctx=_context(client), attempt=1, max_bytes=20
    )

    assert result.attempt == 1
    assert result.truncated is True
    assert result.bytes_returned <= 20
    assert result.total_bytes == len(logs.encode())
    log_args, log_kwargs = client.calls[1]
    assert "--log-failed" in log_args
    assert log_kwargs == {"json_output": False}


@pytest.mark.asyncio
async def test_issue_branch_uses_linked_graphql_and_pr_edit_uses_raw_write() -> None:
    sha = "a" * 40
    branch_client = FakeGhClient(
        [
            {"node_id": "R_repo", "default_branch": "main"},
            {"node_id": "I_issue"},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            {
                "data": {
                    "createLinkedBranch": {
                        "issue": {"id": "I_issue"},
                        "linkedBranch": {"id": "LB_feature"},
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "id": "R_repo",
                        "issue": {
                            "id": "I_issue",
                            "linkedBranches": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "LB_feature",
                                        "ref": {
                                            "name": "feature",
                                            "prefix": "refs/heads/",
                                            "repository": {"id": "R_repo"},
                                            "target": {"oid": sha},
                                        },
                                    }
                                ],
                            },
                        },
                    }
                }
            },
        ]
    )
    branch = await gh_create_branch(
        "octo",
        "repo",
        4,
        "feature",
        ctx=_context(branch_client),
    )
    assert branch.precondition_checked is True
    assert branch.readback_completed is True
    assert branch.state_matches_requested is True
    assert branch.linked_branch_id == "LB_feature"
    assert branch_client.calls[4][0][:4] == ("api", "graphql", "-X", "POST")
    assert branch_client.payloads[0]["variables"]["input"] == {
        "issueId": "I_issue",
        "repositoryId": "R_repo",
        "oid": sha,
        "name": "feature",
    }

    pr_client = FakeGhClient(
        [
            {"stdout": ""},
            {"title": "Updated PR", "url": "https://github.com/octo/repo/pull/9"},
        ]
    )
    result = await gh_edit_pr(
        "octo",
        "repo",
        9,
        ctx=_context(pr_client),
        title="Updated PR",
    )
    assert result.title == "Updated PR"
    assert pr_client.calls[0][1] == {"json_output": False, "stdin_text": None}
    assert pr_client.calls[1][0][:3] == ("pr", "view", "9")


@pytest.mark.asyncio
async def test_issue_branch_rejects_commit_sha_before_client_execution() -> None:
    client = FakeGhClient([])

    with pytest.raises(ValueError, match="gh_create_branch_from_sha"):
        await gh_create_branch(
            "octo",
            "repo",
            4,
            "feature",
            ctx=_context(client),
            base="a" * 40,
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_create_branch_from_sha_verifies_commit_and_creates_exact_ref() -> None:
    base_sha = "A" * 40
    client = FakeGhClient(
        [
            {"sha": "a" * 40},
            {"ref": "refs/heads/feature/exact", "object": {"sha": "a" * 40}},
            {"ref": "refs/heads/feature/exact", "object": {"sha": "a" * 40}},
        ]
    )

    result = await gh_create_branch_from_sha(
        "octo",
        "repo",
        "feature/exact",
        base_sha,
        ctx=_context(client),
    )

    assert result.created is True
    assert result.base_sha == "a" * 40
    assert result.ref == "refs/heads/feature/exact"
    assert result.write_completed is True
    assert result.readback_completed is True
    assert client.calls[0] == (
        ("api", f"repos/octo/repo/git/commits/{'a' * 40}", "-X", "GET"),
        {},
    )
    assert client.calls[1][0][:3] == ("api", "repos/octo/repo/git/refs", "-X")
    assert client.calls[2][0] == (
        "api",
        "repos/octo/repo/git/ref/heads/feature/exact",
        "-X",
        "GET",
    )
    assert client.payloads == [{"ref": "refs/heads/feature/exact", "sha": "a" * 40}]


@pytest.mark.asyncio
async def test_create_branch_from_sha_recovers_same_ref_without_duplicate_write() -> None:
    base_sha = "a" * 40
    client = FakeGhClient(
        [
            {"sha": base_sha},
            RuntimeError("create response was interrupted"),
            {"ref": "refs/heads/feature", "object": {"sha": base_sha}},
        ]
    )

    result = await gh_create_branch_from_sha(
        "octo", "repo", "feature", base_sha, ctx=_context(client)
    )

    assert result.created is False
    assert result.write_completed is False
    assert result.readback_completed is True
    assert "no write was performed" in result.message
    assert client.calls[2] == (
        ("api", "repos/octo/repo/git/ref/heads/feature", "-X", "GET"),
        {},
    )


@pytest.mark.asyncio
async def test_create_branch_from_sha_never_overwrites_conflicting_ref() -> None:
    base_sha = "a" * 40
    other_sha = "b" * 40
    client = FakeGhClient(
        [
            {"sha": base_sha},
            RuntimeError("Reference already exists"),
            {"ref": "refs/heads/feature", "object": {"sha": other_sha}},
        ]
    )

    with pytest.raises(RuntimeError, match=f"already exists at {other_sha}"):
        await gh_create_branch_from_sha("octo", "repo", "feature", base_sha, ctx=_context(client))

    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_write_disabled_stops_before_client_execution() -> None:
    client = FakeGhClient([])
    context = _context(client)
    context.request_context.lifespan_context.settings.allow_write_commands = False

    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_create_issue("octo", "repo", "No write", ctx=context)

    assert client.calls == []


@pytest.mark.asyncio
async def test_repository_allowlist_stops_before_client_execution() -> None:
    client = FakeGhClient([])
    context = _context(client)
    context.request_context.lifespan_context.settings.allowed_repositories = "octo/allowed"

    with pytest.raises(RuntimeError, match="not allowed"):
        await gh_create_issue("octo", "other", "No write", ctx=context)

    assert client.calls == []


@pytest.mark.asyncio
async def test_high_risk_action_requires_separate_opt_in() -> None:
    client = FakeGhClient([])
    context = _context(client)
    context.request_context.lifespan_context.settings.allow_workflow_dispatch = False

    with pytest.raises(RuntimeError, match="ALLOW_WORKFLOW_DISPATCH"):
        await gh_run_workflow_exact(
            "octo",
            "repo",
            99,
            ".github/workflows/release.yml",
            "heads/main",
            "a" * 40,
            ctx=context,
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_noop_edits_are_rejected() -> None:
    client = FakeGhClient([])
    context = _context(client)

    with pytest.raises(ValueError, match="at least one issue edit"):
        await gh_edit_issue("octo", "repo", 1, ctx=context)
    with pytest.raises(ValueError, match="at least one pull request edit"):
        await gh_edit_pr("octo", "repo", 1, ctx=context)
    with pytest.raises(ValueError, match="at least one label edit"):
        await gh_edit_label("octo", "repo", "bug", ctx=context)

    assert client.calls == []


@pytest.mark.asyncio
async def test_successful_write_with_failed_readback_returns_partial_success() -> None:
    url = "https://github.com/octo/repo/issues/42"
    client = FakeGhClient([{"stdout": url}, RuntimeError("readback unavailable")])

    result = await gh_create_issue("octo", "repo", "Created", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.url == url
    assert result.number == 42
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


@pytest.mark.asyncio
async def test_empty_edit_body_is_sent_explicitly() -> None:
    client = FakeGhClient(
        [
            {"stdout": ""},
            {
                "number": 4,
                "title": "Issue",
                "state": "OPEN",
                "url": "https://github.com/octo/repo/issues/4",
            },
        ]
    )

    result = await gh_edit_issue("octo", "repo", 4, ctx=_context(client), body="")

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert "--body-file" in client.calls[0][0]
    assert client.calls[0][1]["stdin_text"] == ""


@pytest.mark.asyncio
async def test_get_file_contents_reads_complete_blob_at_exact_ref() -> None:
    sha = "a" * 40
    client = FakeGhClient(
        [
            {"type": "file", "sha": sha},
            {"sha": sha, "size": 6, "encoding": "base64", "content": "aGVsbG8K"},
        ]
    )

    result = await gh_get_file_contents(
        "octo", "repo", ".github/workflows/apply.yml", sha, ctx=_context(client)
    )

    assert result.content == "hello\n"
    assert result.encoding == "utf-8"
    assert result.ref == sha
    assert client.calls[0][0][-4:] == ("-X", "GET", "-f", f"ref={sha}")
    assert client.calls[1][0] == ("api", f"repos/octo/repo/git/blobs/{sha}")


@pytest.mark.asyncio
async def test_get_file_contents_returns_binary_as_base64() -> None:
    sha = "a" * 40
    client = FakeGhClient(
        [
            {"type": "file", "sha": sha},
            {"sha": sha, "encoding": "base64", "content": "/wA="},
        ]
    )

    result = await gh_get_file_contents("octo", "repo", "asset.bin", sha, ctx=_context(client))

    assert result.content == "/wA="
    assert result.encoding == "base64"
    assert result.size == 2


@pytest.mark.asyncio
async def test_get_pr_diff_uses_exact_shas_and_returns_bounded_fingerprint() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    content = "diff --git a/a.txt b/a.txt\n+replacement\n"
    client = FakeGhClient(
        [
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
            {"stdout": content},
        ]
    )

    result = await gh_get_pr_diff("octo", "repo", 224, ctx=_context(client), max_bytes=20)

    assert result.base_sha == base_sha
    assert result.head_sha == head_sha
    assert result.truncated is True
    assert result.bytes_returned <= 20
    assert result.total_bytes == len(content.encode())
    assert result.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert client.calls[0][0] == (
        "api",
        "repos/octo/repo/pulls/224",
        "-X",
        "GET",
    )
    assert client.calls[1] == (
        (
            "api",
            f"repos/octo/repo/compare/{base_sha}...{head_sha}",
            "-X",
            "GET",
            "-H",
            "Accept: application/vnd.github.v3.diff",
        ),
        {"json_output": False},
    )


@pytest.mark.asyncio
async def test_list_pr_files_is_explicit_get_and_bounded_page() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    client = FakeGhClient(
        [
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
            [
                {
                    "filename": "src/app.py",
                    "status": "modified",
                    "additions": 2,
                    "deletions": 1,
                    "changes": 3,
                    "sha": "c" * 40,
                    "patch": "@@ -1 +1 @@",
                }
            ],
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
        ]
    )

    result = await gh_list_pr_files("octo", "repo", 224, ctx=_context(client), page=2, per_page=10)

    assert result.base_sha == base_sha
    assert result.files[0].filename == "src/app.py"
    assert result.files[0].patch_bytes_returned == len(b"@@ -1 +1 @@")
    assert result.has_more is False
    assert client.calls[1][0][-6:] == ("-X", "GET", "-f", "page=2", "-f", "per_page=10")


@pytest.mark.asyncio
async def test_list_pr_commits_normalizes_nested_api_result() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    commit_sha = "c" * 40
    client = FakeGhClient(
        [
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
            [
                {
                    "sha": commit_sha,
                    "html_url": f"https://github.com/octo/repo/commit/{commit_sha}",
                    "author": {"login": "octocat"},
                    "committer": {"login": "hubot"},
                    "commit": {
                        "message": "Reviewable change",
                        "author": {"name": "Octo Cat", "date": "2026-08-04T01:00:00Z"},
                        "committer": {"date": "2026-08-04T01:01:00Z"},
                    },
                }
            ],
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
        ]
    )

    result = await gh_list_pr_commits("octo", "repo", 224, ctx=_context(client), per_page=5)

    assert result.commits[0].sha == commit_sha
    assert result.commits[0].author_login == "octocat"
    assert result.commits[0].message == "Reviewable change"
    assert result.has_more is False


@pytest.mark.asyncio
async def test_list_pr_files_rejects_snapshot_drift() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    client = FakeGhClient(
        [
            {"base": {"sha": base_sha}, "head": {"sha": head_sha}},
            [],
            {"base": {"sha": base_sha}, "head": {"sha": "c" * 40}},
        ]
    )

    with pytest.raises(RuntimeError, match="changed during the read"):
        await gh_list_pr_files("octo", "repo", 224, ctx=_context(client))


@pytest.mark.asyncio
async def test_submit_pr_review_is_exact_head_formal_review_with_readback() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    review_url = "https://github.com/octo/repo/pull/224#pullrequestreview-91"
    client = FakeGhClient(
        [
            {"login": "reviewer"},
            {
                "base": {"sha": base_sha},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
            {"id": 91, "state": "APPROVED", "html_url": review_url},
            {
                "id": 91,
                "state": "APPROVED",
                "body": "Reviewed exact revision.",
                "html_url": review_url,
                "submitted_at": "2026-08-04T18:00:00Z",
                "commit_id": head_sha,
                "user": {"login": "reviewer"},
            },
        ]
    )

    result = await gh_submit_pr_review(
        "octo",
        "repo",
        224,
        head_sha,
        "approve",
        ctx=_context(client),
        body="Reviewed exact revision.",
    )

    assert result.review_id == 91
    assert result.state == "APPROVED"
    assert result.author == "reviewer"
    assert result.commit_sha == head_sha
    assert client.payloads == [
        {
            "body": "Reviewed exact revision.",
            "event": "APPROVE",
            "commit_id": head_sha,
        }
    ]
    assert client.calls[0][0] == ("api", "user", "-X", "GET")
    assert client.calls[1][0] == ("api", "repos/octo/repo/pulls/224", "-X", "GET")
    assert client.calls[2][0][:4] == (
        "api",
        "repos/octo/repo/pulls/224/reviews",
        "-X",
        "POST",
    )
    assert client.calls[3][0] == (
        "api",
        "repos/octo/repo/pulls/224/reviews/91",
        "-X",
        "GET",
    )


@pytest.mark.asyncio
async def test_submit_pr_review_rejects_stale_head_before_write() -> None:
    client = FakeGhClient(
        [
            {"login": "reviewer"},
            {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}},
        ]
    )

    with pytest.raises(RuntimeError, match="precondition mismatch"):
        await gh_submit_pr_review("octo", "repo", 224, "c" * 40, "approve", ctx=_context(client))

    assert len(client.calls) == 2
    assert client.payloads == []


@pytest.mark.asyncio
async def test_submit_pr_review_rejects_verified_self_approval_before_write() -> None:
    head_sha = "b" * 40
    client = FakeGhClient(
        [
            {"login": "AUTHOR"},
            {
                "base": {"sha": "a" * 40},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
        ]
    )

    with pytest.raises(ValueError, match="cannot approve its own pull request"):
        await gh_submit_pr_review("octo", "repo", 224, head_sha, "approve", ctx=_context(client))

    assert len(client.calls) == 2
    assert client.payloads == []


@pytest.mark.asyncio
async def test_submit_pr_review_requires_body_for_non_approval() -> None:
    client = FakeGhClient([])

    with pytest.raises(ValueError, match="non-empty review body"):
        await gh_submit_pr_review(
            "octo", "repo", 224, "b" * 40, "request_changes", ctx=_context(client)
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_submit_pr_review_known_failure_returns_structured_outcome_without_retry() -> None:
    head_sha = "b" * 40
    client = FakeGhClient(
        [
            {
                "base": {"sha": "a" * 40},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
            GitHubRequestError(
                "GitHub rejected the review",
                status_code=422,
                ambiguous=False,
                metadata=GitHubRequestMetadata(request_id="req-review-failed"),
            ),
        ]
    )

    result = await gh_submit_pr_review(
        "octo",
        "repo",
        224,
        head_sha,
        "request_changes",
        ctx=_context(client),
        body="Please revise.",
    )

    assert result.write_completed is False
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-review-failed"
    assert result.warning is not None
    assert "write failed before confirmed completion" in result.warning
    assert len(client.calls) == 2
    assert len(client.payloads) == 1


@pytest.mark.asyncio
async def test_submit_pr_review_returns_partial_success_when_readback_fails() -> None:
    head_sha = "b" * 40
    review_url = "https://github.com/octo/repo/pull/224#pullrequestreview-91"
    client = FakeGhClient(
        [
            {"login": "reviewer"},
            {
                "base": {"sha": "a" * 40},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
            {"id": 91, "state": "APPROVED", "html_url": review_url},
            RuntimeError("readback unavailable"),
        ]
    )

    result = await gh_submit_pr_review(
        "octo", "repo", 224, head_sha, "approve", ctx=_context(client)
    )

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.review_id == 91
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


@pytest.mark.asyncio
async def test_merge_pr_uses_atomic_head_guard_and_noninteractive_body() -> None:
    head_sha = "b" * 40
    merge_sha = "c" * 40
    client = FakeGhClient(
        [
            {"base": {"sha": "a" * 40}, "head": {"sha": head_sha}},
            {"stdout": ""},
            {
                "number": 224,
                "url": "https://github.com/octo/repo/pull/224",
                "state": "MERGED",
                "mergedAt": "2026-08-04T18:01:00Z",
                "mergeCommit": {"oid": merge_sha},
                "headRefOid": head_sha,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
            },
        ]
    )

    result = await gh_merge_pr(
        "octo",
        "repo",
        224,
        head_sha,
        "squash",
        ctx=_context(client),
        subject="Reviewed change",
        body="Merge exact reviewed revision.",
    )

    assert result.merged is True
    assert result.merge_commit_sha == merge_sha
    merge_args, merge_kwargs = client.calls[1]
    assert merge_args == (
        "pr",
        "merge",
        "224",
        "--repo",
        "octo/repo",
        "--squash",
        "--match-head-commit",
        head_sha,
        "--body-file",
        "-",
        "--subject",
        "Reviewed change",
    )
    assert merge_kwargs == {
        "json_output": False,
        "stdin_text": "Merge exact reviewed revision.",
    }
    assert "--admin" not in merge_args
    assert "--delete-branch" not in merge_args
    assert "--auto" not in merge_args


@pytest.mark.asyncio
async def test_merge_pr_requires_separate_opt_in() -> None:
    client = FakeGhClient([])
    context = _context(client)
    context.request_context.lifespan_context.settings.allow_pr_merge = False

    with pytest.raises(RuntimeError, match="MCP_GH_ALLOW_PR_MERGE"):
        await gh_merge_pr("octo", "repo", 224, "b" * 40, "merge", ctx=context)

    assert client.calls == []


@pytest.mark.asyncio
async def test_merge_pr_returns_partial_success_when_readback_fails() -> None:
    head_sha = "b" * 40
    client = FakeGhClient(
        [
            {"base": {"sha": "a" * 40}, "head": {"sha": head_sha}},
            {"stdout": ""},
            RuntimeError("readback unavailable"),
        ]
    )

    result = await gh_merge_pr("octo", "repo", 224, head_sha, "rebase", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.merged is False
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


@pytest.mark.asyncio
async def test_commit_files_requires_separate_content_commit_opt_in() -> None:
    client = FakeGhClient([])
    context = _context(client)
    context.request_context.lifespan_context.settings.allow_content_commits = False

    with pytest.raises(RuntimeError, match="MCP_GH_ALLOW_CONTENT_COMMITS"):
        await gh_commit_files(
            "octo",
            "repo",
            "feature",
            "a" * 40,
            [PublicCommitFile(path="file.txt", content="content")],
            "Commit",
            ctx=context,
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_commit_files_creates_one_tree_commit_and_cas_ref_update() -> None:
    head = "a" * 40
    base_tree = "b" * 40
    blob_one = "c" * 40
    blob_two = "d" * 40
    tree = "e" * 40
    commit = "f" * 40
    client = FakeGhClient(
        [
            {"object": {"sha": head}},
            {"node_id": "R_repo"},
            {"tree": {"sha": base_tree}},
            {"sha": blob_one},
            {"sha": blob_two},
            {"sha": tree},
            {"sha": commit, "html_url": f"https://github.com/octo/repo/commit/{commit}"},
            {"data": {"updateRefs": {"clientMutationId": None}}},
            {"object": {"sha": commit}},
        ]
    )
    files = [
        PublicCommitFile(path="docs/one.md", content="one\n"),
        PublicCommitFile(path="scripts/run.sh", content="#!/bin/sh\n", mode="100755"),
    ]

    result = await gh_commit_files(
        "octo",
        "repo",
        "rc/audit-01-regression-baseline",
        head,
        files,
        "Add issue 207 files",
        ctx=_context(client),
    )

    assert result.ref_updated is True
    assert result.commit_sha == commit
    assert result.files_committed == 2
    assert client.payloads[0] == {"content": "one\n", "encoding": "utf-8"}
    assert client.payloads[2] == {
        "base_tree": base_tree,
        "tree": [
            {"path": "docs/one.md", "mode": "100644", "type": "blob", "sha": blob_one},
            {
                "path": "scripts/run.sh",
                "mode": "100755",
                "type": "blob",
                "sha": blob_two,
            },
        ],
    }
    assert client.payloads[3] == {
        "message": "Add issue 207 files",
        "tree": tree,
        "parents": [head],
    }
    assert client.payloads[4] == {
        "query": (
            "mutation($input: UpdateRefsInput!) { updateRefs(input: $input) { clientMutationId } }"
        ),
        "variables": {
            "input": {
                "repositoryId": "R_repo",
                "refUpdates": [
                    {
                        "name": "refs/heads/rc/audit-01-regression-baseline",
                        "beforeOid": head,
                        "afterOid": commit,
                        "force": False,
                    }
                ],
            }
        },
    }


@pytest.mark.asyncio
async def test_commit_files_head_mismatch_creates_no_git_objects() -> None:
    head = "a" * 40
    client = FakeGhClient([{"object": {"sha": "b" * 40}}])

    with pytest.raises(RuntimeError, match="head mismatch"):
        await gh_commit_files(
            "octo",
            "repo",
            "feature",
            head,
            [PublicCommitFile(path="file.txt", content="content")],
            "Commit",
            ctx=_context(client),
        )

    assert len(client.calls) == 1
    assert client.payloads == []


@pytest.mark.asyncio
async def test_commit_files_ref_race_returns_branch_unchanged_result() -> None:
    head = "a" * 40
    base_tree = "b" * 40
    blob = "c" * 40
    tree = "d" * 40
    commit = "e" * 40
    client = FakeGhClient(
        [
            {"object": {"sha": head}},
            {"node_id": "R_repo"},
            {"tree": {"sha": base_tree}},
            {"sha": blob},
            {"sha": tree},
            {"sha": commit, "html_url": "commit-url"},
            RuntimeError("non-fast-forward"),
            {"object": {"sha": head}},
        ]
    )

    result = await gh_commit_files(
        "octo",
        "repo",
        "feature",
        head,
        [PublicCommitFile(path="file.txt", content="content")],
        "Commit",
        ctx=_context(client),
    )

    assert result.write_completed is False
    assert result.ref_updated is False
    assert result.files_committed == 0
    assert result.commit_sha == commit
    assert result.warning is not None
    assert "authoritative exact-ref readback" in result.warning
    assert "head is unchanged at" in result.warning
    assert head in result.warning
    assert "Do not retry automatically" in result.warning


@pytest.mark.asyncio
async def test_commit_files_unknown_ref_outcome_requires_read_before_retry() -> None:
    head = "a" * 40
    commit = "e" * 40
    client = FakeGhClient(
        [
            {"object": {"sha": head}},
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            {"sha": "c" * 40},
            {"sha": "d" * 40},
            {"sha": commit},
            GitHubRequestError(
                "timeout",
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-content-ambiguous"),
            ),
            RuntimeError("readback timeout"),
        ]
    )

    result = await gh_commit_files(
        "octo",
        "repo",
        "feature",
        head,
        [PublicCommitFile(path="file.txt", content="content")],
        "Commit",
        ctx=_context(client),
    )

    assert result.ref_updated is None
    assert result.write_completed is None
    assert result.readback_completed is False
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "Do not retry automatically" in result.warning
