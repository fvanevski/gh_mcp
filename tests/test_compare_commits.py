"""Regression tests for exact bounded commit-comparison evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext, gh_compare_commits
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record exact comparison reads and return queued values."""

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


def _comparison_commit(sha: str, message: str = "Compared commit") -> dict[str, Any]:
    return {
        "sha": sha,
        "html_url": f"https://github.com/octo/repo/commit/{sha}",
        "commit": {
            "message": message,
            "author": {
                "name": "A. Author",
                "email": "author@example.com",
                "date": "2026-08-12T10:00:00Z",
            },
            "committer": {
                "name": "C. Committer",
                "email": "committer@example.com",
                "date": "2026-08-12T10:01:00Z",
            },
        },
        "author": {"login": "author"},
        "committer": {"login": "committer"},
    }


def _comparison_file(index: int) -> dict[str, Any]:
    sha = f"{index + 1:040x}"[-40:]
    return {
        "sha": sha,
        "filename": f"src/file_{index}.py",
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "changes": 3,
        "previous_filename": None,
        "blob_url": f"https://github.com/octo/repo/blob/{sha}/src/file_{index}.py",
        "raw_url": f"https://github.com/octo/repo/raw/{sha}/src/file_{index}.py",
        "contents_url": f"https://api.github.com/repos/octo/repo/contents/src/file_{index}.py",
    }


