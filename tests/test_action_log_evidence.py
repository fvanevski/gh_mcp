"""Regression coverage for bounded successful and failed Actions log evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext, gh_get_job_logs, gh_get_run_logs
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Return queued GitHub results while recording every governed command."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _context(client: FakeGhClient, *, hard_max: int = 500_000) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(max_action_log_bytes=hard_max),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _run(
    *,
    attempt: int = 2,
    head_sha: str = "a" * 40,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "headSha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "url": "https://github.com/octo/repo/actions/runs/123",
    }


def _job(
    *,
    job_id: int = 456,
    run_id: int = 123,
    head_sha: str = "a" * 40,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "run_id": run_id,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://github.com/octo/repo/actions/runs/123/job/456",
    }


def _job_run(
    *,
    attempt: int = 2,
    head_sha: str = "a" * 40,
    job_id: int = 456,
) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "headSha": head_sha,
        "jobs": [{"databaseId": job_id, "name": "tests"}],
    }


async def test_run_logs_return_successful_complete_evidence_and_full_digest() -> None:
    text = "build ok\ntests ok"
    client = FakeGhClient([_run(), {"stdout": text}, _run()])

    result = await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert result.run_id == 123
    assert result.attempt == 2
    assert result.head_sha == "a" * 40
    assert result.status == "completed"
    assert result.conclusion == "success"
    assert result.text == text
    assert result.total_bytes == len(text.encode())
    assert result.bytes_returned == result.total_bytes
    assert result.truncated is False
    assert result.warning is None
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert client.calls[1][0][-1] == "--log"


async def test_run_logs_preserve_failed_and_empty_logs_without_false_truncation() -> None:
    failed = _run(conclusion="failure")
    client = FakeGhClient([failed, {"stdout": ""}, failed])

    result = await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert result.conclusion == "failure"
    assert result.text == ""
    assert result.total_bytes == 0
    assert result.bytes_returned == 0
    assert result.truncated is False
    assert result.sha256 == hashlib.sha256(b"").hexdigest()


async def test_run_logs_max_bytes_truncation_keeps_complete_source_digest() -> None:
    text = "abcdefghij"
    client = FakeGhClient([_run(), {"stdout": text}, _run()])

    result = await gh_get_run_logs(
        "octo",
        "repo",
        123,
        2,
        ctx=_context(client, hard_max=6),
        max_bytes=9,
    )

    assert result.text == "abcdef"
    assert result.bytes_returned == 6
    assert result.total_bytes == 10
    assert result.truncated is True
    assert result.warning is not None
    assert "capped at the server hard limit of 6 bytes" in result.warning
    assert "complete retrieved source log" in result.warning
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()


async def test_run_logs_tail_selection_is_utf8_safe_and_marks_incomplete() -> None:
    text = "prefix-αβ-tail"
    client = FakeGhClient([_run(), {"stdout": text}, _run()])

    result = await gh_get_run_logs(
        "octo",
        "repo",
        123,
        2,
        ctx=_context(client),
        tail_bytes=6,
    )

    assert result.text == "-tail"
    assert result.bytes_returned == 5
    assert result.total_bytes == len(text.encode())
    assert result.truncated is True
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()


async def test_run_logs_literal_markers_are_inclusive_and_no_regex_is_interpreted() -> None:
    text = "before [start].* literal [end] after"
    client = FakeGhClient([_run(), {"stdout": text}, _run()])

    result = await gh_get_run_logs(
        "octo",
        "repo",
        123,
        2,
        ctx=_context(client),
        start_marker="[start]",
        end_marker="[end]",
    )

    assert result.text == "[start].* literal [end]"
    assert result.truncated is True
    assert result.warning is not None
    assert "literal start_marker" in result.warning
    assert "literal end_marker" in result.warning


async def test_run_logs_missing_marker_fails_closed() -> None:
    client = FakeGhClient([_run(), {"stdout": "complete log"}, _run()])

    with pytest.raises(ValueError, match="start_marker was not found"):
        await gh_get_run_logs(
            "octo",
            "repo",
            123,
            2,
            ctx=_context(client),
            start_marker="missing",
        )


async def test_run_logs_reject_attempt_mismatch_before_log_read() -> None:
    client = FakeGhClient([_run(attempt=3)])

    with pytest.raises(RuntimeError, match="attempt mismatch"):
        await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert len(client.calls) == 1
    assert "--log" not in client.calls[0][0]


async def test_job_logs_verify_exact_job_membership_and_return_attempt_identity() -> None:
    text = "job succeeded"
    client = FakeGhClient(
        [_job(), _job_run(), {"stdout": text}, _job(), _job_run()]
    )

    result = await gh_get_job_logs("octo", "repo", 456, 2, ctx=_context(client))

    assert result.run_id == 123
    assert result.attempt == 2
    assert result.job_id == 456
    assert result.head_sha == "a" * 40
    assert result.text == text
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert client.calls[0][0] == (
        "api",
        "repos/octo/repo/actions/jobs/456",
        "-X",
        "GET",
    )
    assert "--attempt" in client.calls[1][0]
    assert "--job" in client.calls[2][0]
    assert "--log" in client.calls[2][0]
    flattened = " ".join(" ".join(args) for args, _ in client.calls)
    for forbidden in ("rerun", "cancel", "delete", "workflow run"):
        assert forbidden not in flattened


async def test_job_logs_reject_attempt_that_does_not_contain_exact_job() -> None:
    client = FakeGhClient([_job(), _job_run(job_id=999)])

    with pytest.raises(RuntimeError, match="does not belong to run 123 attempt 2"):
        await gh_get_job_logs("octo", "repo", 456, 2, ctx=_context(client))

    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        GitHubRequestError("run missing", status_code=404),
        GitHubRequestError("run forbidden", status_code=403),
    ],
)
async def test_run_logs_preserve_governed_missing_or_forbidden_run_failure(
    failure: GitHubRequestError,
) -> None:
    client = FakeGhClient([failure])

    with pytest.raises(GitHubRequestError, match=str(failure)):
        await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))


async def test_job_logs_preserve_missing_job_failure() -> None:
    failure = GitHubRequestError("job missing", status_code=404)
    client = FakeGhClient([failure])

    with pytest.raises(GitHubRequestError, match="job missing"):
        await gh_get_job_logs("octo", "repo", 456, 2, ctx=_context(client))

    assert len(client.calls) == 1


def test_tail_and_markers_are_mutually_exclusive() -> None:
    from mcp_gh_server.action_log_evidence import select_action_log_evidence

    with pytest.raises(ValueError, match="tail_bytes cannot be combined"):
        select_action_log_evidence(
            "log",
            requested_max_bytes=None,
            hard_max_bytes=100,
            tail_bytes=10,
            start_marker="start",
        )
