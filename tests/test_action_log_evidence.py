"""Regression coverage for streamed bounded Actions log evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from mcp_gh_server.action_log_evidence import (
    ActionLogEvidenceAccumulator,
    select_action_log_evidence,
)
from mcp_gh_server.request_governor import GitHubRequestError, GitHubRequestMetadata
from mcp_gh_server.server import AppContext, gh_get_job_logs, gh_get_run_logs
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Return queued GitHub results and streamed text while recording every command."""

    results: list[Any]
    streams: list[Any] = field(default_factory=list)
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    stream_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def stream_text(
        self,
        *args: str,
        on_chunk: Callable[[str], None],
        timeout: float | None = None,
    ) -> GitHubRequestMetadata:
        self.stream_calls.append((args, {"timeout": timeout}))
        result = self.streams.pop(0)
        if isinstance(result, Exception):
            raise result
        chunks = result if isinstance(result, list) else [result]
        for chunk in chunks:
            on_chunk(chunk)
        return GitHubRequestMetadata()


def _context(
    client: FakeGhClient,
    *,
    hard_max: int = 500_000,
    max_jobs: int = 100,
) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            max_action_log_bytes=hard_max,
            max_action_log_jobs=max_jobs,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _run(
    *,
    run_id: int = 123,
    attempt: int = 2,
    head_sha: str = "a" * 40,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "run_attempt": attempt,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://github.com/octo/repo/actions/runs/123",
    }


def _job(
    *,
    job_id: int = 456,
    run_id: int = 123,
    head_sha: str = "a" * 40,
    name: str = "tests",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "run_id": run_id,
        "head_sha": head_sha,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/octo/repo/actions/runs/{run_id}/job/{job_id}",
    }


def _jobs(*jobs: dict[str, Any], total_count: int | None = None) -> dict[str, Any]:
    return {
        "total_count": len(jobs) if total_count is None else total_count,
        "jobs": list(jobs),
    }


def _single_run_results(
    *,
    run: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
) -> list[Any]:
    run_payload = _run() if run is None else run
    job_payload = _job() if job is None else job
    jobs_payload = _jobs(job_payload)
    return [run_payload, jobs_payload, run_payload, jobs_payload]


def _single_job_results(
    *,
    run: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    membership_job: dict[str, Any] | None = None,
) -> list[Any]:
    run_payload = _run() if run is None else run
    job_payload = _job() if job is None else job
    member = job_payload if membership_job is None else membership_job
    jobs_payload = _jobs(member)
    return [
        job_payload,
        run_payload,
        jobs_payload,
        job_payload,
        run_payload,
        jobs_payload,
    ]


async def test_run_logs_stream_successful_complete_evidence_without_run_archive() -> None:
    text = "build ok\ntests ok"
    client = FakeGhClient(_single_run_results(), streams=[text])

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
    assert client.stream_calls == [
        (
            (
                "api",
                "repos/octo/repo/actions/jobs/456/logs",
                "-X",
                "GET",
            ),
            {"timeout": None},
        )
    ]
    flattened = " ".join(
        " ".join(args) for args, _ in [*client.calls, *client.stream_calls]
    )
    assert "actions/runs/123/attempts/2/logs" not in flattened
    assert "run view" not in flattened


async def test_run_logs_preserve_failed_and_empty_logs_without_false_truncation() -> None:
    failed_run = _run(conclusion="failure")
    failed_job = _job(conclusion="failure")
    client = FakeGhClient(
        _single_run_results(run=failed_run, job=failed_job),
        streams=[""],
    )

    result = await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert result.conclusion == "failure"
    assert result.text == ""
    assert result.total_bytes == 0
    assert result.bytes_returned == 0
    assert result.truncated is False
    assert result.sha256 == hashlib.sha256(b"").hexdigest()


async def test_run_logs_max_bytes_truncation_keeps_complete_stream_digest() -> None:
    text = "abcdefghij"
    client = FakeGhClient(_single_run_results(), streams=[["abc", "def", "ghij"]])

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
    assert "complete normalized evidence stream" in result.warning
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()


async def test_run_logs_tail_selection_is_utf8_safe_and_reports_actual_suffix_bytes() -> None:
    text = "prefix-αβ-tail"
    client = FakeGhClient(_single_run_results(), streams=[["prefix-α", "β-tail"]])

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
    assert result.warning is not None
    assert "UTF-8-safe suffix of 5 bytes" in result.warning
    assert "up to 6 requested" in result.warning
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()


async def test_run_logs_tail_preserves_log_end_when_hard_cap_is_smaller() -> None:
    text = "0123456789"
    client = FakeGhClient(_single_run_results(), streams=[text])

    result = await gh_get_run_logs(
        "octo",
        "repo",
        123,
        2,
        ctx=_context(client, hard_max=4),
        tail_bytes=8,
    )

    assert result.text == "6789"
    assert result.bytes_returned == 4
    assert result.total_bytes == 10
    assert result.truncated is True
    assert result.warning is not None
    assert "tail_bytes=8" in result.warning
    assert "effective max_bytes=4" in result.warning
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()


async def test_run_logs_literal_markers_work_across_stream_chunks_without_regex() -> None:
    text = "before [start].* literal [end] after"
    client = FakeGhClient(
        _single_run_results(),
        streams=[["before [st", "art].* literal [en", "d] after"]],
    )

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
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()


