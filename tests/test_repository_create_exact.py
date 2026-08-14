"""Regression tests for canonical exact-target repository creation."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_gh_server.write_tool_schema as write_tool_schema
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext, gh_create_repo
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools import repository_create as repository_create_tools


@dataclass
class RepositoryCreateClient:
    """Protocol fake that separates the one mutation attempt from exact readback."""

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


def _context(client: RepositoryCreateClient, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "allow_write_commands": True,
        "allow_repo_creation": True,
        "allowed_repo_creation_targets": "octo/new-repo",
    }
    values.update(overrides)
    settings = Settings(**values)
    app = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _repository(
    *,
    owner: str = "octo",
    repo: str = "new-repo",
    private: bool = False,
    description: str | None = None,
    initialized: bool = False,
) -> dict[str, Any]:
    return {
        "nameWithOwner": f"{owner}/{repo}",
        "url": f"https://github.com/{owner}/{repo}",
        "isPrivate": private,
        "description": description,
        "isEmpty": not initialized,
    }


def _write_calls(client: RepositoryCreateClient) -> list[tuple[str, ...]]:
    return [args for kind, args, _ in client.calls if kind == "write"]


def _read_calls(client: RepositoryCreateClient) -> list[tuple[str, ...]]:
    return [args for kind, args, _ in client.calls if kind == "read"]


def test_public_repository_facade_uses_canonical_implementation() -> None:
    assert write_tool_schema._gh_create_repo is repository_create_tools.gh_create_repo


async def test_disallowed_prospective_target_fails_before_any_github_call() -> None:
    client = RepositoryCreateClient()
    ctx = _context(client, allowed_repo_creation_targets="octo/allowed-repo")

    with pytest.raises(RuntimeError, match="ALLOWED_REPO_CREATION_TARGETS"):
        await gh_create_repo("octo", "new-repo", ctx=ctx)

    assert client.calls == []


@pytest.mark.parametrize(
    ("private", "description", "auto_init"),
    [
        (False, None, False),
        (True, "Private repository", True),
    ],
)
async def test_success_uses_one_create_and_exact_repository_readback(
    private: bool,
    description: str | None,
    auto_init: bool,
) -> None:
    snapshot = _repository(
        private=private,
        description=description,
        initialized=auto_init,
    )
    client = RepositoryCreateClient(
        read_results=[snapshot],
        write_results=[
            GitHubRequestResult(
                value={"stdout": ""},
                metadata=GitHubRequestMetadata(request_id="req-repo-create"),
            )
        ],
    )

    result = await gh_create_repo(
        "octo",
        "new-repo",
        ctx=_context(client),
        description=description,
        private=private,
        auto_init=auto_init,
    )

    assert result.precondition_checked is False
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-repo-create"
    assert result.warning is None
    assert result.owner == "octo"
    assert result.repo == "new-repo"
    assert result.name_with_owner == "octo/new-repo"
    assert result.url == "https://github.com/octo/new-repo"
    assert result.is_private is private
    assert result.description == description
    assert result.initialized is auto_init

    writes = _write_calls(client)
    assert len(writes) == 1
    expected_write = [
        "repo",
        "create",
        "octo/new-repo",
        "--private" if private else "--public",
    ]
    if description is not None:
        expected_write.extend(["--description", description])
    if auto_init:
        expected_write.append("--add-readme")
    assert writes == [tuple(expected_write)]
    assert _read_calls(client) == [
        (
            "repo",
            "view",
            "octo/new-repo",
            "--json",
            "nameWithOwner,url,isPrivate,description,isEmpty",
        )
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nameWithOwner", "octo/other"),
        ("isPrivate", True),
        ("description", "different"),
        ("isEmpty", False),
    ],
)
async def test_repository_readback_mismatch_is_not_reported_as_verified(
    field: str,
    value: Any,
) -> None:
    snapshot = _repository()
    snapshot[field] = value
    client = RepositoryCreateClient(read_results=[snapshot], write_results=[{"stdout": ""}])

    result = await gh_create_repo("octo", "new-repo", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.warning is not None
    assert "does not match the requested state" in result.warning
    assert len(_write_calls(client)) == 1
    assert len(_read_calls(client)) == 1


async def test_missing_initialization_evidence_does_not_invent_a_state() -> None:
    snapshot = _repository()
    snapshot.pop("isEmpty")
    client = RepositoryCreateClient(read_results=[snapshot], write_results=[{"stdout": ""}])

    result = await gh_create_repo("octo", "new-repo", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.initialized is None
    assert len(_write_calls(client)) == 1


async def test_successful_write_with_readback_failure_is_not_retried() -> None:
    client = RepositoryCreateClient(
        read_results=[RuntimeError("readback failed")],
        write_results=[
            GitHubRequestResult(
                value={"stdout": ""},
                metadata=GitHubRequestMetadata(request_id="req-readback-failed"),
            )
        ],
    )

    result = await gh_create_repo("octo", "new-repo", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-readback-failed"
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning
    assert len(_write_calls(client)) == 1
    assert len(_read_calls(client)) == 1


async def test_known_write_failure_with_failed_readback_is_false_and_not_retried() -> None:
    failure = GitHubRequestError(
        "validation failed",
        status_code=422,
        ambiguous=False,
        metadata=GitHubRequestMetadata(request_id="req-known-failure"),
    )
    client = RepositoryCreateClient(
        read_results=[RuntimeError("repository not found")],
        write_results=[failure],
    )

    result = await gh_create_repo("octo", "new-repo", ctx=_context(client))

    assert result.write_completed is False
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-known-failure"
    assert result.warning is not None
    assert len(_write_calls(client)) == 1
    assert len(_read_calls(client)) == 1


async def test_ambiguous_transport_readback_can_verify_state_without_replaying_write() -> None:
    failure = GitHubRequestError(
        "connection lost after request send",
        retryable=True,
        ambiguous=True,
        metadata=GitHubRequestMetadata(request_id="req-ambiguous"),
    )
    client = RepositoryCreateClient(
        read_results=[_repository(initialized=True)],
        write_results=[failure],
    )

    result = await gh_create_repo(
        "octo",
        "new-repo",
        ctx=_context(client),
        auto_init=True,
    )

    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-ambiguous"
    assert result.initialized is True
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning
    assert len(_write_calls(client)) == 1
    assert len(_read_calls(client)) == 1
