"""Regression tests for exact-state write/readback contracts."""

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
from mcp_gh_server.server import AppContext, gh_merge_pr, gh_submit_pr_review
from mcp_gh_server.settings import Settings
from mcp_gh_server.write_contracts import (
    ExactWriteResult,
    WritePreconditionMismatch,
    execute_write_readback,
    legacy_write_status,
    require_write_precondition,
)


def test_exact_write_result_exposes_required_07_contract_fields() -> None:
    schema = ExactWriteResult.model_json_schema()

    assert set(schema["properties"]) == {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
    }
    assert set(schema["required"]) == {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
    }


@pytest.mark.asyncio
async def test_precondition_mismatch_stops_before_mutation() -> None:
    writes = 0
    readbacks = 0

    async def precondition() -> Any:
        async def current() -> str:
            return "new-head"

        return await require_write_precondition(
            current,
            "reviewed-head",
            label="pull request head",
        )

    async def write() -> GitHubRequestResult[dict[str, bool]]:
        nonlocal writes
        writes += 1
        return GitHubRequestResult(value={"ok": True})

    async def readback() -> str:
        nonlocal readbacks
        readbacks += 1
        return "merged"

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await execute_write_readback(
            resource="Pull request merge",
            precondition=precondition,
            write=write,
            readback=readback,
            state_matches_requested=lambda state: state == "merged",
        )

    assert writes == 0
    assert readbacks == 0


@pytest.mark.asyncio
async def test_successful_mutation_requires_semantic_readback_match() -> None:
    calls: list[str] = []

    async def precondition() -> Any:
        async def current() -> str:
            calls.append("precondition")
            return "expected"

        return await require_write_precondition(current, "expected", label="state")

    async def write() -> GitHubRequestResult[dict[str, bool]]:
        calls.append("write")
        return GitHubRequestResult(
            value={"accepted": True},
            metadata=GitHubRequestMetadata(request_id="req-success"),
        )

    async def readback() -> str:
        calls.append("readback")
        return "requested"

    execution = await execute_write_readback(
        resource="State transition",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=lambda state: state == "requested",
    )

    assert calls == ["precondition", "write", "readback"]
    assert execution.outcome.precondition_checked is True
    assert execution.outcome.write_completed is True
    assert execution.outcome.readback_completed is True
    assert execution.outcome.state_matches_requested is True
    assert execution.outcome.request_id == "req-success"
    assert execution.outcome.warning is None


@pytest.mark.asyncio
async def test_successful_write_with_mismatched_readback_is_not_verified_success() -> None:
    async def write() -> GitHubRequestResult[None]:
        return GitHubRequestResult(value=None)

    async def readback() -> str:
        return "old-state"

    execution = await execute_write_readback(
        resource="Issue transition",
        write=write,
        readback=readback,
        state_matches_requested=lambda state: state == "closed",
    )
    legacy = legacy_write_status(execution.outcome)

    assert execution.outcome.write_completed is True
    assert execution.outcome.readback_completed is True
    assert execution.outcome.state_matches_requested is False
    assert legacy.write_completed is True
    assert legacy.readback_completed is False
    assert execution.outcome.warning is not None
    assert "does not match the requested state" in execution.outcome.warning


@pytest.mark.asyncio
async def test_known_write_failure_is_not_replayed_and_records_failed_outcome() -> None:
    writes = 0

    async def write() -> GitHubRequestResult[None]:
        nonlocal writes
        writes += 1
        raise GitHubRequestError(
            "validation failed",
            status_code=422,
            ambiguous=False,
            metadata=GitHubRequestMetadata(request_id="req-failed"),
        )

    async def readback() -> str:
        return "open"

    execution = await execute_write_readback(
        resource="Issue transition",
        write=write,
        readback=readback,
        state_matches_requested=lambda state: state == "closed",
    )

    assert writes == 1
    assert execution.error is not None
    assert execution.outcome.write_completed is False
    assert execution.outcome.readback_completed is True
    assert execution.outcome.state_matches_requested is False
    assert execution.outcome.request_id == "req-failed"


@pytest.mark.asyncio
async def test_readback_failure_preserves_completed_write_without_verification() -> None:
    async def write() -> GitHubRequestResult[None]:
        return GitHubRequestResult(
            value=None,
            metadata=GitHubRequestMetadata(request_id="req-readback"),
        )

    async def readback() -> str:
        raise RuntimeError("readback unavailable")

    execution = await execute_write_readback(
        resource="Release",
        write=write,
        readback=readback,
        state_matches_requested=lambda state: state == "published",
    )

    assert execution.outcome.write_completed is True
    assert execution.outcome.readback_completed is False
    assert execution.outcome.state_matches_requested is None
    assert execution.outcome.request_id == "req-readback"
    assert execution.outcome.warning is not None
    assert "Do not retry automatically" in execution.outcome.warning


@pytest.mark.asyncio
async def test_ambiguous_transport_outcome_is_unknown_and_never_replayed() -> None:
    writes = 0
    readbacks = 0

    async def write() -> GitHubRequestResult[None]:
        nonlocal writes
        writes += 1
        raise GitHubRequestError(
            "transport timeout",
            retryable=True,
            ambiguous=True,
            metadata=GitHubRequestMetadata(request_id="req-ambiguous"),
        )

    async def readback() -> str:
        nonlocal readbacks
        readbacks += 1
        raise RuntimeError("readback timeout")

    execution = await execute_write_readback(
        resource="Workflow dispatch",
        write=write,
        readback=readback,
        state_matches_requested=lambda state: state == "dispatched",
    )

    assert writes == 1
    assert readbacks == 1
    assert execution.outcome.write_completed is None
    assert execution.outcome.readback_completed is False
    assert execution.outcome.state_matches_requested is None
    assert execution.outcome.request_id == "req-ambiguous"
    assert execution.outcome.warning is not None
    assert "outcome is unknown" in execution.outcome.warning
    assert "Do not retry automatically" in execution.outcome.warning
    assert "re-read authoritative state" in execution.outcome.warning


