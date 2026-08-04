"""Regression tests for write-command execution and structured readback."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.models import CommandApproval
from mcp_gh_server.server import (
    AppContext,
    gh_create_branch,
    gh_create_comment,
    gh_create_issue,
    gh_create_label,
    gh_create_milestone,
    gh_create_pr,
    gh_create_release,
    gh_create_repo,
    gh_edit_issue,
    gh_edit_label,
    gh_edit_pr,
    gh_list_milestones,
    gh_run_workflow,
)
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record gh invocations and return queued results."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.results.pop(0)

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(client: FakeGhClient) -> Any:
    app = AppContext(client=client, settings=Settings())  # type: ignore[arg-type]
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=app),
    )


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
        approval=CommandApproval(approved=True),
    )

    assert result.number == 42
    create_args, create_kwargs = client.calls[0]
    assert create_args[:2] == ("issue", "create")
    assert "--json" not in create_args
    assert create_kwargs == {"json_output": False}
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
        approval=CommandApproval(approved=True),
    )

    assert result.name == "needs triage"
    create_args, create_kwargs = client.calls[0]
    assert create_args[:3] == ("label", "create", "needs triage")
    assert "--json" not in create_args
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
        approval=CommandApproval(approved=True),
    )

    assert result.name == "renamed"
    create_args = client.calls[0][0]
    assert "--name" in create_args
    assert "--new-name" not in create_args
    assert "--json" not in create_args
    assert client.calls[1][0] == ("api", "repos/octo/repo/labels/renamed")


@pytest.mark.asyncio
async def test_create_milestone_uses_input_then_reads_by_number() -> None:
    client = FakeGhClient(
        [
            {"stdout": '{"number":7,"title":"v1","url":"api-url"}'},
            {"number": 7, "title": "v1", "url": "api-url"},
        ]
    )

    result = await gh_create_milestone(
        "octo",
        "repo",
        "v1",
        ctx=_context(client),
        approval=CommandApproval(approved=True),
    )

    assert result.number == 7
    create_args, create_kwargs = client.calls[0]
    assert create_args[:4] == (
        "api",
        "repos/octo/repo/milestones",
        "-X",
        "POST",
    )
    assert "--input" in create_args
    assert "-i" not in create_args
    assert create_kwargs == {"json_output": False}
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
        approval=CommandApproval(approved=True),
    )

    assert result.number == 9
    assert client.calls[0][1] == {"json_output": False}
    assert "--json" not in client.calls[0][0]
    assert client.calls[1][0][:3] == ("pr", "view", url)


@pytest.mark.asyncio
async def test_create_repo_uses_visibility_and_readme_then_reads_repo() -> None:
    url = "https://github.example/octo/new-repo"
    client = FakeGhClient(
        [
            {"stdout": ""},
            {"login": "octo"},
            {"nameWithOwner": "octo/new-repo", "url": url},
        ]
    )

    result = await gh_create_repo(
        "new-repo",
        ctx=_context(client),
        auto_init=True,
        approval=CommandApproval(approved=True),
    )

    assert result.name == "octo/new-repo"
    create_args, create_kwargs = client.calls[0]
    assert "--public" in create_args
    assert "--add-readme" in create_args
    assert "--json" not in create_args
    assert create_kwargs == {"json_output": False}
    assert client.calls[1][0] == ("api", "user")
    assert client.calls[2][0] == (
        "repo",
        "view",
        "octo/new-repo",
        "--json",
        "nameWithOwner,url",
    )


@pytest.mark.asyncio
async def test_create_release_executes_then_reads_tag() -> None:
    url = "https://github.com/octo/repo/releases/tag/v1"
    client = FakeGhClient(
        [
            {"stdout": url},
            {"tagName": "v1", "url": url},
        ]
    )

    result = await gh_create_release(
        "octo",
        "repo",
        "v1",
        ctx=_context(client),
        body="Notes",
        approval=CommandApproval(approved=True),
    )

    assert result.tag_name == "v1"
    assert client.calls[0][1] == {"json_output": False}
    assert "--json" not in client.calls[0][0]
    assert client.calls[1][0] == (
        "release",
        "view",
        "v1",
        "--repo",
        "octo/repo",
        "--json",
        "tagName,url",
    )


@pytest.mark.asyncio
async def test_edit_issue_resolves_milestone_then_reads_issue() -> None:
    url = "https://github.com/octo/repo/issues/4"
    client = FakeGhClient(
        [
            {"title": "Version 1"},
            {"stdout": ""},
            {"number": 4, "title": "Updated", "state": "OPEN", "url": url},
        ]
    )

    result = await gh_edit_issue(
        "octo",
        "repo",
        4,
        ctx=_context(client),
        title="Updated",
        milestone=7,
        approval=CommandApproval(approved=True),
    )

    assert result.title == "Updated"
    assert client.calls[0][0] == ("api", "repos/octo/repo/milestones/7")
    edit_args, edit_kwargs = client.calls[1]
    assert edit_args[-2:] == ("--milestone", "Version 1")
    assert "--json" not in edit_args
    assert edit_kwargs == {"json_output": False}
    assert client.calls[2][0][:3] == ("issue", "view", "4")


@pytest.mark.asyncio
async def test_run_workflow_reads_returned_run_url() -> None:
    url = "https://github.com/octo/repo/actions/runs/123"
    client = FakeGhClient(
        [
            {"stdout": f"Workflow dispatched: {url}\n"},
            {"databaseId": 123, "url": url},
        ]
    )

    result = await gh_run_workflow(
        "octo",
        "repo",
        99,
        ctx=_context(client),
        fields=["environment=test"],
        approval=CommandApproval(approved=True),
    )

    assert result.run_id == 123
    dispatch_args, dispatch_kwargs = client.calls[0]
    assert "--json" not in dispatch_args
    assert dispatch_args[-2:] == ("-f", "environment=test")
    assert dispatch_kwargs == {"json_output": False}
    assert client.calls[1][0][:3] == ("run", "view", "123")


@pytest.mark.asyncio
async def test_run_workflow_handles_dispatch_without_run_url() -> None:
    client = FakeGhClient([{"stdout": ""}])

    result = await gh_run_workflow(
        "octo",
        "repo",
        99,
        ctx=_context(client),
        approval=CommandApproval(approved=True),
    )

    assert result.run_id is None
    assert result.url is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_comment_branch_and_pr_edit_use_raw_writes() -> None:
    comment_url = "https://github.com/octo/repo/issues/4#issuecomment-5"
    comment_client = FakeGhClient([{"stdout": comment_url}])
    comment = await gh_create_comment(
        "octo",
        "repo",
        4,
        "Hello",
        ctx=_context(comment_client),
        approval=CommandApproval(approved=True),
    )
    assert comment.url == comment_url
    assert comment_client.calls[0][1] == {"json_output": False}

    branch_client = FakeGhClient([{"stdout": ""}])
    await gh_create_branch(
        "octo",
        "repo",
        4,
        "feature",
        ctx=_context(branch_client),
        approval=CommandApproval(approved=True),
    )
    assert branch_client.calls[0][1] == {"json_output": False}

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
        approval=CommandApproval(approved=True),
    )
    assert result.title == "Updated PR"
    assert pr_client.calls[0][1] == {"json_output": False}
    assert pr_client.calls[1][0][:3] == ("pr", "view", "9")
