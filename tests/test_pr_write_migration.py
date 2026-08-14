"""Focused regressions for canonical pull-request write migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mcp_gh_server.write_tool_schema as write_tool_schema
from mcp_gh_server.pr_write_models import (
    PullRequestCreate,
    PullRequestEdit,
    PullRequestMerge,
    PullRequestReviewSubmission,
)
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import (
    AppContext,
    gh_create_pr,
    gh_edit_pr,
    gh_merge_pr,
    gh_submit_pr_review,
    mcp,
)
from mcp_gh_server.settings import Settings


@dataclass
class FakeCanonicalClient:
    """Separate governed mutations from authoritative readback results."""

    write_results: list[Any] = field(default_factory=list)
    read_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        if "--input" in args:
            payload_path = Path(args[args.index("--input") + 1])
            import json

            self.payloads.append(json.loads(payload_path.read_text()))
        result = self.write_results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(client: FakeCanonicalClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=True, allow_pr_merge=True),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _write_calls(client: FakeCanonicalClient) -> list[tuple[str, tuple[str, ...], dict[str, Any]]]:
    return [call for call in client.calls if call[0] == "write"]


async def test_create_pr_success_is_verified_with_direct_outcome_metadata() -> None:
    url = "https://github.com/octo/repo/pull/9"
    client = FakeCanonicalClient(
        write_results=[
            GitHubRequestResult(
                value={"stdout": url},
                metadata=GitHubRequestMetadata(request_id="req-create-pr"),
            )
        ],
        read_results=[
            {
                "number": 9,
                "title": "Requested",
                "url": url,
                "body": "Body",
                "headRefName": "feature",
                "baseRefName": "main",
                "isDraft": False,
                "labels": [],
                "assignees": [],
                "reviewRequests": [],
            }
        ],
    )

    result = await gh_create_pr(
        "octo", "repo", "Requested", "Body", "feature", "main", ctx=_context(client)
    )

    assert isinstance(result, PullRequestCreate)
    assert result.precondition_checked is False
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-create-pr"
    assert len(_write_calls(client)) == 1


async def test_edit_pr_known_failure_is_not_retried() -> None:
    client = FakeCanonicalClient(
        write_results=[
            GitHubRequestError(
                "GitHub rejected the mutation",
                status_code=422,
                ambiguous=False,
                metadata=GitHubRequestMetadata(request_id="req-edit-failed"),
            )
        ],
        read_results=[{"title": "Old", "url": "https://github.com/octo/repo/pull/9"}],
    )

    result = await gh_edit_pr("octo", "repo", 9, ctx=_context(client), title="Requested")

    assert isinstance(result, PullRequestEdit)
    assert result.write_completed is False
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.request_id == "req-edit-failed"
    assert result.warning is not None
    assert "write failed" in result.warning
    assert len(_write_calls(client)) == 1


async def test_create_pr_semantic_readback_mismatch_is_explicit() -> None:
    url = "https://github.com/octo/repo/pull/9"
    client = FakeCanonicalClient(
        write_results=[{"stdout": url}],
        read_results=[
            {
                "number": 9,
                "title": "Different",
                "url": url,
                "body": "Body",
                "headRefName": "feature",
                "baseRefName": "main",
                "isDraft": False,
                "labels": [],
                "assignees": [],
                "reviewRequests": [],
            }
        ],
    )

    result = await gh_create_pr(
        "octo", "repo", "Requested", "Body", "feature", "main", ctx=_context(client)
    )

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.warning is not None
    assert "does not match" in result.warning
    assert len(_write_calls(client)) == 1


async def test_review_readback_failure_is_partial_success_without_replay() -> None:
    head = "b" * 40
    client = FakeCanonicalClient(
        write_results=[{"id": 91, "state": "COMMENTED", "html_url": "review-url"}],
        read_results=[
            {"head": {"sha": head}, "user": {"login": "author"}},
            RuntimeError("readback unavailable"),
        ],
    )

    result = await gh_submit_pr_review(
        "octo",
        "repo",
        9,
        head,
        "comment",
        ctx=_context(client),
        body="Reviewed.",
    )

    assert isinstance(result, PullRequestReviewSubmission)
    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning
    assert len(_write_calls(client)) == 1


async def test_merge_ambiguous_transport_uses_readback_without_replay() -> None:
    head = "b" * 40
    merge_sha = "c" * 40
    client = FakeCanonicalClient(
        write_results=[
            GitHubRequestError(
                "transport reset after send",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-merge-ambiguous"),
            )
        ],
        read_results=[
            {"head": {"sha": head}},
            {
                "number": 9,
                "url": "https://github.com/octo/repo/pull/9",
                "state": "MERGED",
                "mergedAt": "2026-08-14T18:00:00Z",
                "mergeCommit": {"oid": merge_sha},
                "headRefOid": head,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
            },
        ],
    )

    result = await gh_merge_pr("octo", "repo", 9, head, "squash", ctx=_context(client))

    assert isinstance(result, PullRequestMerge)
    assert result.precondition_checked is True
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-merge-ambiguous"
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "Do not retry" in result.warning
    assert len(_write_calls(client)) == 1


async def test_review_stale_head_fails_before_mutation() -> None:
    client = FakeCanonicalClient(
        read_results=[{"head": {"sha": "b" * 40}, "user": {"login": "author"}}]
    )

    try:
        await gh_submit_pr_review(
            "octo",
            "repo",
            9,
            "c" * 40,
            "comment",
            ctx=_context(client),
            body="Reviewed.",
        )
    except RuntimeError as exc:
        assert "precondition mismatch" in str(exc)
    else:
        raise AssertionError("stale review head must fail before mutation")
    assert _write_calls(client) == []


async def test_merge_stale_head_fails_before_mutation() -> None:
    client = FakeCanonicalClient(read_results=[{"head": {"sha": "b" * 40}}])

    try:
        await gh_merge_pr("octo", "repo", 9, "c" * 40, "merge", ctx=_context(client))
    except RuntimeError as exc:
        assert "precondition mismatch" in str(exc)
    else:
        raise AssertionError("stale merge head must fail before mutation")
    assert _write_calls(client) == []


async def test_review_rejects_self_approval_before_mutation() -> None:
    head = "b" * 40
    client = FakeCanonicalClient(
        read_results=[
            {"login": "AUTHOR"},
            {"head": {"sha": head}, "user": {"login": "author"}},
        ]
    )

    try:
        await gh_submit_pr_review("octo", "repo", 9, head, "approve", ctx=_context(client))
    except ValueError as exc:
        assert "cannot approve its own pull request" in str(exc)
    else:
        raise AssertionError("self approval must fail before mutation")
    assert _write_calls(client) == []


async def test_merge_preserves_exact_head_guard_and_no_bypass_flags() -> None:
    head = "b" * 40
    client = FakeCanonicalClient(
        write_results=[{"stdout": ""}],
        read_results=[
            {"head": {"sha": head}},
            {
                "number": 9,
                "url": "https://github.com/octo/repo/pull/9",
                "state": "MERGED",
                "mergedAt": "2026-08-14T18:00:00Z",
                "mergeCommit": {"oid": "c" * 40},
                "headRefOid": head,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
            },
        ],
    )

    await gh_merge_pr("octo", "repo", 9, head, "squash", ctx=_context(client))

    _, args, _ = _write_calls(client)[0]
    assert "--match-head-commit" in args
    assert args[args.index("--match-head-commit") + 1] == head
    assert "--admin" not in args
    assert "--delete-branch" not in args
    assert "--auto" not in args


async def test_migrated_public_output_schemas_expose_shared_outcome_metadata() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    outcome_fields = {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
    }
    for name in ("gh_create_pr", "gh_edit_pr", "gh_submit_pr_review", "gh_merge_pr"):
        assert outcome_fields <= set(tools[name].output_schema["properties"])


def test_public_facade_delegates_migrated_pr_writes_to_canonical_module() -> None:
    for implementation in (
        write_tool_schema._gh_create_pr,
        write_tool_schema._gh_edit_pr,
        write_tool_schema._gh_submit_pr_review,
        write_tool_schema._gh_merge_pr,
    ):
        assert implementation.__module__ == "mcp_gh_server.tools.pr_writes"
