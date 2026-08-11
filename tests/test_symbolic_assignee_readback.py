"""Regression coverage for symbolic assignee selectors in semantic readback."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.request_governor import GitHubRequestResult
from mcp_gh_server.server import (
    AppContext,
    gh_create_issue,
    gh_create_pr,
    gh_edit_issue,
    gh_edit_pr,
)
from mcp_gh_server.settings import Settings


@dataclass
class MetadataAwareClient:
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
        settings=Settings(allow_write_commands=True),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _write(value: Any) -> GitHubRequestResult[Any]:
    return GitHubRequestResult(value=value)


def _write_args(client: MetadataAwareClient) -> tuple[str, ...]:
    return next(args for kind, args, _ in client.calls if kind == "write")


@pytest.mark.asyncio
async def test_create_issue_resolves_me_for_semantic_readback() -> None:
    url = "https://github.com/octo/repo/issues/42"
    client = MetadataAwareClient(
        read_results=[
            {"login": "octocat"},
            {
                "title": "Self assigned",
                "number": 42,
                "url": url,
                "assignees": [{"login": "octocat"}],
            },
        ],
        write_results=[_write({"stdout": url})],
    )

    result = await gh_create_issue(
        "octo",
        "repo",
        "Self assigned",
        assignees=["@me"],
        ctx=_context(client),
    )

    assert result.readback_completed is True
    assert client.calls[0][1] == ("api", "user")
    args = _write_args(client)
    assert args[args.index("--assignee") + 1] == "@me"


@pytest.mark.asyncio
async def test_create_pr_resolves_me_for_semantic_readback() -> None:
    url = "https://github.com/octo/repo/pull/9"
    client = MetadataAwareClient(
        read_results=[
            {"login": "octocat"},
            {
                "title": "Self assigned",
                "body": "Body",
                "number": 9,
                "url": url,
                "headRefName": "feature",
                "baseRefName": "main",
                "isDraft": False,
                "labels": [],
                "assignees": [{"login": "octocat"}],
                "reviewRequests": [],
            },
        ],
        write_results=[_write({"stdout": url})],
    )

    result = await gh_create_pr(
        "octo",
        "repo",
        "Self assigned",
        "Body",
        "feature",
        "main",
        assignees=["@me"],
        ctx=_context(client),
    )

    assert result.readback_completed is True
    assert client.calls[0][1] == ("api", "user")
    args = _write_args(client)
    assert args[args.index("--assignee") + 1] == "@me"


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_edit_issue_resolves_me_for_add_and_remove_readback(remove: bool) -> None:
    assignees = [] if remove else [{"login": "octocat"}]
    client = MetadataAwareClient(
        read_results=[
            {"login": "octocat"},
            {
                "title": "Issue",
                "number": 4,
                "state": "OPEN",
                "url": "https://github.com/octo/repo/issues/4",
                "assignees": assignees,
            },
        ],
        write_results=[_write({"stdout": ""})],
    )
    kwargs = {"assignees_remove": ["@me"]} if remove else {"assignees_add": ["@me"]}

    result = await gh_edit_issue(
        "octo",
        "repo",
        4,
        ctx=_context(client),
        **kwargs,
    )

    assert result.readback_completed is True
    args = _write_args(client)
    flag = "--remove-assignee" if remove else "--add-assignee"
    assert args[args.index(flag) + 1] == "@me"


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_edit_pr_resolves_me_for_add_and_remove_readback(remove: bool) -> None:
    assignees = [] if remove else [{"login": "octocat"}]
    client = MetadataAwareClient(
        read_results=[
            {"login": "octocat"},
            {
                "title": "PR",
                "url": "https://github.com/octo/repo/pull/9",
                "assignees": assignees,
            },
        ],
        write_results=[_write({"stdout": ""})],
    )
    kwargs = {"assignees_remove": ["@me"]} if remove else {"assignees_add": ["@me"]}

    result = await gh_edit_pr(
        "octo",
        "repo",
        9,
        ctx=_context(client),
        **kwargs,
    )

    assert result.readback_completed is True
    args = _write_args(client)
    flag = "--remove-assignee" if remove else "--add-assignee"
    assert args[args.index(flag) + 1] == "@me"
