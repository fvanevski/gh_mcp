"""Regression coverage for the complete issue #9 write-surface migration."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    gh_submit_pr_review,
    gh_upsert_label,
)
from mcp_gh_server.settings import Settings


@dataclass
class MetadataAwareClient:
    """Minimal protocol fake that distinguishes governed writes from reads."""

    read_results: list[Any] = field(default_factory=list)
    write_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        result = self.write_results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(client: MetadataAwareClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            allow_write_commands=True,
            allow_repo_creation=True,
            allow_release_creation=True,
            allow_workflow_dispatch=True,
            allow_content_commits=True,
            allow_pr_merge=True,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def test_all_public_writes_are_bound_to_issue9_compatibility_modules() -> None:
    expected = {
        gh_create_issue: "mcp_gh_server.write_tool_schema",
        gh_edit_issue: "mcp_gh_server.write_tool_schema",
        gh_set_issue_state: "mcp_gh_server.write_tool_schema",
        gh_create_label: "mcp_gh_server.write_tool_schema",
        gh_upsert_label: "mcp_gh_server.write_tool_schema",
        gh_edit_label: "mcp_gh_server.write_tool_schema",
        gh_create_milestone: "mcp_gh_server.write_tool_schema",
        gh_create_comment: "mcp_gh_server.write_tool_schema",
        gh_create_pr: "mcp_gh_server.write_tool_schema",
        gh_edit_pr: "mcp_gh_server.write_tool_schema",
        gh_set_pr_draft_state: "mcp_gh_server.write_tool_schema",
        gh_submit_pr_review: "mcp_gh_server.write_tool_schema",
        gh_merge_pr: "mcp_gh_server.write_tool_schema",
        gh_create_repo: "mcp_gh_server.write_tool_schema",
        gh_commit_files: "mcp_gh_server.write_tool_schema",
        gh_create_release_exact: "mcp_gh_server.write_tool_schema",
        gh_run_workflow_exact: "mcp_gh_server.write_tool_schema",
        gh_create_branch: "mcp_gh_server.write_tool_schema",
        gh_create_branch_from_sha: "mcp_gh_server.write_tool_schema",
    }

    assert len(expected) == 19
    for function, module in expected.items():
        assert function.__module__ == module


@pytest.mark.asyncio
async def test_legacy_create_issue_preserves_successful_governor_metadata() -> None:
    url = "https://github.com/octo/repo/issues/42"
    client = MetadataAwareClient(
        read_results=[{"number": 42, "title": "Contract", "url": url}],
        write_results=[
            GitHubRequestResult(
                value={"stdout": url},
                metadata=GitHubRequestMetadata(
                    request_id="req-create-42",
                    warning="Governor supplied a transport warning.",
                ),
            )
        ],
    )

    result = await gh_create_issue("octo", "repo", "Contract", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.warning is not None
    assert "Governor supplied a transport warning." in result.warning
    assert "req-create-42" in result.warning
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_merge_success_preserves_request_identity_and_warning() -> None:
    head = "b" * 40
    client = MetadataAwareClient(
        read_results=[
            {"head": {"sha": head}},
            {
                "number": 9,
                "url": "https://github.com/octo/repo/pull/9",
                "state": "MERGED",
                "mergedAt": "2026-08-11T04:00:00Z",
                "mergeCommit": {"oid": "c" * 40},
                "headRefOid": head,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
            },
        ],
        write_results=[
            GitHubRequestResult(
                value={"stdout": ""},
                metadata=GitHubRequestMetadata(
                    request_id="req-merge-9",
                    warning="Governor metadata retained.",
                ),
            )
        ],
    )

    result = await gh_merge_pr(
        "octo",
        "repo",
        9,
        head,
        "squash",
        ctx=_context(client),
    )

    assert result.merged is True
    assert result.readback_completed is True
    assert result.warning is not None
    assert "Governor metadata retained." in result.warning
    assert "req-merge-9" in result.warning


@pytest.mark.asyncio
async def test_merge_ambiguous_failure_rejects_wrong_auto_merge_method() -> None:
    head = "b" * 40
    client = MetadataAwareClient(
        read_results=[
            {"head": {"sha": head}},
            {
                "number": 9,
                "url": "https://github.com/octo/repo/pull/9",
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefOid": head,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": {"mergeMethod": "MERGE"},
            },
        ],
        write_results=[
            GitHubRequestError(
                "transport reset",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-ambiguous-method"),
            )
        ],
    )

    result = await gh_merge_pr(
        "octo",
        "repo",
        9,
        head,
        "squash",
        ctx=_context(client),
    )

    assert result.write_completed is False
    assert result.readback_completed is False
    assert result.auto_merge_enabled is True
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "does not match the requested state" in result.warning
    assert "req-ambiguous-method" in result.warning
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_legacy_writes_without_structured_readback_do_not_claim_verification() -> None:
    comment_url = "https://github.com/octo/repo/issues/4#issuecomment-5"
    comment_client = MetadataAwareClient(
        write_results=[GitHubRequestResult(value={"stdout": comment_url})]
    )

    comment = await gh_create_comment(
        "octo",
        "repo",
        4,
        "Hello",
        ctx=_context(comment_client),
    )

    assert comment.write_completed is True
    assert comment.readback_completed is False
    assert comment.warning is not None
    assert "Do not retry automatically" in comment.warning

    branch_client = MetadataAwareClient(write_results=[GitHubRequestResult(value={"stdout": ""})])
    branch = await gh_create_branch(
        "octo",
        "repo",
        4,
        "feature",
        ctx=_context(branch_client),
    )

    assert branch.write_completed is True
    assert branch.readback_completed is False
    assert branch.warning is not None
