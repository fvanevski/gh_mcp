"""GitHub MCP Server — comprehensive integration tests.

Requires a GITHUB_TOKEN with sufficient scopes for read operations.
Set GITHUB_TOKEN in the environment or .env file before running.

These tests hit a real GitHub repository — uses cli/cli as the test repo.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.gh_client import GhClient
from mcp_gh_server.server import (
    AppContext,
    gh_get_file_contents,
    gh_get_ref,
    gh_get_repo,
    gh_list_repository_tree,
)
from mcp_gh_server.settings import Settings


@pytest.fixture()
def settings() -> Settings:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN not set; skipping integration tests")
    return Settings(github_token=token)


@pytest.fixture()
def client(settings: Settings) -> GhClient:
    return GhClient(settings=settings)


@pytest.fixture()
def context(client: GhClient, settings: Settings) -> Any:
    app = AppContext(client=client, settings=settings)
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


class TestGhInfo:
    """Basic connectivity test: verify gh is authenticated."""

    async def test_info(self, client: GhClient) -> None:
        result = await client.run("auth", "status", "--json", "hosts")
        assert "hosts" in result


class TestSearchRepos:
    """Search test: find a well-known repository."""

    async def test_search_repos(self, client: GhClient) -> None:
        result = await client.run(
            "search",
            "repos",
            "--json",
            "fullName,name,description",
            "--limit",
            "5",
            "--",
            "cli/cli",
        )
        # gh search repos returns a list directly
        results = result if isinstance(result, list) else result.get("results", [])
        assert len(results) > 0
        full_names = [r["fullName"] for r in results]
        assert "cli/cli" in full_names


class TestListIssues:
    """List issues test on a public repository."""

    async def test_list_issues(self, client: GhClient) -> None:
        result = await client.run(
            "issue",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "title,number,state",
            "--limit",
            "5",
            "--state",
            "closed",
        )
        issues = result if isinstance(result, list) else []
        assert len(issues) > 0
        assert issues[0]["number"] > 0

    async def test_list_issues_label_filter_uses_supported_cli_contract(
        self, client: GhClient
    ) -> None:
        result = await client.run(
            "issue",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "title,number,state,labels",
            "--limit",
            "5",
            "--state",
            "all",
            "--label",
            "bug",
        )
        assert isinstance(result, list)


class TestGetRepo:
    """Get repository details test."""

    async def test_get_repo(self, client: GhClient) -> None:
        result = await client.run(
            "repo",
            "view",
            "cli/cli",
            "--json",
            "nameWithOwner,name,description",
        )
        assert result["nameWithOwner"] == "cli/cli"


class TestRepositoryTree:
    """Live exact-state structural discovery and same-commit file-read replay."""

    async def test_exact_commit_tree_to_file_replay(self, context: Any) -> None:
        repo = await gh_get_repo("cli", "cli", ctx=context)
        assert repo.default_branch is not None

        ref = await gh_get_ref(
            "cli",
            "cli",
            f"heads/{repo.default_branch}",
            ctx=context,
        )
        assert ref.found is True
        assert ref.object_type == "commit"
        assert ref.object_sha is not None
        commit_sha = ref.object_sha

        root = await gh_list_repository_tree(
            "cli",
            "cli",
            commit_sha,
            max_entries=100,
            ctx=context,
        )
        assert root.commit_sha == commit_sha
        assert root.path == ""
        assert root.entries_returned > 1
        directory = next((entry for entry in root.entries if entry.type == "tree"), None)
        blob = next((entry for entry in root.entries if entry.type == "blob"), None)
        assert directory is not None
        assert blob is not None

        nested = await gh_list_repository_tree(
            "cli",
            "cli",
            commit_sha,
            path=directory.path,
            max_entries=100,
            ctx=context,
        )
        assert nested.directory_tree_sha == directory.sha
        assert all(entry.path.startswith(f"{directory.path}/") for entry in nested.entries)

        file_result = await gh_get_file_contents(
            "cli",
            "cli",
            blob.path,
            commit_sha,
            ctx=context,
        )
        assert file_result.ref == commit_sha
        assert file_result.sha == blob.sha

        bounded = await gh_list_repository_tree(
            "cli",
            "cli",
            commit_sha,
            recursive=True,
            max_entries=1,
            ctx=context,
        )
        assert bounded.entries_returned == 1
        assert bounded.truncated is True
        assert bounded.evidence_complete is False
        assert bounded.warning is not None

        with pytest.raises(ValueError, match="directory component not found"):
            await gh_list_repository_tree(
                "cli",
                "cli",
                commit_sha,
                path="__gh_mcp_issue_82_missing__",
                ctx=context,
            )

        with pytest.raises(ValueError, match="not a directory"):
            await gh_list_repository_tree(
                "cli",
                "cli",
                commit_sha,
                path=blob.path,
                ctx=context,
            )


class TestListReleases:
    """List releases test."""

    async def test_list_releases(self, client: GhClient) -> None:
        result = await client.run(
            "release",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "tagName,name",
            "--limit",
            "5",
        )
        releases = result if isinstance(result, list) else []
        assert len(releases) > 0
