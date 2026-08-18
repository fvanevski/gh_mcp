"""Regression coverage for canonical write transport metadata semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.write_contracts import run_api_json_write_with_metadata


@dataclass
class FakeGhClient:
    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


async def test_canonical_json_write_preserves_structured_ambiguity() -> None:
    error = GitHubRequestError(
        "connection reset",
        ambiguous=True,
        retryable=True,
        metadata=GitHubRequestMetadata(request_id="req-structured"),
    )
    client = FakeGhClient([error])

    with pytest.raises(GitHubRequestError) as raised:
        await run_api_json_write_with_metadata(
            client,  # type: ignore[arg-type]
            "POST",
            "repos/octo/repo/issues",
            {"title": "x"},
        )

    assert raised.value is error
    assert raised.value.ambiguous is True
    assert raised.value.metadata.request_id == "req-structured"
    assert len(client.calls) == 1


async def test_canonical_json_write_does_not_infer_bare_runtimeerror_text() -> None:
    error = RuntimeError("transport timeout after request body was sent")
    client = FakeGhClient([error])

    with pytest.raises(RuntimeError) as raised:
        await run_api_json_write_with_metadata(
            client,  # type: ignore[arg-type]
            "POST",
            "repos/octo/repo/issues",
            {"title": "x"},
        )

    assert raised.value is error
    assert not isinstance(raised.value, GitHubRequestError)
    assert len(client.calls) == 1


def test_release_documentation_describes_current_090_inventory() -> None:
    readme = Path("README.md").read_text()
    contract = Path("docs/write-schema-contract.md").read_text()

    assert "0.9.0" in readme
    assert "61 public MCP tools" in readme
    assert "41 read-only" in readme
    assert "20 write" in readme
    assert "0.9.0" in contract
    assert "61" in contract
    assert "20" in contract