def _compare_payload(
    base_sha: str,
    merge_base_sha: str,
    *,
    status: str = "ahead",
    ahead_by: int = 1,
    behind_by: int = 0,
    total_commits: int = 1,
    commits: list[dict[str, Any]] | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_commits = commits
    if resolved_commits is None:
        resolved_commits = [_comparison_commit("c" * 40)] if total_commits else []
    return {
        "status": status,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "total_commits": total_commits,
        "base_commit": {"sha": base_sha},
        "merge_base_commit": {"sha": merge_base_sha},
        "commits": resolved_commits,
        "files": files or [],
    }


def _exact_commit_payload(sha: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "tree": {"sha": "d" * 40},
        "parents": [],
        "author": {
            "name": "A. Author",
            "email": "author@example.com",
            "date": "2026-08-12T10:00:00Z",
        },
        "committer": {
            "name": "C. Committer",
            "email": "committer@example.com",
            "date": "2026-08-12T10:01:00Z",
        },
        "message": "Exact commit",
        "verification": {
            "verified": False,
            "reason": "unsigned",
            "signature": None,
            "payload": None,
            "verified_at": None,
        },
    }


@pytest.mark.parametrize(
    ("status", "ahead_by", "behind_by"),
    [
        ("identical", 0, 0),
        ("ahead", 3, 0),
        ("behind", 1, 3),
        ("diverged", 2, 4),
    ],
)
async def test_compare_preserves_all_supported_statuses_and_merge_base(
    status: str,
    ahead_by: int,
    behind_by: int,
) -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    merge_base_sha = "9" * 40
    payload = _compare_payload(
        base_sha,
        merge_base_sha,
        status=status,
        ahead_by=ahead_by,
        behind_by=behind_by,
    )
    client = FakeGhClient([payload])

    result = await gh_compare_commits(base_sha=base_sha, head_sha=head_sha, owner="octo", repo="repo", ctx=_context(client))

    assert result.base_sha == base_sha
    assert result.head_sha == head_sha
    assert result.base_found is True
    assert result.head_found is True
    assert result.comparison_available is True
    assert result.merge_base_sha == merge_base_sha
    assert result.status == status
    assert result.ahead_by == ahead_by
    assert result.behind_by == behind_by
    assert result.evidence_complete is True
    assert result.truncated is False
    assert len(result.sha256) == 64
    assert client.calls[0][0][:2] == (
        "api",
        f"repos/octo/repo/compare/{base_sha}...{head_sha}",
    )
    assert "per_page=30" in client.calls[0][0]
    assert "--jq" in client.calls[0][0]


@pytest.mark.parametrize("bad_sha", ["a" * 39, "a" * 41, "g" * 40, "main", "deadbeef"])
async def test_compare_rejects_non_exact_sha_before_github(bad_sha: str) -> None:
    client = FakeGhClient([])

    with pytest.raises(ValidationError):
        await gh_compare_commits(
            owner="octo",
            repo="repo",
            base_sha=bad_sha,
            head_sha="b" * 40,
            ctx=_context(client),
        )

    assert client.calls == []


async def test_commit_and_file_collections_are_independently_bounded() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    commits = [_comparison_commit(f"{index + 10:040x}"[-40:]) for index in range(3)]
    files = [_comparison_file(index) for index in range(4)]
    payload = _compare_payload(
        base_sha,
        base_sha,
        total_commits=3,
        commits=commits[:2],
        files=files,
    )
    client = FakeGhClient([payload])

    result = await gh_compare_commits(
        owner="octo",
        repo="repo",
        base_sha=base_sha,
        head_sha=head_sha,
        max_commits=2,
        max_files=3,
        ctx=_context(client),
    )

    assert len(result.commits) == 2
    assert result.commits_evidence.total_count == 3
    assert result.commits_evidence.truncated is True
    assert result.commits_evidence.complete is False
    assert len(result.files) == 3
    assert result.files_evidence.total_count == 4
    assert result.files_evidence.truncated is True
    assert result.files_evidence.complete is False
    assert result.truncated is True
    assert result.evidence_complete is False
    assert result.warning is not None
    assert len(result.commits_evidence.sha256) == 64
    assert len(result.files_evidence.sha256) == 64


async def test_file_saturation_cannot_be_reported_complete() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    files = [_comparison_file(index) for index in range(300)]
    payload = _compare_payload(base_sha, base_sha, files=files)
    settings = Settings(default_max_results=500, hard_max_results=500)
    client = FakeGhClient([payload])

    result = await gh_compare_commits(
        owner="octo",
        repo="repo",
        base_sha=base_sha,
        head_sha=head_sha,
        max_files=500,
        ctx=_context(client, settings),
    )

    assert len(result.files) == 299
    assert result.files_evidence.returned_count == 299
    assert result.files_evidence.total_count is None
    assert result.files_evidence.truncated is True
    assert result.files_evidence.complete is False
    assert result.files_evidence.warning is not None
    assert "300-file upstream limit" in result.files_evidence.warning


async def test_commit_message_byte_truncation_marks_commit_evidence_incomplete() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    payload = _compare_payload(
        base_sha,
        base_sha,
        commits=[_comparison_commit("c" * 40, message="abcdefghij")],
    )
    settings = Settings(max_pr_commit_message_bytes=5)
    client = FakeGhClient([payload])

    result = await gh_compare_commits(
        owner="octo",
        repo="repo",
        base_sha=base_sha,
        head_sha=head_sha,
        ctx=_context(client, settings),
    )

    assert result.commits[0].message == "abcde"
    assert result.commits[0].message_truncated is True
    assert result.commits[0].message_bytes_returned == 5
    assert len(result.commits[0].message_sha256) == 64
    assert result.commits_evidence.truncated is True
    assert result.commits_evidence.complete is False
    assert result.evidence_complete is False


async def test_digest_is_deterministic_for_identical_returned_evidence() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    payload = _compare_payload(base_sha, base_sha, files=[_comparison_file(0)])
    first_client = FakeGhClient([payload])
    second_client = FakeGhClient([payload])

    first = await gh_compare_commits(
        owner="octo",
        repo="repo",
        base_sha=base_sha,
        head_sha=head_sha,
        ctx=_context(first_client),
    )
    second = await gh_compare_commits(
        owner="octo",
        repo="repo",
        base_sha=base_sha,
        head_sha=head_sha,
        ctx=_context(second_client),
    )

    assert first.sha256 == second.sha256
    assert first.commits_evidence.sha256 == second.commits_evidence.sha256
    assert first.files_evidence.sha256 == second.files_evidence.sha256


async def test_compare_404_reports_missing_base_only_after_exact_commit_classification() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    compare_missing = GitHubRequestError("comparison not found", status_code=404)
    base_missing = GitHubRequestError("commit not found", status_code=404)
    client = FakeGhClient(
        [
            compare_missing,
            base_missing,
            [{"name": "main"}],
            _exact_commit_payload(head_sha),
        ]
    )

    result = await gh_compare_commits(
        owner="octo",
        repo="repo",
        base_sha=base_sha,
        head_sha=head_sha,
        ctx=_context(client),
    )

    assert result.base_found is False
    assert result.head_found is True
    assert result.comparison_available is False
    assert result.status is None
    assert result.merge_base_sha is None
    assert result.commits == []
    assert result.files == []
    assert result.truncated is False
    assert result.evidence_complete is False
    assert result.warning is not None
    assert "base commit SHA is missing" in result.warning
    assert client.calls[1][0][:2] == (
        "api",
        f"repos/octo/repo/git/commits/{base_sha}",
    )
    assert client.calls[2][0] == (
        "api",
        "repos/octo/repo/branches",
        "-X",
        "GET",
        "-f",
        "per_page=1",
    )
    assert client.calls[3][0][:2] == (
        "api",
        f"repos/octo/repo/git/commits/{head_sha}",
    )


async def test_compare_404_is_not_reclassified_when_both_exact_commits_exist() -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    compare_missing = GitHubRequestError("comparison unavailable", status_code=404)
    client = FakeGhClient(
        [
            compare_missing,
            _exact_commit_payload(base_sha),
            _exact_commit_payload(head_sha),
        ]
    )

    with pytest.raises(GitHubRequestError) as raised:
        await gh_compare_commits(
            owner="octo",
            repo="repo",
            base_sha=base_sha,
            head_sha=head_sha,
            ctx=_context(client),
        )

    assert raised.value is compare_missing


@pytest.mark.parametrize(
    "error",
    [
        GitHubRequestError("unauthorized", status_code=401),
        GitHubRequestError("forbidden", status_code=403),
        GitHubRequestError("transport reset", retryable=True),
    ],
)
async def test_permission_and_transport_failures_are_not_missing(error: GitHubRequestError) -> None:
    client = FakeGhClient([error])

    with pytest.raises(GitHubRequestError) as raised:
        await gh_compare_commits(
            owner="octo",
            repo="repo",
            base_sha="a" * 40,
            head_sha="b" * 40,
            ctx=_context(client),
        )

    assert raised.value is error
    assert len(client.calls) == 1


async def test_comparison_rejects_mismatched_returned_base_identity() -> None:
    client = FakeGhClient([_compare_payload("c" * 40, "a" * 40)])

    with pytest.raises(RuntimeError, match="did not preserve the requested base commit SHA"):
        await gh_compare_commits(
            owner="octo",
            repo="repo",
            base_sha="a" * 40,
            head_sha="b" * 40,
            ctx=_context(client),
        )
