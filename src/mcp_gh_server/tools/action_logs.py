"""Bounded read-only GitHub Actions job and run log evidence tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from mcp.server.mcpserver import Context
from pydantic import Field

from ..action_log_evidence import ActionLogEvidenceAccumulator
from ..action_log_models import WorkflowJobLogs, WorkflowRunLogs
from ..evidence import BoundedTextEvidence
from ..tooling import (
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repository,
)

_GITHUB_JOBS_PER_PAGE_MAX = 100


@dataclass(frozen=True, slots=True)
class _RunAttemptSnapshot:
    run_id: int
    attempt: int
    head_sha: str
    status: str
    conclusion: str | None
    url: str | None

    @property
    def identity(self) -> tuple[int, int, str]:
        return self.run_id, self.attempt, self.head_sha


@dataclass(frozen=True, slots=True)
class _AttemptJob:
    job_id: int
    run_id: int
    head_sha: str
    name: str
    status: str
    conclusion: str | None

    @property
    def identity(self) -> tuple[int, int, str]:
        return self.job_id, self.run_id, self.head_sha


@dataclass(frozen=True, slots=True)
class _JobSnapshot:
    run: _RunAttemptSnapshot
    job: _AttemptJob
    url: str | None

    @property
    def identity(self) -> tuple[int, int, int, str]:
        return self.run.run_id, self.run.attempt, self.job.job_id, self.run.head_sha


async def _get_run_attempt_snapshot(
    app: AppContext,
    owner: str,
    repo: str,
    run_id: int,
    attempt: int,
) -> _RunAttemptSnapshot:
    """Resolve one exact run attempt through its immutable REST resource."""

    payload = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}",
        "-X",
        "GET",
    )
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an invalid workflow-run attempt payload")

    actual_run_id = payload.get("id")
    actual_attempt = payload.get("run_attempt")
    head_sha = payload.get("head_sha")
    status = payload.get("status")
    conclusion = payload.get("conclusion")
    url = payload.get("html_url")
    if not isinstance(actual_run_id, int) or actual_run_id != run_id:
        raise RuntimeError("Workflow run identity mismatch")
    if not isinstance(actual_attempt, int) or actual_attempt != attempt:
        raise RuntimeError(
            f"Workflow run attempt mismatch: requested {attempt}, got {actual_attempt!r}"
        )
    if not isinstance(head_sha, str) or OBJECT_SHA_RE.fullmatch(head_sha) is None:
        raise RuntimeError("Workflow run attempt payload is missing a valid head SHA")
    if not isinstance(status, str) or not status:
        raise RuntimeError("Workflow run attempt payload is missing a valid status")
    if conclusion is not None and not isinstance(conclusion, str):
        raise RuntimeError("Workflow run attempt payload has an invalid conclusion")
    if url is not None and not isinstance(url, str):
        raise RuntimeError("Workflow run attempt payload has an invalid URL")

    return _RunAttemptSnapshot(
        run_id=actual_run_id,
        attempt=actual_attempt,
        head_sha=head_sha.casefold(),
        status=status,
        conclusion=conclusion,
        url=url,
    )


def _parse_attempt_job(
    item: object,
    *,
    run_id: int,
    expected_head_sha: str,
) -> _AttemptJob:
    if not isinstance(item, dict):
        raise RuntimeError("GitHub returned an invalid workflow-job entry")

    job_id = item.get("id")
    item_run_id = item.get("run_id")
    head_sha = item.get("head_sha")
    name = item.get("name")
    status = item.get("status")
    conclusion = item.get("conclusion")
    if not isinstance(job_id, int) or job_id < 1:
        raise RuntimeError("Workflow job entry is missing a valid job ID")
    if not isinstance(item_run_id, int) or item_run_id != run_id:
        raise RuntimeError("Workflow job entry does not match the requested run")
    if not isinstance(head_sha, str) or OBJECT_SHA_RE.fullmatch(head_sha) is None:
        raise RuntimeError("Workflow job entry is missing a valid head SHA")
    normalized_sha = head_sha.casefold()
    if normalized_sha != expected_head_sha:
        raise RuntimeError("Workflow job head SHA does not match the requested run attempt")
    if not isinstance(name, str):
        raise RuntimeError("Workflow job entry is missing a valid name")
    if not isinstance(status, str) or not status:
        raise RuntimeError("Workflow job entry is missing a valid status")
    if conclusion is not None and not isinstance(conclusion, str):
        raise RuntimeError("Workflow job entry has an invalid conclusion")

    return _AttemptJob(
        job_id=job_id,
        run_id=item_run_id,
        head_sha=normalized_sha,
        name=name,
        status=status,
        conclusion=conclusion,
    )


async def _get_attempt_job_page(
    app: AppContext,
    owner: str,
    repo: str,
    run: _RunAttemptSnapshot,
    page: int,
) -> tuple[int, list[_AttemptJob]]:
    payload = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/runs/{run.run_id}/attempts/{run.attempt}/jobs",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={_GITHUB_JOBS_PER_PAGE_MAX}",
    )
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an invalid workflow-run jobs payload")

    total_count = payload.get("total_count")
    raw_jobs = payload.get("jobs")
    if not isinstance(total_count, int) or total_count < 0:
        raise RuntimeError("Workflow-run jobs payload is missing a valid total_count")
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Workflow-run jobs payload is missing its jobs list")

    jobs = [
        _parse_attempt_job(item, run_id=run.run_id, expected_head_sha=run.head_sha)
        for item in raw_jobs
    ]
    return total_count, jobs


async def _list_attempt_jobs(
    app: AppContext,
    owner: str,
    repo: str,
    run: _RunAttemptSnapshot,
) -> list[_AttemptJob]:
    """Return the complete bounded job set for one exact run attempt."""

    jobs: list[_AttemptJob] = []
    total_count: int | None = None
    page = 1
    while True:
        page_total, page_jobs = await _get_attempt_job_page(app, owner, repo, run, page)
        if total_count is None:
            total_count = page_total
            if total_count > app.settings.max_action_log_jobs:
                raise RuntimeError(
                    f"Workflow run attempt contains {total_count} jobs, exceeding "
                    f"MCP_GH_MAX_ACTION_LOG_JOBS={app.settings.max_action_log_jobs}; "
                    "use gh_get_job_logs for a specific job or raise the deployment cap."
                )
        elif page_total != total_count:
            raise RuntimeError("Workflow job count changed while enumerating the run attempt")

        jobs.extend(page_jobs)
        if len(jobs) >= total_count:
            break
        if not page_jobs:
            raise RuntimeError("Workflow job pagination ended before total_count was satisfied")
        page += 1

    if len(jobs) != total_count:
        raise RuntimeError("Workflow job pagination returned an inconsistent job count")
    identities = [job.job_id for job in jobs]
    if len(set(identities)) != len(identities):
        raise RuntimeError("Workflow job pagination returned duplicate job IDs")
    return sorted(jobs, key=lambda job: job.job_id)


async def _verify_job_membership(
    app: AppContext,
    owner: str,
    repo: str,
    run: _RunAttemptSnapshot,
    target: _AttemptJob,
) -> None:
    """Prove target membership while bounding pagination work."""

    scanned = 0
    total_count: int | None = None
    page = 1
    while True:
        page_total, page_jobs = await _get_attempt_job_page(app, owner, repo, run, page)
        if total_count is None:
            total_count = page_total
        elif total_count != page_total:
            raise RuntimeError("Workflow job count changed while verifying job membership")

        for job in page_jobs:
            if job.job_id == target.job_id:
                if job.identity != target.identity:
                    raise RuntimeError(
                        "Workflow job identity does not match the requested run attempt"
                    )
                return

        scanned += len(page_jobs)
        if scanned >= total_count:
            break
        if scanned >= app.settings.max_action_log_jobs:
            raise RuntimeError(
                f"Could not prove workflow job {target.job_id} membership within "
                f"MCP_GH_MAX_ACTION_LOG_JOBS={app.settings.max_action_log_jobs}; "
                "raise the deployment cap to inspect a larger attempt."
            )
        if not page_jobs:
            raise RuntimeError("Workflow job pagination ended before membership was resolved")
        page += 1

    raise RuntimeError(
        f"Workflow job {target.job_id} does not belong to run {run.run_id} attempt {run.attempt}"
    )


async def _get_job_snapshot(
    app: AppContext,
    owner: str,
    repo: str,
    job_id: int,
    attempt: int,
) -> _JobSnapshot:
    """Resolve one exact job and prove membership in the caller-supplied attempt."""

    payload = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/jobs/{job_id}",
        "-X",
        "GET",
    )
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an invalid workflow-job payload")

    actual_job_id = payload.get("id")
    run_id = payload.get("run_id")
    head_sha = payload.get("head_sha")
    name = payload.get("name")
    status = payload.get("status")
    conclusion = payload.get("conclusion")
    url = payload.get("html_url")
    if not isinstance(actual_job_id, int) or actual_job_id != job_id:
        raise RuntimeError("Workflow job identity mismatch")
    if not isinstance(run_id, int) or run_id < 1:
        raise RuntimeError("Workflow job payload is missing a valid run ID")
    if not isinstance(head_sha, str) or OBJECT_SHA_RE.fullmatch(head_sha) is None:
        raise RuntimeError("Workflow job payload is missing a valid head SHA")
    if not isinstance(name, str):
        raise RuntimeError("Workflow job payload is missing a valid name")
    if not isinstance(status, str) or not status:
        raise RuntimeError("Workflow job payload is missing a valid status")
    if conclusion is not None and not isinstance(conclusion, str):
        raise RuntimeError("Workflow job payload has an invalid conclusion")
    if url is not None and not isinstance(url, str):
        raise RuntimeError("Workflow job payload has an invalid URL")

    run = await _get_run_attempt_snapshot(app, owner, repo, run_id, attempt)
    normalized_sha = head_sha.casefold()
    if run.head_sha != normalized_sha:
        raise RuntimeError("Workflow job head SHA does not match the requested run attempt")

    job = _AttemptJob(
        job_id=actual_job_id,
        run_id=run_id,
        head_sha=normalized_sha,
        name=name,
        status=status,
        conclusion=conclusion,
    )
    await _verify_job_membership(app, owner, repo, run, job)
    return _JobSnapshot(run=run, job=job, url=url)


def _new_accumulator(
    app: AppContext,
    *,
    max_bytes: int | None,
    tail_bytes: int | None,
    start_marker: str | None,
    end_marker: str | None,
    label: str,
) -> ActionLogEvidenceAccumulator:
    return ActionLogEvidenceAccumulator(
        requested_max_bytes=max_bytes,
        hard_max_bytes=app.settings.max_action_log_bytes,
        tail_bytes=tail_bytes,
        start_marker=start_marker,
        end_marker=end_marker,
        label=label,
    )


async def _stream_job_log(
    app: AppContext,
    owner: str,
    repo: str,
    job_id: int,
    accumulator: ActionLogEvidenceAccumulator,
) -> None:
    await app.client.stream_text(
        "api",
        f"repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
        "-X",
        "GET",
        on_chunk=accumulator.add_text,
        allow_escape_sequences=True,
    )


def _run_result_from_evidence(
    evidence: BoundedTextEvidence,
    *,
    run: _RunAttemptSnapshot,
) -> WorkflowRunLogs:
    return WorkflowRunLogs(
        run_id=run.run_id,
        attempt=run.attempt,
        head_sha=run.head_sha,
        status=run.status,
        conclusion=run.conclusion,
        url=run.url,
        text=evidence.content,
        truncated=evidence.truncated,
        bytes_returned=evidence.bytes_returned,
        total_bytes=evidence.total_bytes,
        sha256=evidence.sha256,
        warning=evidence.warning,
    )


def _job_result_from_evidence(
    evidence: BoundedTextEvidence,
    *,
    snapshot: _JobSnapshot,
) -> WorkflowJobLogs:
    return WorkflowJobLogs(
        run_id=snapshot.run.run_id,
        attempt=snapshot.run.attempt,
        job_id=snapshot.job.job_id,
        head_sha=snapshot.run.head_sha,
        status=snapshot.job.status,
        conclusion=snapshot.job.conclusion,
        url=snapshot.url,
        text=evidence.content,
        truncated=evidence.truncated,
        bytes_returned=evidence.bytes_returned,
        total_bytes=evidence.total_bytes,
        sha256=evidence.sha256,
        warning=evidence.warning,
    )


@mcp.tool(
    title="Get exact workflow job logs",
    description=(
        "Read-only: stream bounded plaintext log evidence for one exact GitHub Actions job "
        "and explicit run attempt. The server verifies exact attempt membership before and "
        "after retrieval and never downloads a workflow-run log archive. Supports a UTF-8 "
        "byte cap, a literal tail selection, or inclusive literal start/end markers; it "
        "exposes no regex, shell, rerun, cancel, delete, or dispatch operation. sha256 "
        "fingerprints the complete normalized plaintext source before selection."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_job_logs(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    job_id: Annotated[int, Field(ge=1, description="Positive workflow job identifier.")],
    attempt: Annotated[int, Field(ge=1, description="Exact workflow run attempt.")],
    *,
    ctx: Context[AppContext],
    max_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description="Maximum returned UTF-8 bytes, capped by server policy.",
        ),
    ] = None,
    tail_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description="Return only the final bounded UTF-8 bytes of the source log.",
        ),
    ] = None,
    start_marker: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=4096,
            description="Inclusive literal start marker; never treated as a regex.",
        ),
    ] = None,
    end_marker: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=4096,
            description="Inclusive literal end marker at or after the selected start.",
        ),
    ] = None,
) -> WorkflowJobLogs:
    """Return streamed bounded logs pinned to one exact job/run-attempt identity."""

    logger.info("MCP tool invocation reached server: tool=gh_get_job_logs")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    before = await _get_job_snapshot(app, owner, repo, job_id, attempt)
    if before.job.status != "completed":
        raise RuntimeError(f"Workflow job {job_id} is not completed; logs are not immutable yet")

    accumulator = _new_accumulator(
        app,
        max_bytes=max_bytes,
        tail_bytes=tail_bytes,
        start_marker=start_marker,
        end_marker=end_marker,
        label="Workflow job log evidence",
    )
    await _stream_job_log(app, owner, repo, job_id, accumulator)

    after = await _get_job_snapshot(app, owner, repo, job_id, attempt)
    if after.identity != before.identity:
        raise RuntimeError("Workflow job identity changed during the log evidence read")

    evidence = accumulator.finish()
    return _job_result_from_evidence(evidence, snapshot=before)


@mcp.tool(
    title="Get exact workflow run logs",
    description=(
        "Read-only: stream bounded log evidence for one exact GitHub Actions workflow run "
        "attempt by enumerating that attempt's jobs and reading their plaintext job-log "
        "endpoints in stable job-ID order. The run-log ZIP endpoint is never used. The "
        "attempt is mandatory and is never silently replaced by the latest attempt. "
        "Supports a UTF-8 byte cap, a literal tail selection, or inclusive literal "
        "start/end markers; it exposes no regex, shell, rerun, cancel, delete, or dispatch "
        "operation. sha256 fingerprints the complete normalized aggregate before selection."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_run_logs(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    run_id: Annotated[int, Field(ge=1, description="Positive workflow run identifier.")],
    attempt: Annotated[int, Field(ge=1, description="Exact workflow run attempt.")],
    *,
    ctx: Context[AppContext],
    max_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description="Maximum returned UTF-8 bytes, capped by server policy.",
        ),
    ] = None,
    tail_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description="Return only the final bounded UTF-8 bytes of the source log.",
        ),
    ] = None,
    start_marker: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=4096,
            description="Inclusive literal start marker; never treated as a regex.",
        ),
    ] = None,
    end_marker: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=4096,
            description="Inclusive literal end marker at or after the selected start.",
        ),
    ] = None,
) -> WorkflowRunLogs:
    """Return streamed bounded logs pinned to one explicit completed run attempt."""

    logger.info("MCP tool invocation reached server: tool=gh_get_run_logs")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    before = await _get_run_attempt_snapshot(app, owner, repo, run_id, attempt)
    if before.status != "completed":
        raise RuntimeError(
            f"Workflow run {run_id} attempt {attempt} is not completed; logs are not immutable yet"
        )

    jobs_before = await _list_attempt_jobs(app, owner, repo, before)
    for job in jobs_before:
        if job.status != "completed":
            raise RuntimeError(
                f"Workflow job {job.job_id} in run {run_id} attempt {attempt} is not completed"
            )

    accumulator = _new_accumulator(
        app,
        max_bytes=max_bytes,
        tail_bytes=tail_bytes,
        start_marker=start_marker,
        end_marker=end_marker,
        label="Workflow run log evidence",
    )
    first_log = True
    for job in jobs_before:
        if job.conclusion == "skipped":
            continue
        if not first_log:
            accumulator.add_text("\n")
        await _stream_job_log(app, owner, repo, job.job_id, accumulator)
        first_log = False

    after = await _get_run_attempt_snapshot(app, owner, repo, run_id, attempt)
    if after.identity != before.identity:
        raise RuntimeError("Workflow run identity changed during the log evidence read")
    jobs_after = await _list_attempt_jobs(app, owner, repo, after)
    if tuple(job.identity for job in jobs_after) != tuple(job.identity for job in jobs_before):
        raise RuntimeError("Workflow run job membership changed during the log evidence read")

    evidence = accumulator.finish()
    return _run_result_from_evidence(evidence, run=before)
