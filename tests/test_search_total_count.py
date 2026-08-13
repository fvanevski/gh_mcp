"""Regression tests for authoritative GitHub Search total counts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.server import AppContext, gh_search_code, gh_search_issues, gh_search_repos
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record search/count reads and return queued values."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def clamp_max_results(self, requested: int | None) -> int:
        value = 30 if requested is None else requested
        if value < 0:
            raise ValueError("per_page must be zero or greater")
        return min(value, 100)


def _context(client: FakeGhClient) -> Any:
    app = AppContext(client=client, settings=Settings())  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.mark.parametrize(
    ("tool", "subcommand", "endpoint"),
    [
        (gh_search_repos, "repos", "search/repositories"),
        (gh_search_issues, "issues", "search/issues"),
        (gh_search_code, "code", "search/code"),
    ],
)
async def test_search_tools_use_authoritative_total_count_and_preserve_items(
    tool: Any,
    subcommand: str,
    endpoint: str,
) -> None:
    items = [
        {"opaque": "first", "nested": {"value": 1}},
        {"opaque": "second", "nested": {"value": 2}},
    ]
    client = FakeGhClient(
        [
            items,
            {"total_count": 5, "incomplete_results": False, "items": [{"ignored": True}]},
        ]
    )

    result = await tool(query="repo:octo/repo bug", per_page=2, ctx=_context(client))

    assert result.items == items
    assert result.total_count == 5
    assert result.truncated is True
    assert result.query == "repo:octo/repo bug"
    assert len(client.calls) == 2
    assert client.calls[0][0][:2] == ("search", subcommand)
    assert client.calls[0][0][-3:] == ("--", "repo:octo/repo", "bug")
    assert client.calls[1][0] == (
        "api",
        endpoint,
        "-X",
        "GET",
        "-f",
        "q=repo:octo/repo bug",
        "-f",
        "per_page=1",
    )


@pytest.mark.parametrize(
    ("tool", "subcommand", "endpoint"),
    [
        (gh_search_repos, "repos", "search/repositories"),
        (gh_search_issues, "issues", "search/issues"),
        (gh_search_code, "code", "search/code"),
    ],
)
@pytest.mark.parametrize(
    ("query", "expected_search_args", "expected_count_query"),
    [
        (
            '"vim plugin" language:vim',
            ("vim plugin", "language:vim"),
            'q="vim plugin" language:vim',
        ),
        (
            'label:"good first issue"',
            ("label:good first issue",),
            'q=label:"good first issue"',
        ),
    ],
)
async def test_search_tools_preserve_query_semantics_for_authoritative_count(
    tool: Any,
    subcommand: str,
    endpoint: str,
    query: str,
    expected_search_args: tuple[str, ...],
    expected_count_query: str,
) -> None:
    items = [{"opaque": "match"}]
    client = FakeGhClient(
        [
            items,
            {"total_count": 1, "incomplete_results": False, "items": [{"ignored": True}]},
        ]
    )

    result = await tool(query=query, per_page=1, ctx=_context(client))

    assert result.items == items
    assert result.total_count == 1
    assert result.truncated is False
    assert len(client.calls) == 2

    search_call = client.calls[0][0]
    assert search_call[:2] == ("search", subcommand)
    separator = search_call.index("--")
    assert search_call[separator + 1 :] == expected_search_args
    assert client.calls[1][0] == (
        "api",
        endpoint,
        "-X",
        "GET",
        "-f",
        expected_count_query,
        "-f",
        "per_page=1",
    )


async def test_zero_items_and_zero_total_are_complete() -> None:
    client = FakeGhClient([[], {"total_count": 0, "incomplete_results": False, "items": []}])

    result = await gh_search_repos(query="no-match", per_page=5, ctx=_context(client))

    assert result.total_count == 0
    assert result.items == []
    assert result.truncated is False


async def test_exact_returned_total_is_complete() -> None:
    items = [{"id": 1}, {"id": 2}]
    client = FakeGhClient(
        [items, {"total_count": 2, "incomplete_results": False, "items": [{"id": 1}]}]
    )

    result = await gh_search_issues(query="is:issue", per_page=2, ctx=_context(client))

    assert result.total_count == 2
    assert result.truncated is False


async def test_incomplete_count_evidence_is_rejected() -> None:
    client = FakeGhClient(
        [[{"id": 1}], {"total_count": 4, "incomplete_results": True, "items": [{"id": 1}]}]
    )

    with pytest.raises(RuntimeError, match="incomplete"):
        await gh_search_code(query="language:python", per_page=1, ctx=_context(client))


@pytest.mark.parametrize(
    "count_evidence",
    [
        {"incomplete_results": False},
        {"total_count": -1, "incomplete_results": False},
        {"total_count": "2", "incomplete_results": False},
        {"total_count": True, "incomplete_results": False},
        {"total_count": 1, "incomplete_results": False},
        {"total_count": 2},
        {"total_count": 2, "incomplete_results": "false"},
        [],
    ],
)
async def test_malformed_count_evidence_is_rejected(count_evidence: Any) -> None:
    client = FakeGhClient([[{"id": 1}, {"id": 2}], count_evidence])

    with pytest.raises(RuntimeError, match=r"count|malformed"):
        await gh_search_repos(query="repo:octo/repo", per_page=2, ctx=_context(client))
