"""Regression tests for exact Git-reference identity and error classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext, gh_get_ref
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record exact ref reads and return queued values."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _context(client: FakeGhClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _object_url(kind: str, sha: str) -> str:
    return f"https://api.github.com/repos/octo/repo/git/{kind}/{sha}"


async def test_branch_ref_returns_exact_commit_identity() -> None:
    sha = "a" * 40
    client = FakeGhClient(
        [
            {
                "ref": "refs/heads/main",
                "object": {
                    "type": "commit",
                    "sha": sha,
                    "url": _object_url("commits", sha),
                },
            }
        ]
    )

    result = await gh_get_ref("octo", "repo", "heads/main", ctx=_context(client))

    assert result.ref == "refs/heads/main"
    assert result.found is True
    assert result.object_type == "commit"
    assert result.object_sha == sha
    assert result.object_url == _object_url("commits", sha)
    assert result.peeled_commit_sha is None
    assert client.calls == [
        (
            ("api", "repos/octo/repo/git/ref/heads/main", "-X", "GET"),
            {},
        )
    ]


async def test_lightweight_tag_preserves_direct_commit_identity() -> None:
    sha = "b" * 40
    client = FakeGhClient(
        [
            {
                "ref": "refs/tags/v1.0.0",
                "object": {
                    "type": "commit",
                    "sha": sha,
                    "url": _object_url("commits", sha),
                },
            }
        ]
    )

    result = await gh_get_ref("octo", "repo", "tags/v1.0.0", ctx=_context(client))

    assert result.ref == "refs/tags/v1.0.0"
    assert result.object_type == "commit"
    assert result.object_sha == sha
    assert result.peeled_commit_sha is None
    assert len(client.calls) == 1


async def test_annotated_tag_preserves_tag_object_and_peeled_commit() -> None:
    tag_sha = "c" * 40
    commit_sha = "d" * 40
    client = FakeGhClient(
        [
            {
                "ref": "refs/tags/v1.0.0",
                "object": {
                    "type": "tag",
                    "sha": tag_sha,
                    "url": _object_url("tags", tag_sha),
                },
            },
            {
                "sha": tag_sha,
                "object": {
                    "type": "commit",
                    "sha": commit_sha,
                    "url": _object_url("commits", commit_sha),
                },
            },
        ]
    )

    result = await gh_get_ref("octo", "repo", "tags/v1.0.0", ctx=_context(client))

    assert result.object_type == "tag"
    assert result.object_sha == tag_sha
    assert result.object_url == _object_url("tags", tag_sha)
    assert result.peeled_commit_sha == commit_sha
    assert client.calls[1][0] == (
        "api",
        f"repos/octo/repo/git/tags/{tag_sha}",
        "-X",
        "GET",
    )


async def test_nested_annotated_tags_are_peeled_by_exact_object_sha() -> None:
    outer_sha = "c" * 40
    inner_sha = "d" * 40
    commit_sha = "e" * 40
    client = FakeGhClient(
        [
            {
                "ref": "refs/tags/release",
                "object": {
                    "type": "tag",
                    "sha": outer_sha,
                    "url": _object_url("tags", outer_sha),
                },
            },
            {
                "sha": outer_sha,
                "object": {
                    "type": "tag",
                    "sha": inner_sha,
                    "url": _object_url("tags", inner_sha),
                },
            },
            {
                "sha": inner_sha,
                "object": {
                    "type": "commit",
                    "sha": commit_sha,
                    "url": _object_url("commits", commit_sha),
                },
            },
        ]
    )

    result = await gh_get_ref("octo", "repo", "tags/release", ctx=_context(client))

    assert result.object_sha == outer_sha
    assert result.peeled_commit_sha == commit_sha
    assert [call[0][1] for call in client.calls] == [
        "repos/octo/repo/git/ref/tags/release",
        f"repos/octo/repo/git/tags/{outer_sha}",
        f"repos/octo/repo/git/tags/{inner_sha}",
    ]


async def test_missing_ref_requires_successful_contents_permission_probe() -> None:
    missing = GitHubRequestError("not found", status_code=404)
    client = FakeGhClient([missing, [{"name": "main"}]])

    result = await gh_get_ref("octo", "repo", "heads/missing", ctx=_context(client))

    assert result.ref == "refs/heads/missing"
    assert result.found is False
    assert result.object_type is None
    assert result.object_sha is None
    assert result.object_url is None
    assert result.peeled_commit_sha is None
    assert client.calls[1][0] == (
        "api",
        "repos/octo/repo/branches",
        "-X",
        "GET",
        "-f",
        "per_page=1",
    )


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "heads",
        "heads/",
        "refs/heads/main",
        "heads/feature*",
        "heads/foo..bar",
        "tags/release@{1}",
        "tags/.hidden",
        "heads/feature.lock",
    ],
)
async def test_malformed_or_ambiguous_ref_is_rejected_before_github(ref: str) -> None:
    client = FakeGhClient([])

    with pytest.raises(ValidationError):
        await gh_get_ref("octo", "repo", ref, ctx=_context(client))

    assert client.calls == []


@pytest.mark.parametrize(
    "error",
    [
        GitHubRequestError("unauthorized", status_code=401),
        GitHubRequestError("forbidden", status_code=403),
        GitHubRequestError("transport reset", retryable=True),
    ],
)
async def test_auth_permission_and_transport_failures_are_not_missing(
    error: GitHubRequestError,
) -> None:
    client = FakeGhClient([error])

    with pytest.raises(GitHubRequestError) as raised:
        await gh_get_ref("octo", "repo", "heads/main", ctx=_context(client))

    assert raised.value is error
    assert len(client.calls) == 1


async def test_ref_404_with_failed_contents_probe_is_not_missing() -> None:
    missing_like = GitHubRequestError("not found", status_code=404)
    forbidden = GitHubRequestError("contents forbidden", status_code=403)
    client = FakeGhClient([missing_like, forbidden])

    with pytest.raises(GitHubRequestError) as raised:
        await gh_get_ref("octo", "repo", "tags/private", ctx=_context(client))

    assert raised.value is forbidden
    assert len(client.calls) == 2


async def test_exact_lookup_rejects_mismatched_returned_ref() -> None:
    sha = "f" * 40
    client = FakeGhClient(
        [
            {
                "ref": "refs/heads/main-extra",
                "object": {
                    "type": "commit",
                    "sha": sha,
                    "url": _object_url("commits", sha),
                },
            }
        ]
    )

    with pytest.raises(RuntimeError, match="different ref"):
        await gh_get_ref("octo", "repo", "heads/main", ctx=_context(client))
