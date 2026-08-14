"""Regression coverage for issue #60 canonical Git/content write migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.models import CommitFile
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.tooling import AppContext
from mcp_gh_server.tools.content_writes import gh_commit_files
from mcp_gh_server.tools.issue_branch_writes import gh_create_branch


@dataclass
class MetadataAwareClient:
    """Queue authoritative reads separately from governed mutation attempts."""

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


def _context(client: MetadataAwareClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=True, allow_content_commits=True),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _linked_branch_page(
    issue_id: str,
    name: str,
    sha: str,
    *,
    linked_repository_id: str = "R_repo",
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "id": "R_repo",
                "issue": {
                    "id": issue_id,
                    "linkedBranches": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "LB_feature",
                                "ref": {
                                    "name": name,
                                    "prefix": "refs/heads/",
                                    "repository": {"id": linked_repository_id},
                                    "target": {"oid": sha},
                                },
                            }
                        ],
                    },
                },
            }
        }
    }


async def test_issue_branch_uses_exact_base_and_authoritative_link_readback() -> None:
    sha = "a" * 40
    client = MetadataAwareClient(
        read_results=[
            {"node_id": "R_repo", "default_branch": "main"},
            {"node_id": "I_issue"},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            _linked_branch_page("I_issue", "feature", sha),
        ],
        write_results=[
            GitHubRequestResult(
                value={
                    "data": {
                        "createLinkedBranch": {
                            "issue": {"id": "I_issue"},
                            "linkedBranch": {"id": "LB_feature"},
                        }
                    }
                },
                metadata=GitHubRequestMetadata(request_id="req-linked-branch"),
            )
        ],
    )

    result = await gh_create_branch("octo", "repo", 60, "feature", ctx=_context(client))

    assert result.created is True
    assert result.ref == "refs/heads/feature"
    assert result.base_sha == sha
    assert result.linked_branch_id == "LB_feature"
    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-linked-branch"
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1


async def test_issue_branch_ambiguous_write_is_not_replayed_and_readback_remains_authoritative() -> (
    None
):
    sha = "a" * 40
    client = MetadataAwareClient(
        read_results=[
            {"node_id": "R_repo", "default_branch": "main"},
            {"node_id": "I_issue"},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            _linked_branch_page("I_issue", "feature", sha),
        ],
        write_results=[
            GitHubRequestError(
                "transport reset",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-linked-ambiguous"),
            )
        ],
    )

    result = await gh_create_branch("octo", "repo", 60, "feature", ctx=_context(client))

    assert result.created is False
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-linked-ambiguous"
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "Do not retry the mutation" in result.warning
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1


async def test_issue_branch_ambiguous_wrong_repository_readback_is_not_verified() -> None:
    sha = "a" * 40
    client = MetadataAwareClient(
        read_results=[
            {"node_id": "R_repo", "default_branch": "main"},
            {"node_id": "I_issue"},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            _linked_branch_page(
                "I_issue",
                "feature",
                sha,
                linked_repository_id="R_other",
            ),
        ],
        write_results=[
            GitHubRequestError(
                "transport reset",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-linked-wrong-repo"),
            )
        ],
    )

    result = await gh_create_branch("octo", "repo", 60, "feature", ctx=_context(client))

    assert result.created is False
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.linked_branch_id is None
    assert result.request_id == "req-linked-wrong-repo"
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "does not match the requested state" in result.warning
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1


async def test_content_commit_stale_head_fails_before_any_mutation() -> None:
    expected = "a" * 40
    actual = "b" * 40
    client = MetadataAwareClient(read_results=[{"object": {"sha": actual}}])

    with pytest.raises(RuntimeError, match="head mismatch"):
        await gh_commit_files(
            "octo",
            "repo",
            "main",
            expected,
            [CommitFile(path="a.txt", content="replacement\n")],
            "update a.txt",
            ctx=_context(client),
        )

    assert all(kind == "read" for kind, _, _ in client.calls)
    assert client.write_results == []


async def test_content_commit_ambiguous_cas_preserves_unknown_write_outcome() -> None:
    head = "a" * 40
    base_tree = "b" * 40
    blob_sha = "c" * 40
    tree_sha = "d" * 40
    commit_sha = "e" * 40
    client = MetadataAwareClient(
        read_results=[
            {"object": {"sha": head}},
            {"node_id": "R_repo"},
            {"tree": {"sha": base_tree}},
            {"ref": "refs/heads/main", "object": {"sha": commit_sha}},
        ],
        write_results=[
            {"sha": blob_sha},
            {"sha": tree_sha},
            {"sha": commit_sha, "html_url": f"https://github.com/octo/repo/commit/{commit_sha}"},
            GitHubRequestError(
                "transport reset",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="req-cas-ambiguous"),
            ),
        ],
    )

    result = await gh_commit_files(
        "octo",
        "repo",
        "main",
        head,
        [CommitFile(path="a.txt", content="replacement\n")],
        "update a.txt",
        ctx=_context(client),
    )

    assert result.precondition_checked is True
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.ref_updated is True
    assert result.files_committed == 1
    assert result.request_id == "req-cas-ambiguous"
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert sum(kind == "write" for kind, _, _ in client.calls) == 4


async def test_issue_branch_graphql_error_is_ambiguous_until_readback() -> None:
    sha = "a" * 40
    client = MetadataAwareClient(
        read_results=[
            {"node_id": "R_repo", "default_branch": "main"},
            {"node_id": "I_issue"},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            {"ref": "refs/heads/main", "object": {"sha": sha}},
            _linked_branch_page("I_issue", "feature", sha),
        ],
        write_results=[
            GitHubRequestResult(
                value={"errors": [{"message": "resolver failed"}]},
                metadata=GitHubRequestMetadata(request_id="req-linked-graphql-error"),
            )
        ],
    )

    result = await gh_create_branch("octo", "repo", 60, "feature", ctx=_context(client))

    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-linked-graphql-error"
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1


async def test_content_commit_graphql_error_is_ambiguous_until_ref_readback() -> None:
    head = "a" * 40
    base_tree = "b" * 40
    blob_sha = "c" * 40
    tree_sha = "d" * 40
    commit_sha = "e" * 40
    client = MetadataAwareClient(
        read_results=[
            {"object": {"sha": head}},
            {"node_id": "R_repo"},
            {"tree": {"sha": base_tree}},
            {"ref": "refs/heads/main", "object": {"sha": head}},
        ],
        write_results=[
            {"sha": blob_sha},
            {"sha": tree_sha},
            {"sha": commit_sha},
            GitHubRequestResult(
                value={"errors": [{"message": "updateRefs resolver failed"}]},
                metadata=GitHubRequestMetadata(request_id="req-cas-graphql-error"),
            ),
        ],
    )

    result = await gh_commit_files(
        "octo",
        "repo",
        "main",
        head,
        [CommitFile(path="a.txt", content="replacement\n")],
        "update a.txt",
        ctx=_context(client),
    )

    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.ref_updated is False
    assert result.files_committed == 0
    assert result.request_id == "req-cas-graphql-error"
    assert result.warning is not None
    assert "Do not retry" in result.warning
    assert sum(kind == "write" for kind, _, _ in client.calls) == 4