@pytest.mark.asyncio
async def test_ambiguous_transport_with_matching_readback_stays_unknown_but_no_retry() -> None:
    async def write() -> GitHubRequestResult[None]:
        raise GitHubRequestError(
            "transport reset",
            retryable=True,
            ambiguous=True,
        )

    async def readback() -> str:
        return "requested"

    execution = await execute_write_readback(
        resource="Exact write",
        write=write,
        readback=readback,
        state_matches_requested=lambda state: state == "requested",
    )

    assert execution.outcome.write_completed is None
    assert execution.outcome.readback_completed is True
    assert execution.outcome.state_matches_requested is True
    assert execution.outcome.warning is not None
    assert "Do not retry the mutation" in execution.outcome.warning


@dataclass
class FakeServerClient:
    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        result = await self.run(*args, **kwargs)
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


def _server_context(client: FakeServerClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=True, allow_pr_merge=True),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.mark.asyncio
async def test_merge_adapter_rejects_semantic_readback_mismatch() -> None:
    head_sha = "b" * 40
    client = FakeServerClient(
        [
            {"base": {"sha": "a" * 40}, "head": {"sha": head_sha}},
            {"stdout": ""},
            {
                "number": 224,
                "url": "https://github.com/octo/repo/pull/224",
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefOid": head_sha,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
            },
        ]
    )

    result = await gh_merge_pr("octo", "repo", 224, head_sha, "merge", ctx=_server_context(client))

    assert len(client.calls) == 3
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.merged is False
    assert result.warning is not None
    assert "does not match the requested state" in result.warning


@pytest.mark.asyncio
async def test_merge_adapter_preserves_unknown_transport_outcome_and_does_not_replay() -> None:
    head_sha = "b" * 40
    client = FakeServerClient(
        [
            {"base": {"sha": "a" * 40}, "head": {"sha": head_sha}},
            GitHubRequestError(
                "transport reset",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-merge-ambiguous"),
            ),
            {
                "number": 224,
                "url": "https://github.com/octo/repo/pull/224",
                "state": "MERGED",
                "mergedAt": "2026-08-10T22:00:00Z",
                "mergeCommit": {"oid": "c" * 40},
                "headRefOid": head_sha,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
            },
        ]
    )

    result = await gh_merge_pr("octo", "repo", 224, head_sha, "squash", ctx=_server_context(client))

    assert len(client.calls) == 3
    assert sum(1 for call, _ in client.calls if call[:2] == ("pr", "merge")) == 1
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.merged is True
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert result.request_id == "req-merge-ambiguous"


@pytest.mark.asyncio
async def test_review_adapter_checks_head_after_viewer_and_immediately_before_write() -> None:
    head_sha = "b" * 40
    review_url = "https://github.com/octo/repo/pull/224#pullrequestreview-91"
    client = FakeServerClient(
        [
            {"login": "reviewer"},
            {
                "base": {"sha": "a" * 40},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
            {"id": 91, "state": "APPROVED", "html_url": review_url},
            {
                "id": 91,
                "state": "APPROVED",
                "body": "Reviewed exact revision.",
                "html_url": review_url,
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
        ctx=_server_context(client),
        body="Reviewed exact revision.",
    )

    assert result.readback_completed is True
    assert client.calls[0][0] == ("api", "user", "-X", "GET")
    assert client.calls[1][0] == (
        "api",
        "repos/octo/repo/pulls/224",
        "-X",
        "GET",
    )
    assert client.calls[2][0][:4] == (
        "api",
        "repos/octo/repo/pulls/224/reviews",
        "-X",
        "POST",
    )


@pytest.mark.asyncio
async def test_review_adapter_rejects_semantic_readback_mismatch() -> None:
    head_sha = "b" * 40
    review_url = "https://github.com/octo/repo/pull/224#pullrequestreview-91"
    client = FakeServerClient(
        [
            {"login": "reviewer"},
            {
                "base": {"sha": "a" * 40},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
            {"id": 91, "state": "APPROVED", "html_url": review_url},
            {
                "id": 91,
                "state": "APPROVED",
                "body": "Reviewed exact revision.",
                "html_url": review_url,
                "commit_id": "c" * 40,
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
        ctx=_server_context(client),
        body="Reviewed exact revision.",
    )

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.warning is not None
    assert "does not match the requested state" in result.warning


@pytest.mark.asyncio
async def test_review_adapter_preserves_ambiguous_write_without_replay() -> None:
    head_sha = "b" * 40
    client = FakeServerClient(
        [
            {"login": "reviewer"},
            {
                "base": {"sha": "a" * 40},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
            GitHubRequestError(
                "transport reset",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-review-ambiguous"),
            ),
        ]
    )

    result = await gh_submit_pr_review(
        "octo", "repo", 224, head_sha, "approve", ctx=_server_context(client)
    )

    assert len(client.calls) == 3
    assert (
        sum(
            1
            for call, _ in client.calls
            if call[:4]
            == (
                "api",
                "repos/octo/repo/pulls/224/reviews",
                "-X",
                "POST",
            )
        )
        == 1
    )
    assert result.write_completed is None
    assert result.readback_completed is False
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert result.request_id == "req-review-ambiguous"
