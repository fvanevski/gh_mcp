"""Regression tests for GraphQL request-authority classification."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_gh_server.gh_client import GhClient, _infer_request_kind, _request_policy
from mcp_gh_server.request_governor import (
    READ_REQUEST,
    WRITE_REQUEST,
    GitHubRequestError,
    GitHubRequestGovernor,
    GitHubRequestKind,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.pr_reviews import _REVIEW_THREADS_QUERY

_QUERY = "query PullRequestReviewState { viewer { login } }"
_MUTATION = "mutation UpdateThing { __typename }"


def _graphql_args(document: str) -> tuple[str, ...]:
    return (
        "api",
        "graphql",
        "-f",
        f"query={document}",
        "-F",
        "owner=octo",
    )


def _review_state_graphql_args() -> tuple[str, ...]:
    return (
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-F",
        "owner=octo",
        "-F",
        "repo=repo",
        "-F",
        "number=19",
        "-F",
        "first=100",
    )


def _fake_failing_gh(tmp_path: Path) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

counter = Path(os.environ["MCP_GH_TEST_ATTEMPT_COUNTER"])
count = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(count + 1))
print("connection reset by peer", file=sys.stderr)
raise SystemExit(1)
"""
    )
    executable.chmod(0o700)
    return executable


def test_exact_pr_review_graphql_query_is_governed_as_read() -> None:
    args = _review_state_graphql_args()

    assert _infer_request_kind(args) is GitHubRequestKind.READ
    assert _request_policy(args) == READ_REQUEST


def test_graphql_query_is_read_but_ambiguous_or_mutating_documents_fail_closed() -> None:
    query_args = _graphql_args(f"# exact-head evidence\n{_QUERY}")
    mutation_args = _graphql_args(_MUTATION)
    mixed_args = _graphql_args(f"{_QUERY} {_MUTATION}")
    anonymous_args = _graphql_args("{ viewer { login } }")
    indirect_args = ("api", "graphql", "-F", "query=@query.graphql")
    input_args = (*_graphql_args(_QUERY), "--input", "payload.json")
    inline_input_args = (*_graphql_args(_QUERY), "--input=payload.json")

    assert _infer_request_kind(query_args) is GitHubRequestKind.READ
    assert _request_policy(query_args) == READ_REQUEST
    for args in (
        mutation_args,
        mixed_args,
        anonymous_args,
        indirect_args,
        input_args,
        inline_input_args,
    ):
        assert _infer_request_kind(args) is GitHubRequestKind.WRITE
        assert _request_policy(args) == WRITE_REQUEST


def test_graphql_query_with_non_post_override_does_not_gain_read_authority() -> None:
    args = (*_graphql_args(_QUERY), "-X", "PATCH")

    assert _infer_request_kind(args) is GitHubRequestKind.WRITE
    assert _request_policy(args) == WRITE_REQUEST


async def test_exact_pr_review_query_transport_failure_uses_safe_read_retry_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_failing_gh(tmp_path)
    counter = tmp_path / "query-attempts"
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("MCP_GH_TEST_ATTEMPT_COUNTER", str(counter))
    client = GhClient(
        Settings(),
        governor=GitHubRequestGovernor(
            max_read_attempts=2,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
        ),
    )

    with pytest.raises(GitHubRequestError) as raised:
        await client.run(*_review_state_graphql_args())

    assert counter.read_text() == "2"
    assert raised.value.retryable is True
    assert raised.value.ambiguous is False


async def test_graphql_mutation_transport_failure_remains_nonretryable_and_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_failing_gh(tmp_path)
    counter = tmp_path / "mutation-attempts"
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("MCP_GH_TEST_ATTEMPT_COUNTER", str(counter))
    client = GhClient(
        Settings(),
        governor=GitHubRequestGovernor(
            max_read_attempts=3,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
        ),
    )

    with pytest.raises(GitHubRequestError) as raised:
        await client.run(*_graphql_args(_MUTATION))

    assert counter.read_text() == "1"
    assert raised.value.retryable is True
    assert raised.value.ambiguous is True
