"""Transport-metadata boundary regressions for governed JSON writes."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_gh_server.legacy_write_support import run_json_write_with_metadata
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
)
from mcp_gh_server.write_contracts import run_api_json_write_with_metadata


class MetadataFailureClient:
    """Minimal metadata-aware client that raises one configured write failure."""

    def __init__(self, error: RuntimeError) -> None:
        self.error = error
        self.calls = 0

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.error


class RunOnlyFailureClient:
    """Legacy run-only fake used to exercise compatibility classification."""

    def __init__(self, error: RuntimeError) -> None:
        self.error = error
        self.calls = 0

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.error


async def test_canonical_json_write_preserves_structured_ambiguity() -> None:
    failure = GitHubRequestError(
        "transport reset after send",
        retryable=True,
        ambiguous=True,
        metadata=GitHubRequestMetadata(request_id="req-structured"),
    )
    client = MetadataFailureClient(failure)

    with pytest.raises(GitHubRequestError) as caught:
        await run_api_json_write_with_metadata(
            client,  # type: ignore[arg-type]
            "POST",
            "repos/octo/repo/milestones",
            {"title": "v1"},
        )

    assert caught.value is failure
    assert caught.value.ambiguous is True
    assert caught.value.metadata.request_id == "req-structured"
    assert client.calls == 1


async def test_canonical_json_write_does_not_infer_ambiguity_from_runtime_text() -> None:
    failure = RuntimeError("timeout after synthetic send")
    client = MetadataFailureClient(failure)

    with pytest.raises(RuntimeError) as caught:
        await run_api_json_write_with_metadata(
            client,  # type: ignore[arg-type]
            "POST",
            "repos/octo/repo/milestones",
            {"title": "v1"},
        )

    assert type(caught.value) is RuntimeError
    assert caught.value is failure
    assert client.calls == 1


async def test_legacy_run_only_fake_keeps_transport_text_compatibility() -> None:
    client = RunOnlyFailureClient(RuntimeError("timeout after synthetic send"))

    with pytest.raises(GitHubRequestError) as caught:
        await run_json_write_with_metadata(
            client,  # type: ignore[arg-type]
            "POST",
            "repos/octo/repo/milestones",
            {"title": "v1"},
        )

    assert caught.value.ambiguous is True
    assert caught.value.retryable is True
    assert client.calls == 1
