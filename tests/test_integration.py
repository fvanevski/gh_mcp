"""GitHub MCP Server — comprehensive integration tests.

Requires a GITHUB_TOKEN with sufficient scopes for read operations.
Set GITHUB_TOKEN in the environment or .env file before running.

These tests hit a real GitHub repository — uses cli/cli as the test repo.
"""

from __future__ import annotations

import os

import pytest

from mcp_gh_server.gh_client import GhClient
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


class TestGhInfo:
    """Basic connectivity test: verify gh is authenticated."""

    def test_info(self, client: GhClient) -> None:
        result = client.run("auth", "status", "--json", "hosts")
        assert "hosts" in result


class TestSearchRepos:
    """Search test: find a well-known repository."""

    def test_search_repos(self, client: GhClient) -> None:
        result = client.run(
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

    def test_list_issues(self, client: GhClient) -> None:
        result = client.run(
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


class TestGetRepo:
    """Get repository details test."""

    def test_get_repo(self, client: GhClient) -> None:
        result = client.run(
            "repo",
            "view",
            "cli/cli",
            "--json",
            "nameWithOwner,name,description",
        )
        assert result["nameWithOwner"] == "cli/cli"


class TestListReleases:
    """List releases test."""

    def test_list_releases(self, client: GhClient) -> None:
        result = client.run(
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
