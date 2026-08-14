"""Follow-up regressions for exact-head review findings on PR #28."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.request_governor import GitHubRequestMetadata, GitHubRequestResult
from mcp_gh_server.server import AppContext, gh_create_issue
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
        settings=Settings(
            allow_write_commands=True,
            allow_repo_creation=True,
            allow_release_creation=True,
            allow_workflow_dispatch=True,
            allow_content_commits=True,
            allow_pr_merge=True,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.mark.asyncio
async def test_successful_request_id_is_projected_without_governor_warning() -> None:
    url = "https://github.com/octo/repo/issues/43"
    client = MetadataAwareClient(
        read_results=[{"number": 43, "title": "Identity", "url": url}],
        write_results=[
            GitHubRequestResult(
                value={"stdout": url},
                metadata=GitHubRequestMetadata(request_id="req-success-only"),
            )
        ],
    )

    result = await gh_create_issue("octo", "repo", "Identity", ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.warning == "GitHub request id: req-success-only."

