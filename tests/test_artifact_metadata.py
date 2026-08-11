"""Regression coverage for immutable GitHub Actions artifact metadata reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext, gh_get_artifact, gh_list_run_artifacts
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record exact GitHub reads and return queued values."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _context(client: FakeGhClient, settings: Settings | None = None) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=settings or Settings(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _run(
    run_id: int = 123,
    *,
    attempt: int = 2,
    head_sha: str = "a" * 40,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "run_attempt": attempt,
        "head_sha": head_sha,
    }


def _artifact(
    artifact_id: int = 77,
    *,
    name: str = "coverage",
    run_id: int = 123,
    head_sha: str = "a" * 40,
    expired: bool = False,
    digest: str | None = "sha256:" + "d" * 64,
) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": 4096,
        "digest": digest,
        "expired": expired,
        "created_at": "2026-08-11T10:00:00Z",
        "expires_at": "2026-11-09T10:00:00Z",
        "workflow_run": {
            "id": run_id,
            "head_sha": head_sha,
        },
    }


def _payload(total_count: int, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"total_count": total_count, "artifacts": artifacts}


async def test_list_artifacts_uses_exact_name_and_reports_run_page_identity() -> None:
    artifact = _artifact(3)
    client = FakeGhClient([_run(), _payload(3, [artifact]), _run()])

    result = await gh_list_run_artifacts(
        "octo",
        "repo",
        123,
        ctx=_context(client),
        page=2,
        per_page=2,
        name="coverage",
    )

    assert client.calls == [
        (
            (
                "api",
                "repos/octo/repo/actions/runs/123",
                "-X",
                "GET",
            ),
            {},
        ),
        (
            (
                "api",
                "repos/octo/repo/actions/runs/123/artifacts",
                "-X",
                "GET",
                "-f",
                "page=2",
                "-f",
                "per_page=2",
                "-f",
                "name=coverage",
            ),
            {},
        ),
        (
            (
                "api",
                "repos/octo/repo/actions/runs/123",
                "-X",
                "GET",
            ),
            {},
        ),
    ]
    assert result.run_id == 123
    assert result.attempt == 2
    assert result.head_sha == "a" * 40
    assert result.total_count == 3
    assert result.page == 2
    assert result.per_page == 2
    assert result.has_more is False
    assert result.truncated is False
    assert result.warning is None
    assert result.artifacts[0].id == 3
    assert result.artifacts[0].name == "coverage"
    assert result.artifacts[0].workflow_run_id == 123
    assert result.artifacts[0].workflow_head_sha == "a" * 40


async def test_list_artifacts_server_cap_reports_additional_evidence() -> None:
    settings = Settings(default_max_results=2, hard_max_results=2)
    client = FakeGhClient(
        [
            _run(),
            _payload(3, [_artifact(1), _artifact(2)]),
            _run(),
        ]
    )

    result = await gh_list_run_artifacts(
        "octo",
        "repo",
        123,
        ctx=_context(client, settings),
        per_page=5,
    )

    assert "per_page=2" in client.calls[1][0]
    assert result.per_page == 2
    assert result.has_more is True
    assert result.truncated is True
    assert result.warning is not None
    assert "capped at the server hard limit of 2" in result.warning


async def test_exact_name_response_mismatch_fails_closed() -> None:
    client = FakeGhClient(
        [
            _run(),
            _payload(1, [_artifact(name="coverage-extra")]),
        ]
    )

    with pytest.raises(RuntimeError, match="Artifact name mismatch"):
        await gh_list_run_artifacts(
            "octo",
            "repo",
            123,
            ctx=_context(client),
            name="coverage",
        )

    assert "name=coverage" in client.calls[1][0]
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("expired", "digest"),
    [
        (True, None),
        (False, "sha256:" + "e" * 64),
    ],
)
async def test_get_artifact_preserves_expiry_and_optional_digest(
    expired: bool,
    digest: str | None,
) -> None:
    client = FakeGhClient([_artifact(expired=expired, digest=digest)])

    result = await gh_get_artifact("octo", "repo", 77, ctx=_context(client))

    assert client.calls == [
        (
            (
                "api",
                "repos/octo/repo/actions/artifacts/77",
                "-X",
                "GET",
            ),
            {},
        )
    ]
    assert result.id == 77
    assert result.expired is expired
    assert result.digest == digest
    assert result.size_in_bytes == 4096
    assert result.created_at == "2026-08-11T10:00:00Z"
    assert result.expires_at == "2026-11-09T10:00:00Z"
    assert result.workflow_run_id == 123
    assert result.workflow_head_sha == "a" * 40


async def test_get_artifact_rejects_artifact_identity_mismatch() -> None:
    client = FakeGhClient([_artifact(78)])

    with pytest.raises(RuntimeError, match="Artifact identity mismatch"):
        await gh_get_artifact("octo", "repo", 77, ctx=_context(client))

    assert len(client.calls) == 1


async def test_get_artifact_rejects_invalid_workflow_head_identity() -> None:
    client = FakeGhClient([_artifact(head_sha="not-a-sha")])

    with pytest.raises(RuntimeError, match="invalid workflow head SHA"):
        await gh_get_artifact("octo", "repo", 77, ctx=_context(client))

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (_artifact(run_id=124), "Artifact/run identity mismatch"),
        (_artifact(head_sha="b" * 40), "Artifact/head identity mismatch"),
    ],
)
async def test_list_artifacts_rejects_run_or_head_identity_mismatch(
    artifact: dict[str, Any],
    message: str,
) -> None:
    client = FakeGhClient([_run(), _payload(1, [artifact])])

    with pytest.raises(RuntimeError, match=message):
        await gh_list_run_artifacts("octo", "repo", 123, ctx=_context(client))

    assert len(client.calls) == 2


async def test_list_artifacts_detects_run_identity_change_during_read() -> None:
    client = FakeGhClient(
        [
            _run(attempt=1),
            _payload(0, []),
            _run(attempt=2),
        ]
    )

    with pytest.raises(RuntimeError, match="changed during the artifact metadata read"):
        await gh_list_run_artifacts("octo", "repo", 123, ctx=_context(client))

    assert len(client.calls) == 3


@pytest.mark.parametrize(
    "failure",
    [
        GitHubRequestError("artifact missing", status_code=404),
        GitHubRequestError("artifact forbidden", status_code=403),
    ],
)
async def test_get_artifact_preserves_governed_artifact_read_failures(
    failure: GitHubRequestError,
) -> None:
    client = FakeGhClient([failure])

    with pytest.raises(GitHubRequestError, match=str(failure)):
        await gh_get_artifact("octo", "repo", 77, ctx=_context(client))

    assert len(client.calls) == 1


async def test_list_artifacts_preserves_missing_run_failure_before_listing() -> None:
    client = FakeGhClient([GitHubRequestError("run missing", status_code=404)])

    with pytest.raises(GitHubRequestError, match="run missing"):
        await gh_list_run_artifacts("octo", "repo", 123, ctx=_context(client))

    assert len(client.calls) == 1
