"""Regression coverage for ordinary-client isolation from reviewer credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from mcp_gh_server.request_governor import GitHubRequestResult
from mcp_gh_server.reviewer_auth import ReviewerPrincipal
from mcp_gh_server.server import AppContext, gh_create_pr
from mcp_gh_server.settings import Settings


@dataclass
class FakeOrdinaryClient:
    """Record ordinary GitHub calls without exposing reviewer credentials."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        result = await self.run(*args, **kwargs)
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


def _context(client: FakeOrdinaryClient) -> Any:
    settings = Settings(
        github_token="ordinary-token",
        reviewer_token="reviewer-token",
        allow_write_commands=True,
    )
    app = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.mark.asyncio
async def test_unrelated_pr_write_uses_only_ordinary_client_when_reviewer_is_configured() -> None:
    url = "https://github.com/octo/repo/pull/9"
    client = FakeOrdinaryClient(
        [
            {"stdout": url},
            {"number": 9, "title": "Ordinary write", "url": url},
        ]
    )
    context = _context(client)

    with patch.object(
        ReviewerPrincipal,
        "from_settings",
        side_effect=AssertionError("ordinary tools must not resolve reviewer credentials"),
    ):
        result = await gh_create_pr(
            "octo",
            "repo",
            "Ordinary write",
            "Body",
            "feature",
            "main",
            ctx=context,
        )

    assert result.number == 9
    assert len(client.calls) == 2
    assert client.calls[0][0][:2] == ("pr", "create")
    assert client.calls[1][0][:3] == ("pr", "view", url)

    settings = context.request_context.lifespan_context.settings
    assert settings.github_token is not None
    assert settings.github_token.get_secret_value() == "ordinary-token"
    assert settings.reviewer_token is not None
    assert settings.reviewer_token.get_secret_value() == "reviewer-token"
