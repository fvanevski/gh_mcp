"""Regression coverage for exact-target guarded release creation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.release_exact_models import ReleaseExactResult
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.release_exact import gh_create_release_exact
from mcp_gh_server.write_contracts import WritePreconditionMismatch


@dataclass
class ReleaseExactClient:
    """Protocol fake with independent read and governed-write queues."""

    read_results: list[Any] = field(default_factory=list)
    write_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        if "--input" in args:
            payload_path = Path(args[args.index("--input") + 1])
            self.payloads.append(json.loads(payload_path.read_text()))
        result = self.write_results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(
    client: ReleaseExactClient,
    *,
    writes_enabled: bool = True,
    release_creation_enabled: bool = True,
) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            allow_write_commands=writes_enabled,
            allow_release_creation=release_creation_enabled,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _sha(value: int) -> str:
    return f"{value:040x}"


def _commit(sha: str, *, returned_sha: str | None = None) -> dict[str, Any]:
    actual = returned_sha or sha
    return {
        "sha": actual,
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


def _tag_ref(sha: str, *, tag: str = "v1.0.0") -> dict[str, Any]:
    return {
        "ref": f"refs/tags/{tag}",
        "object": {
            "type": "commit",
            "sha": sha,
            "url": f"https://api.github.com/repos/octo/repo/git/commits/{sha}",
        },
    }


def _release(
    release_id: int,
    *,
    tag: str = "v1.0.0",
    name: str | None = "v1.0.0",
    body: str | None = "Release notes",
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, Any]:
    return {
        "id": release_id,
        "tag_name": tag,
        "html_url": f"https://github.com/octo/repo/releases/tag/{tag}",
        "name": name,
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
    }


def _missing_ref() -> list[Any]:
    return [GitHubRequestError("missing ref", status_code=404), []]


def _missing_release() -> list[Any]:
    return [GitHubRequestError("missing release", status_code=404), []]


def _successful_reads(
    sha: str,
    *,
    release_id: int = 17,
    prerelease: bool = True,
    latest_release_id: int = 99,
    tag_sha: str | None = None,
) -> list[Any]:
    return [
        _commit(sha),
        *_missing_ref(),
        *_missing_release(),
        _commit(sha),
        _release(release_id, prerelease=prerelease),
        _tag_ref(tag_sha or sha),
        _release(latest_release_id, tag="v0.9.0", name="v0.9.0", body="Previous"),
    ]


def test_result_model_exposes_standard_exact_write_contract() -> None:
    schema = ReleaseExactResult.model_json_schema()

    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "tag_name",
        "expected_target_sha",
        "resolved_target_sha",
        "release_id",
        "release_url",
        "tag_commit_sha",
        "release_name",
        "is_draft",
        "is_prerelease",
        "make_latest",
        "is_latest",
    } == set(schema["properties"])


async def test_target_identity_mismatch_fails_closed_without_write() -> None:
    expected = _sha(1)
    client = ReleaseExactClient(read_results=[_commit(expected, returned_sha=_sha(2))])

    with pytest.raises(RuntimeError, match="did not preserve the requested commit SHA"):
        await gh_create_release_exact(
            "octo",
            "repo",
            "v1.0.0",
            expected,
            False,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read"]
    assert client.payloads == []


async def test_missing_target_fails_precondition_without_write() -> None:
    expected = _sha(3)
    client = ReleaseExactClient(
        read_results=[GitHubRequestError("missing commit", status_code=404), []]
    )

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_create_release_exact(
            "octo",
            "repo",
            "v1.0.0",
            expected,
            False,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read", "read"]
    assert client.payloads == []


async def test_existing_tag_fails_closed_when_absence_is_required() -> None:
    expected = _sha(4)
    client = ReleaseExactClient(read_results=[_commit(expected), _tag_ref(expected)])

    with pytest.raises(WritePreconditionMismatch, match="tag refs/tags/v1.0.0 absence"):
        await gh_create_release_exact(
            "octo",
            "repo",
            "v1.0.0",
            expected,
            False,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read", "read"]
    assert client.payloads == []


async def test_existing_release_fails_closed_when_absence_is_required() -> None:
    expected = _sha(5)
    client = ReleaseExactClient(
        read_results=[
            _commit(expected),
            *_missing_ref(),
            _release(17),
        ]
    )

    with pytest.raises(WritePreconditionMismatch, match="release 'v1.0.0' absence"):
        await gh_create_release_exact(
            "octo",
            "repo",
            "v1.0.0",
            expected,
            False,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read", "read", "read", "read"]
    assert client.payloads == []


async def test_release_404_is_not_absence_when_release_read_probe_is_forbidden() -> None:
    expected = _sha(6)
    client = ReleaseExactClient(
        read_results=[
            _commit(expected),
            *_missing_ref(),
            GitHubRequestError("release hidden", status_code=404),
            GitHubRequestError("forbidden", status_code=403),
        ]
    )

    with pytest.raises(GitHubRequestError) as error:
        await gh_create_release_exact(
            "octo",
            "repo",
            "v1.0.0",
            expected,
            False,
            ctx=_context(client),
        )

    assert error.value.status_code == 403
    assert [kind for kind, _, _ in client.calls] == ["read", "read", "read", "read", "read"]
    assert client.payloads == []


async def test_release_specific_write_gate_denies_before_any_github_request() -> None:
    client = ReleaseExactClient()

    with pytest.raises(RuntimeError, match="MCP_GH_ALLOW_RELEASE_CREATION"):
        await gh_create_release_exact(
            "octo",
            "repo",
            "v1.0.0",
            _sha(7),
            False,
            ctx=_context(client, release_creation_enabled=False),
        )

    assert client.calls == []


async def test_successful_prerelease_uses_exact_sha_and_explicit_non_latest_readback() -> None:
    expected = _sha(8)
    client = ReleaseExactClient(
        read_results=_successful_reads(expected),
        write_results=[
            GitHubRequestResult(
                value=_release(17, prerelease=True),
                metadata=GitHubRequestMetadata(request_id="REQ-17"),
            )
        ],
    )

    result = await gh_create_release_exact(
        "octo",
        "repo",
        "v1.0.0",
        expected.upper(),
        False,
        ctx=_context(client),
        name="v1.0.0",
        body="Release notes",
        prerelease=True,
    )

    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "REQ-17"
    assert result.expected_target_sha == expected
    assert result.resolved_target_sha == expected
    assert result.tag_commit_sha == expected
    assert result.release_id == 17
    assert result.is_prerelease is True
    assert result.make_latest is False
    assert result.is_latest is False
    assert [kind for kind, _, _ in client.calls].count("write") == 1
    assert client.payloads == [
        {
            "tag_name": "v1.0.0",
            "target_commitish": expected,
            "draft": False,
            "prerelease": True,
            "make_latest": "false",
            "name": "v1.0.0",
            "body": "Release notes",
        }
    ]


async def test_successful_write_with_wrong_tag_target_reports_semantic_mismatch_without_replay() -> None:
    expected = _sha(9)
    client = ReleaseExactClient(
        read_results=_successful_reads(expected, tag_sha=_sha(10)),
        write_results=[GitHubRequestResult(value=_release(17, prerelease=True))],
    )

    result = await gh_create_release_exact(
        "octo",
        "repo",
        "v1.0.0",
        expected,
        False,
        ctx=_context(client),
        prerelease=True,
    )

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.tag_commit_sha == _sha(10)
    assert result.warning is not None
    assert "resulting state does not match the requested state" in result.warning
    assert [kind for kind, _, _ in client.calls].count("write") == 1


async def test_ambiguous_write_is_never_replayed_and_matching_readback_remains_unknown() -> None:
    expected = _sha(11)
    client = ReleaseExactClient(
        read_results=_successful_reads(expected),
        write_results=[
            GitHubRequestError(
                "connection dropped",
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="REQ-AMB"),
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
        name="v1.0.0",
        body="Release notes",
        prerelease=True,
    )

    assert result.precondition_checked is True
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "REQ-AMB"
    assert result.warning is not None
    assert "transport outcome is unknown" in result.warning
    assert "Do not retry" in result.warning
    assert [kind for kind, _, _ in client.calls].count("write") == 1
    assert len(client.payloads) == 1


async def test_latest_true_is_rejected_for_prerelease_before_any_github_request() -> None:
    client = ReleaseExactClient()

    with pytest.raises(ValueError, match="cannot be marked as latest"):
        await gh_create_release_exact(
            "octo",
            "repo",
            "v1.0.0-rc1",
            _sha(12),
            True,
            ctx=_context(client),
            prerelease=True,
        )

    assert client.calls == []
