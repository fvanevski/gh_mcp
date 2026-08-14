"""Regression coverage for the canonical release-write surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server import server
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.release_exact import gh_create_release_exact


def _sha(value: int) -> str:
    return f"{value:040x}"


def _commit(sha: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "tree": {"sha": _sha(900)},
        "parents": [],
        "author": {
            "name": "Author",
            "email": "author@example.com",
            "date": "2026-08-12T15:00:00Z",
        },
        "committer": {
            "name": "Committer",
            "email": "committer@example.com",
            "date": "2026-08-12T15:00:00Z",
        },
        "message": "release target",
        "verification": {
            "verified": True,
            "reason": "valid",
            "signature": None,
            "payload": None,
            "verified_at": "2026-08-12T15:00:01Z",
        },
    }


def _permissions() -> dict[str, Any]:
    return {"permissions": {"admin": False, "push": True, "pull": True}}


@dataclass
class ReleaseFailureClient:
    """Minimal governed client for one known release-write failure."""

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


def _context(client: ReleaseFailureClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            allow_write_commands=True,
            allow_release_creation=True,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def test_generic_release_write_is_absent_from_server_public_namespace() -> None:
    assert not hasattr(server, "gh_create_release")
    assert hasattr(server, "gh_create_release_exact")


@pytest.mark.asyncio
async def test_known_release_write_failure_is_read_back_once_without_retry() -> None:
    expected = _sha(1)
    client = ReleaseFailureClient(
        read_results=[
            _commit(expected),
            GitHubRequestError("missing ref", status_code=404),
            [],
            _permissions(),
            [],
            _commit(expected),
            _permissions(),
            [],
        ],
        write_results=[
            GitHubRequestError(
                "validation failed",
                status_code=422,
                metadata=GitHubRequestMetadata(request_id="REQ-KNOWN-FAILURE"),
            )
        ],
    )

    result = await gh_create_release_exact(
        "octo",
        "repo",
        "v1.0.0",
        expected,
        False,
        ctx=_context(client),
    )

    assert result.precondition_checked is True
    assert result.write_completed is False
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "REQ-KNOWN-FAILURE"
    assert result.warning is not None
    assert len([call for call in client.calls if call[0] == "write"]) == 1
