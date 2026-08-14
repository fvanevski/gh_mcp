"""Regression tests for exact-commit branch write/readback semantics."""

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
from mcp_gh_server.server import AppContext, gh_create_branch_from_sha
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record exact branch-write calls and return queued values."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


def _context(client: FakeGhClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=True),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.mark.asyncio
async def test_exact_branch_success_uses_independent_authoritative_readback() -> None:
    sha = "a" * 40
    ref = "refs/heads/feature/exact"
    client = FakeGhClient(
        [
            {"sha": sha},
            {"ref": ref, "object": {"sha": sha}},
            {"ref": ref, "object": {"sha": sha}},
        ]
    )

    result = await gh_create_branch_from_sha(
        "octo", "repo", "feature/exact", sha, ctx=_context(client)
    )

    assert len(client.calls) == 3
    assert client.calls[0][0] == (
        "api",
        f"repos/octo/repo/git/commits/{sha}",
        "-X",
        "GET",
    )
    assert client.calls[1][0][:4] == (
        "api",
        "repos/octo/repo/git/refs",
        "-X",
        "POST",
    )
    assert client.calls[2][0] == (
        "api",
        "repos/octo/repo/git/ref/heads/feature/exact",
        "-X",
        "GET",
    )
    assert result.created is True
    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.warning is None


@pytest.mark.asyncio
async def test_exact_branch_success_with_mismatched_readback_is_not_verified_success() -> None:
    sha = "a" * 40
    other_sha = "b" * 40
    ref = "refs/heads/feature"
    client = FakeGhClient(
        [
            {"sha": sha},
            {"ref": ref, "object": {"sha": sha}},
            {"ref": ref, "object": {"sha": other_sha}},
        ]
    )

    result = await gh_create_branch_from_sha("octo", "repo", "feature", sha, ctx=_context(client))

    assert result.created is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.warning is not None
    assert "does not match the requested state" in result.warning


@pytest.mark.asyncio
async def test_exact_branch_ambiguous_create_is_never_replayed() -> None:
    sha = "a" * 40
    ref = "refs/heads/feature"
    client = FakeGhClient(
        [
            {"sha": sha},
            GitHubRequestError(
                "transport reset",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-branch-ambiguous"),
            ),
            {"ref": ref, "object": {"sha": sha}},
        ]
    )

    result = await gh_create_branch_from_sha("octo", "repo", "feature", sha, ctx=_context(client))

    assert len(client.calls) == 3
    assert (
        sum(
            1
            for call, _ in client.calls
            if call[:4] == ("api", "repos/octo/repo/git/refs", "-X", "POST")
        )
        == 1
    )
    assert result.created is False
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-branch-ambiguous"
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "Do not retry the mutation" in result.warning


@pytest.mark.asyncio
async def test_exact_branch_known_conflict_at_other_sha_preserves_no_overwrite_error() -> None:
    sha = "a" * 40
    other_sha = "b" * 40
    ref = "refs/heads/feature"
    client = FakeGhClient(
        [
            {"sha": sha},
            RuntimeError("Reference already exists"),
            {"ref": ref, "object": {"sha": other_sha}},
        ]
    )

    with pytest.raises(RuntimeError, match=f"already exists at {other_sha}"):
        await gh_create_branch_from_sha("octo", "repo", "feature", sha, ctx=_context(client))

    assert len(client.calls) == 3