async def test_run_logs_missing_marker_fails_closed_after_complete_stream_scan() -> None:
    client = FakeGhClient(_single_run_results(), streams=[["complete ", "log"]])

    with pytest.raises(ValueError, match="start_marker was not found"):
        await gh_get_run_logs(
            "octo",
            "repo",
            123,
            2,
            ctx=_context(client),
            start_marker="missing",
        )


async def test_run_logs_reject_attempt_mismatch_before_any_log_stream() -> None:
    client = FakeGhClient([_run(attempt=3)])

    with pytest.raises(RuntimeError, match=r"attempt mismatch: requested 2, got 3"):
        await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert client.stream_calls == []


async def test_run_logs_reject_in_progress_attempt_before_job_enumeration() -> None:
    client = FakeGhClient([_run(status="in_progress", conclusion=None)])

    with pytest.raises(RuntimeError, match="not completed"):
        await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert len(client.calls) == 1
    assert client.stream_calls == []


async def test_run_logs_fail_closed_when_attempt_exceeds_job_cap() -> None:
    jobs_payload = _jobs(_job(), total_count=101)
    client = FakeGhClient([_run(), jobs_payload])

    with pytest.raises(RuntimeError, match="MCP_GH_MAX_ACTION_LOG_JOBS=100"):
        await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert client.stream_calls == []


async def test_run_logs_aggregate_non_skipped_jobs_in_stable_job_id_order() -> None:
    first = _job(job_id=111, name="first")
    second = _job(job_id=222, name="second")
    skipped = _job(job_id=333, name="skip", conclusion="skipped")
    before_jobs = _jobs(skipped, second, first)
    after_jobs = _jobs(first, skipped, second)
    client = FakeGhClient(
        [_run(), before_jobs, _run(), after_jobs],
        streams=["first log", "second log"],
    )

    result = await gh_get_run_logs("octo", "repo", 123, 2, ctx=_context(client))

    assert result.text == "first log\nsecond log"
    assert [call[0][1] for call in client.stream_calls] == [
        "repos/octo/repo/actions/jobs/111/logs",
        "repos/octo/repo/actions/jobs/222/logs",
    ]


async def test_job_logs_verify_exact_attempt_membership_and_stream_job_endpoint() -> None:
    text = "job succeeded"
    client = FakeGhClient(_single_job_results(), streams=[text])

    result = await gh_get_job_logs("octo", "repo", 456, 2, ctx=_context(client))

    assert result.run_id == 123
    assert result.attempt == 2
    assert result.job_id == 456
    assert result.head_sha == "a" * 40
    assert result.status == "completed"
    assert result.conclusion == "success"
    assert result.text == text
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert client.calls[0][0] == (
        "api",
        "repos/octo/repo/actions/jobs/456",
        "-X",
        "GET",
    )
    assert client.stream_calls[0][0] == (
        "api",
        "repos/octo/repo/actions/jobs/456/logs",
        "-X",
        "GET",
    )
    flattened = " ".join(
        " ".join(args) for args, _ in [*client.calls, *client.stream_calls]
    )
    for forbidden in (
        "rerun",
        "cancel",
        "delete",
        "actions/runs/123/attempts/2/logs",
        "run view",
    ):
        assert forbidden not in flattened


async def test_job_logs_reject_attempt_that_does_not_contain_exact_job() -> None:
    wrong_member = _job(job_id=999)
    client = FakeGhClient(
        [_job(), _run(), _jobs(wrong_member)],
    )

    with pytest.raises(RuntimeError, match="does not belong to run 123 attempt 2"):
        await gh_get_job_logs("octo", "repo", 456, 2, ctx=_context(client))

    assert client.stream_calls == []


async def test_job_logs_reject_invalid_head_sha_before_log_stream() -> None:
    client = FakeGhClient([_job(head_sha="z" * 40)])

    with pytest.raises(RuntimeError, match="valid head SHA"):
        await gh_get_job_logs("octo", "repo", 456, 2, ctx=_context(client))

    assert client.stream_calls == []


async def test_job_logs_reject_in_progress_job_before_log_stream() -> None:
    active_job = _job(status="in_progress", conclusion=None)
    client = FakeGhClient(
        [active_job, _run(status="in_progress", conclusion=None), _jobs(active_job)],
    )

    with pytest.raises(RuntimeError, match="not completed"):
        await gh_get_job_logs("octo", "repo", 456, 2, ctx=_context(client))

    assert client.stream_calls == []


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


def test_streaming_accumulator_bounds_memory_relevant_state_and_cross_chunk_markers() -> None:
    accumulator = ActionLogEvidenceAccumulator(
        requested_max_bytes=8,
        hard_max_bytes=8,
        start_marker="<start>",
        end_marker="<end>",
    )
    accumulator.add_text("x" * 10_000 + "<sta")
    accumulator.add_text("rt>abcdef")
    accumulator.add_text("ghijkl<e")
    accumulator.add_text("nd>" + "y" * 10_000)

    result = accumulator.finish()

    assert result.content == "<start>a"
    assert result.bytes_returned == 8
    assert result.total_bytes == len(
        ("x" * 10_000 + "<start>abcdefghijkl<end>" + "y" * 10_000).encode()
    )
    assert result.truncated is True


def test_tail_and_markers_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="tail_bytes cannot be combined"):
        select_action_log_evidence(
            "log",
            requested_max_bytes=None,
            hard_max_bytes=100,
            tail_bytes=10,
            start_marker="start",
        )
