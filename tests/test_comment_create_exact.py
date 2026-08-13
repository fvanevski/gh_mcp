"""Regression tests for exact REST issue-comment creation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_gh_server.write_tool_schema as write_tool_schema
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext, gh_create_comment
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools import issues as issue_tools


@dataclass
class CommentClient:
    """Protocol fake that records governed writes, exact reads, and JSON payloads."""

    read_results: list[Any] = field(default_factory=list)
    write_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        if "--input" in args:
            input_index = args.index("--input") + 1
            self.payloads.append(json.loads(Path(args[input_index]).read_text()))
        result = self.write_results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


def _context(client: CommentClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=True),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _comment(
    *,
    comment_id: int = 5,
    owner: str = "octo",
    repo: str = "repo",
    issue_number: int = 4,
    body: str = "Hello",
    html_surface: str = "issues",
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "url": f"https://api.github.com/repos/{owner}/{repo}/issues/comments/{comment_id}",
        "html_url": (
            f"https://github.com/{owner}/{repo}/{html_surface}/{issue_number}"
            f"#issuecomment-{comment_id}"
        ),
        "issue_url": f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        "body": body,
    }


def _write_calls(client: CommentClient) -> list[tuple[str, ...]]:
    return [args for kind, args, _ in client.calls if kind == "write"]


def _read_calls(client: CommentClient) -> list[tuple[str, ...]]:
    return [args for kind, args, _ in client.calls if kind == "read"]


def test_public_comment_facade_uses_exact_issue_implementation() -> None:
    assert write_tool_schema._gh_create_comment is issue_tools.gh_create_comment


@pytest.mark.parametrize("html_surface", ["issues", "pull"])
async def test_comment_success_uses_one_rest_post_and_exact_id_readback(
    html_surface: str,
) -> None:
    created = _comment(html_surface=html_surface)
    client = CommentClient(
        read_results=[created],
        write_results=[
            GitHubRequestResult(
                value=created,
                metadata=GitHubRequestMetadata(request_id="req-comment-5"),
            )
        ],
    )

    result = await gh_create_comment("octo", "repo", 4, "Hello", ctx=_context(client))

    assert result.precondition_checked is False
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-comment-5"
    assert result.warning is None
    assert result.comment_id == 5
    assert result.url == created["html_url"]
    assert client.payloads == [{"body": "Hello"}]

    writes = _write_calls(client)
    reads = _read_calls(client)
    assert len(writes) == 1
    assert writes[0][:5] == (
        "api",
        "repos/octo/repo/issues/4/comments",
        "-X",
        "POST",
        "--input",
    )
    assert len(writes[0]) == 6
    assert reads == [("api", "repos/octo/repo/issues/comments/5", "-X", "GET")]
    assert all(args[:2] != ("issue", "comment") for _, args, _ in client.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("body", "different"),
        ("issue_url", "https://api.github.com/repos/octo/repo/issues/99"),
        ("url", "https://api.github.com/repos/other/repo/issues/comments/5"),
    ],
)
async def test_comment_readback_mismatch_is_not_reported_as_verified(
    field: str,
    value: str,
) -> None:
    created = _comment()
    readback = _comment()
    readback[field] = value
    client = CommentClient(read_results=[readback], write_results=[created])

    result = await gh_create_comment("octo", "repo", 4, "Hello", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.comment_id == 5
    assert result.warning is not None
    assert "does not match the requested state" in result.warning
    assert len(_write_calls(client)) == 1


async def test_comment_readback_failure_preserves_completed_write_without_retry() -> None:
    created = _comment()
    client = CommentClient(
        read_results=[RuntimeError("readback failed")],
        write_results=[
            GitHubRequestResult(
                value=created,
                metadata=GitHubRequestMetadata(request_id="req-readback-failed"),
            )
        ],
    )

    result = await gh_create_comment("octo", "repo", 4, "Hello", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-readback-failed"
    assert result.comment_id == 5
    assert result.url == created["html_url"]
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning
    assert len(_write_calls(client)) == 1
    assert len(_read_calls(client)) == 1


async def test_comment_known_write_failure_is_false_and_not_retried() -> None:
    failure = GitHubRequestError(
        "validation failed",
        status_code=422,
        ambiguous=False,
        metadata=GitHubRequestMetadata(request_id="req-known-failure"),
    )
    client = CommentClient(write_results=[failure])

    result = await gh_create_comment("octo", "repo", 4, "Hello", ctx=_context(client))

    assert result.write_completed is False
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-known-failure"
    assert result.comment_id is None
    assert result.url == ""
    assert len(_write_calls(client)) == 1
    assert _read_calls(client) == []


async def test_comment_ambiguous_write_remains_unknown_and_never_duplicates() -> None:
    failure = GitHubRequestError(
        "connection lost after request send",
        retryable=True,
        ambiguous=True,
        metadata=GitHubRequestMetadata(request_id="req-ambiguous"),
    )
    client = CommentClient(write_results=[failure])

    result = await gh_create_comment("octo", "repo", 4, "Hello", ctx=_context(client))

    assert result.write_completed is None
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-ambiguous"
    assert result.comment_id is None
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "Do not retry automatically" in result.warning
    assert len(_write_calls(client)) == 1
    assert _read_calls(client) == []


async def test_comment_success_without_stable_identity_cannot_claim_readback() -> None:
    client = CommentClient(
        write_results=[
            GitHubRequestResult(
                value={"html_url": "https://github.com/octo/repo/issues/4#issuecomment-5"},
                metadata=GitHubRequestMetadata(request_id="req-missing-id"),
            )
        ]
    )

    result = await gh_create_comment("octo", "repo", 4, "Hello", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-missing-id"
    assert result.comment_id is None
    assert result.url == ""
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning
    assert len(_write_calls(client)) == 1
    assert _read_calls(client) == []
