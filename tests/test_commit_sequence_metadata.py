"""Regression for request identity across atomic content-commit setup writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.models import CommitFile
from mcp_gh_server.request_governor import GitHubRequestMetadata, GitHubRequestResult
from mcp_gh_server.server import AppContext, gh_commit_files
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
            allow_content_commits=True,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _write_result(value: Any, request_id: str) -> GitHubRequestResult[Any]:
    return GitHubRequestResult(
        value=value,
        metadata=GitHubRequestMetadata(request_id=request_id),
    )


@pytest.mark.asyncio
async def test_commit_files_preserves_all_warning_free_setup_request_ids() -> None:
    head = "a" * 40
    base_tree = "b" * 40
    blob_sha = "c" * 40
    tree_sha = "d" * 40
    commit_sha = "e" * 40
    commit_url = f"https://github.com/octo/repo/commit/{commit_sha}"
    client = MetadataAwareClient(
        read_results=[
            {"object": {"sha": head}},
            {"node_id": "repo-node"},
            {"tree": {"sha": base_tree}},
            {"object": {"sha": commit_sha}},
        ],
        write_results=[
            _write_result({"sha": blob_sha}, "req-blob"),
            _write_result({"sha": tree_sha}, "req-tree"),
            _write_result(
                {"sha": commit_sha, "html_url": commit_url},
                "req-commit",
            ),
            _write_result(
                {"data": {"updateRefs": {"clientMutationId": None}}},
                "req-ref",
            ),
        ],
    )

    result = await gh_commit_files(
        "octo",
        "repo",
        "main",
        head,
        [CommitFile(path="a.txt", content="new content")],
        "update a.txt",
        ctx=_context(client),
    )

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.ref_updated is True
    assert result.warning is not None
    for request_id in ("req-blob", "req-tree", "req-commit", "req-ref"):
        assert f"GitHub request id: {request_id}." in result.warning
    assert sum(kind == "write" for kind, _, _ in client.calls) == 4
