"""Regression tests for exact Git commit identity and verification evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext, gh_get_commit
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record exact commit reads and return queued values."""

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


def _person(name: str, email: str, date: str) -> dict[str, str]:
    return {"name": name, "email": email, "date": date}


def _commit_payload(
    sha: str,
    tree_sha: str,
    parents: list[str],
    *,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "sha": sha,
        "tree": {
            "sha": tree_sha,
            "url": f"https://api.github.com/repos/octo/repo/git/trees/{tree_sha}",
        },
        "parents": [
            {
                "sha": parent_sha,
                "url": f"https://api.github.com/repos/octo/repo/git/commits/{parent_sha}",
                "html_url": f"https://github.com/octo/repo/commit/{parent_sha}",
            }
            for parent_sha in parents
        ],
        "author": _person("A. Author", "author@example.com", "2026-08-11T10:00:00Z"),
        "committer": _person("C. Committer", "committer@example.com", "2026-08-11T10:01:00Z"),
        "message": "Exact commit evidence\n\nPreserve immutable identity.",
        "verification": verification
        or {
            "verified": False,
            "reason": "unsigned",
            "signature": None,
            "payload": None,
            "verified_at": None,
        },
    }


async def test_exact_commit_returns_tree_parent_people_message_and_verification() -> None:
    sha = "a" * 40
    tree_sha = "b" * 40
    parent_sha = "c" * 40
    client = FakeGhClient([_commit_payload(sha, tree_sha, [parent_sha])])

    result = await gh_get_commit("octo", "repo", "A" * 40, ctx=_context(client))

    assert result.commit_sha == sha
    assert result.found is True
    assert result.tree_sha == tree_sha
    assert result.parents == [parent_sha]
    assert result.author is not None
    assert result.author.name == "A. Author"
    assert result.author.email == "author@example.com"
    assert result.committer is not None
    assert result.committer.name == "C. Committer"
    assert result.message == "Exact commit evidence\n\nPreserve immutable identity."
    assert result.verification is not None
    assert result.verification.verified is False
    assert result.verification.reason == "unsigned"
    assert client.calls == [
        (
            ("api", f"repos/octo/repo/git/commits/{sha}", "-X", "GET"),
            {},
        )
    ]


async def test_merge_commit_preserves_all_parent_shas_in_api_order() -> None:
    sha = "d" * 40
    tree_sha = "e" * 40
    parents = ["1" * 40, "2" * 40]
    client = FakeGhClient([_commit_payload(sha, tree_sha, parents)])

    result = await gh_get_commit("octo", "repo", sha, ctx=_context(client))

    assert result.found is True
    assert result.parents == parents


async def test_verification_metadata_is_preserved_without_reinterpretation() -> None:
    sha = "f" * 40
    signature = "-----BEGIN PGP SIGNATURE-----\nsignature\n-----END PGP SIGNATURE-----"
    payload = "tree deadbeef\nauthor A. Author <author@example.com>\n"
    verification = {
        "verified": True,
        "reason": "valid",
        "signature": signature,
        "payload": payload,
        "verified_at": "2026-08-11T10:02:00Z",
    }
    client = FakeGhClient([_commit_payload(sha, "a" * 40, [], verification=verification)])

    result = await gh_get_commit("octo", "repo", sha, ctx=_context(client))

    assert result.verification is not None
    assert result.verification.model_dump() == verification


@pytest.mark.parametrize(
    "commit_sha",
    [
        "a" * 39,
        "a" * 41,
        "g" * 40,
        "main",
        "deadbeef",
    ],
)
async def test_non_exact_commit_sha_is_rejected_before_github(commit_sha: str) -> None:
    client = FakeGhClient([])

    with pytest.raises(ValidationError):
        await gh_get_commit("octo", "repo", commit_sha, ctx=_context(client))

    assert client.calls == []


async def test_missing_commit_requires_successful_contents_permission_probe() -> None:
    sha = "a" * 40
    missing = GitHubRequestError("not found", status_code=404)
    client = FakeGhClient([missing, [{"name": "main"}]])

    result = await gh_get_commit("octo", "repo", sha, ctx=_context(client))

    assert result.commit_sha == sha
    assert result.found is False
    assert result.tree_sha is None
    assert result.parents == []
    assert result.author is None
    assert result.committer is None
    assert result.message is None
    assert result.verification is None
    assert client.calls[1][0] == (
        "api",
        "repos/octo/repo/branches",
        "-X",
        "GET",
        "-f",
        "per_page=1",
    )


async def test_empty_repository_409_is_missing_only_after_empty_probe() -> None:
    sha = "a" * 40
    conflict = GitHubRequestError("empty or unavailable", status_code=409)
    client = FakeGhClient([conflict, {"isEmpty": True}])

    result = await gh_get_commit("octo", "repo", sha, ctx=_context(client))

    assert result.found is False
    assert client.calls[1][0] == (
        "repo",
        "view",
        "octo/repo",
        "--json",
        "isEmpty",
    )


async def test_nonempty_repository_409_preserves_original_conflict() -> None:
    conflict = GitHubRequestError("repository unavailable", status_code=409)
    client = FakeGhClient([conflict, {"isEmpty": False}])

    with pytest.raises(GitHubRequestError) as raised:
        await gh_get_commit("octo", "repo", "a" * 40, ctx=_context(client))

    assert raised.value is conflict
    assert len(client.calls) == 2


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
        await gh_get_commit("octo", "repo", "a" * 40, ctx=_context(client))

    assert raised.value is error
    assert len(client.calls) == 1


async def test_commit_404_with_failed_contents_probe_is_not_missing() -> None:
    missing_like = GitHubRequestError("not found", status_code=404)
    forbidden = GitHubRequestError("contents forbidden", status_code=403)
    client = FakeGhClient([missing_like, forbidden])

    with pytest.raises(GitHubRequestError) as raised:
        await gh_get_commit("octo", "repo", "a" * 40, ctx=_context(client))

    assert raised.value is forbidden
    assert len(client.calls) == 2


async def test_exact_lookup_rejects_mismatched_returned_commit_sha() -> None:
    requested_sha = "a" * 40
    client = FakeGhClient([_commit_payload("b" * 40, "c" * 40, [])])

    with pytest.raises(RuntimeError, match="did not preserve the requested commit SHA"):
        await gh_get_commit("octo", "repo", requested_sha, ctx=_context(client))

    assert len(client.calls) == 1


async def test_exact_lookup_rejects_malformed_tree_or_parent_identity() -> None:
    sha = "a" * 40
    malformed_tree = _commit_payload(sha, "b" * 40, [])
    malformed_tree["tree"] = {"sha": "short"}
    tree_client = FakeGhClient([malformed_tree])

    with pytest.raises(RuntimeError, match="exact tree SHA"):
        await gh_get_commit("octo", "repo", sha, ctx=_context(tree_client))

    malformed_parent = _commit_payload(sha, "b" * 40, [])
    malformed_parent["parents"] = [{"sha": "short"}]
    parent_client = FakeGhClient([malformed_parent])

    with pytest.raises(RuntimeError, match="malformed parent SHA"):
        await gh_get_commit("octo", "repo", sha, ctx=_context(parent_client))
