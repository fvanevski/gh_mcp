"""Bounded read-only GitHub Actions job and run log evidence tools."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context
from pydantic import Field

from ..action_log_evidence import select_action_log_evidence
from ..action_log_models import WorkflowJobLogs, WorkflowRunLogs
from ..evidence import BoundedTextEvidence
from ..tooling import (
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    mcp,
    validate_repository,
)
from .actions import _get_run_snapshot


async def _get_job_snapshot(
    app: AppContext,
    owner: str,
    repo: str,
    job_id: int,
    attempt: int,
) -> tuple[int, int, str, str, str | None, str | None]:
    """Resolve one job and prove that it belongs to the requested run attempt."""

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
    status = payload.get("status")
    conclusion = payload.get("conclusion")
    url = payload.get("html_url")
    if not isinstance(actual_job_id, int) or actual_job_id != job_id:
        raise RuntimeError("Workflow job identity mismatch")
    if not isinstance(run_id, int) or run_id < 1:
        raise RuntimeError("Workflow job payload is missing a valid run ID")
    if not isinstance(head_sha, str) or OBJECT_SHA_RE.fullmatch(head_sha) is None:
        raise RuntimeError("Workflow job payload is missing a valid head SHA")
    if not isinstance(status, str):
        raise RuntimeError("Workflow job payload is missing a valid status")
    if conclusion is not None and not isinstance(conclusion, str):
        raise RuntimeError("Workflow job payload has an invalid conclusion")
    if url is not None and not isinstance(url, str):
        raise RuntimeError("Workflow job payload has an invalid URL")

    run_payload = await app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--attempt",
        str(attempt),
        "--json",
        "attempt,headSha,jobs",
    )
    if not isinstance(run_payload, dict):
        raise RuntimeError("GitHub returned an invalid workflow-run attempt payload")
    actual_attempt = run_payload.get("attempt")
    run_head_sha = run_payload.get("headSha")
    jobs = run_payload.get("jobs")
    if not isinstance(actual_attempt, int) or actual_attempt != attempt:
        raise RuntimeError(
            f"Workflow run attempt mismatch: requested {attempt}, got {actual_attempt!r}"
        )
    if not isinstance(run_head_sha, str) or OBJECT_SHA_RE.fullmatch(run_head_sha) is None:
        raise RuntimeError("Workflow run attempt payload is missing a valid head SHA")
    if run_head_sha.casefold() != head_sha.casefold():
        raise RuntimeError("Workflow job head SHA does not match the requested run attempt")
    if not isinstance(jobs, list):
        raise RuntimeError("Workflow run attempt payload is missing its job list")

    member_ids = {
        job.get("databaseId")
        for job in jobs
        if isinstance(job, dict) and isinstance(job.get("databaseId"), int)
    }
    if job_id not in member_ids:
        raise RuntimeError(
            f"Workflow job {job_id} does not belong to run {run_id} attempt {attempt}"
        )

    return run_id, actual_attempt, head_sha.casefold(), status, conclusion, url


def _select_log(
    app: AppContext,
    text: str,
    *,
    max_bytes: int | None,
    tail_bytes: int | None,
    start_marker: str | None,
    end_marker: str | None,
    label: str,
) -> BoundedTextEvidence:
    return select_action_log_evidence(
        text,
        requested_max_bytes=max_bytes,
        hard_max_bytes=app.settings.max_action_log_bytes,
        tail_bytes=tail_bytes,
        start_marker=start_marker,
        end_marker=end_marker,
        label=label,
    )


@mcp.tool(
    title="Get exact workflow job logs",
    description=(
        "Read-only: return bounded log evidence for one exact GitHub Actions job and "
        "explicit run attempt. Supports a UTF-8 byte cap, a literal tail selection, or "
        "inclusive literal start/end markers; it exposes no regex, shell, rerun, cancel, "
        "delete, or dispatch operation. sha256 fingerprints the complete retrieved source "
        "log before selection."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_job_logs(
    owner: str,
    repo: str,
    job_id: Annotated[int, Field(ge=1)],
    attempt: Annotated[int, Field(ge=1)],
    ctx: Context[AppContext],
    max_bytes: Annotated[int | None, Field(ge=1, le=1_000_000)] = None,
    tail_bytes: Annotated[int | None, Field(ge=1, le=1_000_000)] = None,
    start_marker: Annotated[str | None, Field(min_length=1, max_length=4096)] = None,
    end_marker: Annotated[str | None, Field(min_length=1, max_length=4096)] = None,
) -> WorkflowJobLogs:
    """Return bounded logs pinned to an exact job and caller-specified run attempt."""

    app = app_from_context(ctx)
    validate_repository(app.settings, owner, repo)
    before = await _get_job_snapshot(app, owner, repo, job_id, attempt)
    run_id, resolved_attempt, head_sha, status, conclusion, url = before

    raw = await app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--attempt",
        str(resolved_attempt),
        "--job",
        str(job_id),
        "--log",
        json_output=False,
    )
    text = raw.get("stdout", "") if isinstance(raw, dict) else ""
    if not isinstance(text, str):
        raise RuntimeError("GitHub returned invalid workflow-job log output")

    after = await _get_job_snapshot(app, owner, repo, job_id, attempt)
    if after != before:
        raise RuntimeError("Workflow job identity changed during the log evidence read")

    evidence = _select_log(
        app,
        text,
        max_bytes=max_bytes,
        tail_bytes=tail_bytes,
        start_marker=start_marker,
        end_marker=end_marker,
        label="Workflow job log evidence",
    )
    return WorkflowJobLogs(
        run_id=run_id,
        attempt=resolved_attempt,
        job_id=job_id,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
        url=url,
        text=evidence.content,
        truncated=evidence.truncated,
        bytes_returned=evidence.bytes_returned,
        total_bytes=evidence.total_bytes,
        sha256=evidence.sha256,
        warning=evidence.warning,
    )


@mcp.tool(
    title="Get exact workflow run logs",
    description=(
        "Read-only: return bounded full-log evidence for one exact GitHub Actions workflow "
        "run attempt. The attempt is mandatory and is never silently replaced by the latest "
        "attempt. Supports a UTF-8 byte cap, a literal tail selection, or inclusive literal "
        "start/end markers; it exposes no regex, shell, rerun, cancel, delete, or dispatch "
        "operation. sha256 fingerprints the complete retrieved source log before selection."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_run_logs(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(ge=1)],
    attempt: Annotated[int, Field(ge=1)],
    ctx: Context[AppContext],
    max_bytes: Annotated[int | None, Field(ge=1, le=1_000_000)] = None,
    tail_bytes: Annotated[int | None, Field(ge=1, le=1_000_000)] = None,
    start_marker: Annotated[str | None, Field(min_length=1, max_length=4096)] = None,
    end_marker: Annotated[str | None, Field(min_length=1, max_length=4096)] = None,
) -> WorkflowRunLogs:
    """Return bounded full logs pinned to one explicit workflow-run attempt."""

    app = app_from_context(ctx)
    validate_repository(app.settings, owner, repo)
    before = await _get_run_snapshot(app, owner, repo, run_id, attempt)
    resolved_attempt, head_sha, status, conclusion, url = before

    raw = await app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--attempt",
        str(resolved_attempt),
        "--log",
        json_output=False,
    )
    text = raw.get("stdout", "") if isinstance(raw, dict) else ""
    if not isinstance(text, str):
        raise RuntimeError("GitHub returned invalid workflow-run log output")

    after = await _get_run_snapshot(app, owner, repo, run_id, attempt)
    if after != before:
        raise RuntimeError("Workflow run identity changed during the log evidence read")

    evidence = _select_log(
        app,
        text,
        max_bytes=max_bytes,
        tail_bytes=tail_bytes,
        start_marker=start_marker,
        end_marker=end_marker,
        label="Workflow run log evidence",
    )
    return WorkflowRunLogs(
        run_id=run_id,
        attempt=resolved_attempt,
        head_sha=head_sha.casefold(),
        status=status,
        conclusion=conclusion,
        url=url,
        text=evidence.content,
        truncated=evidence.truncated,
        bytes_returned=evidence.bytes_returned,
        total_bytes=evidence.total_bytes,
        sha256=evidence.sha256,
        warning=evidence.warning,
    )
