"""GitHub Actions workflow and run tools."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..models import (
    SearchResults,
    WorkflowInfo,
    WorkflowJob,
    WorkflowJobsPage,
    WorkflowJobStep,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunFailedLogs,
    WorkflowRunWatchResult,
)
from ..tooling import (
    MUTATE_EXTERNAL,
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    bounded_utf8,
    created_url,
    logger,
    mcp,
    readback_warning,
    require_write_enabled,
    validate_repository,
    workflow_run_id,
)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_workflows(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "active",
    per_page: int | None = None,
) -> SearchResults:
    """List GitHub Actions workflows in a repository.

    state: active, all, disabled, disabled_inactivity, disabled_fork.
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "id,name,path,state"
    args = [
        "workflow",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--limit",
        str(limit),
    ]
    if state != "active":
        args.extend(["--all"])

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} workflows ({state})",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_get_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    *,
    ctx: Context[AppContext],
) -> WorkflowInfo:
    """Get details of a specific GitHub Actions workflow."""

    app = app_from_context(ctx)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/workflows/{workflow_id}",
    )
    return WorkflowInfo.model_validate(result)


@mcp.tool(annotations=MUTATE_EXTERNAL)
async def gh_run_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    ref: str = "main",
    *,
    ctx: Context[AppContext],
    fields: list[str] | None = None,
) -> WorkflowRunCreate:
    """Trigger a workflow dispatch event for a GitHub Actions workflow.

    The workflow must support an `on.workflow_dispatch` trigger.
    Use `fields` to pass inputs as key=value pairs (e.g. ["key=value"]).
    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true.
    """

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="workflow_dispatch")
    args = [
        "workflow",
        "run",
        str(workflow_id),
        "--repo",
        f"{owner}/{repo}",
        "--ref",
        ref,
    ]
    if fields:
        for field in fields:
            if "=" not in field or not field.split("=", 1)[0]:
                raise ValueError("workflow fields must use non-empty key=value form")
            args.extend(["-f", field])
        stdin_text = None
    else:
        args.append("--json")
        stdin_text = "{}"

    dispatch_result = await app.client.run(*args, json_output=False, stdin_text=stdin_text)
    stdout = dispatch_result.get("stdout", "") if isinstance(dispatch_result, dict) else ""
    if not isinstance(stdout, str) or not stdout.strip():
        warning = readback_warning("Workflow dispatch")
        return WorkflowRunCreate(
            run_id=None,
            url=None,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    created = created_url(dispatch_result, "Workflow run")
    run_id = workflow_run_id(created)
    try:
        result = await app.client.run(
            "run",
            "view",
            str(run_id),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "databaseId,url",
        )
    except RuntimeError:
        warning = readback_warning("Workflow dispatch", created)
        return WorkflowRunCreate(
            run_id=run_id,
            url=created,
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return WorkflowRunCreate(
        run_id=result.get("databaseId", run_id),
        url=result.get("url", created),
        message="Workflow dispatch triggered successfully.",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_runs(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    branch: str | None = None,
    status: str | None = None,
    per_page: int | None = None,
) -> SearchResults:
    """List recent GitHub Actions workflow runs.

    status: completed, in_progress, queued, pending, requested, waiting,
            actionable, null (all).
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "databaseId,name,displayTitle,headBranch,headSha,conclusion,status,"
        "event,url,createdAt,updatedAt,startedAt,workflowName"
    )
    args = [
        "run",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--limit",
        str(limit),
    ]
    if branch:
        args.extend(["--branch", branch])
    if status:
        args.extend(["--status", status])

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} runs",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_get_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    ctx: Context[AppContext],
) -> WorkflowRun:
    """Get details of a specific GitHub Actions workflow run."""

    app = app_from_context(ctx)
    fields = (
        "databaseId,name,displayTitle,headBranch,headSha,conclusion,status,"
        "event,url,createdAt,updatedAt,startedAt,workflowName"
    )
    result = await app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    return WorkflowRun.model_validate(result)


async def _get_run_snapshot(
    app: AppContext,
    owner: str,
    repo: str,
    run_id: int,
    attempt: int | None,
) -> tuple[int, str, str, str | None, str | None]:
    """Resolve one workflow-run attempt and its immutable head revision."""

    args = [
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        "attempt,headSha,status,conclusion,url",
    ]
    if attempt is not None:
        args.extend(["--attempt", str(attempt)])
    result = await app.client.run(*args)
    if not isinstance(result, dict):
        raise RuntimeError("GitHub CLI did not return workflow-run metadata")
    actual_attempt = result.get("attempt")
    head_sha = result.get("headSha")
    status = result.get("status")
    if not isinstance(actual_attempt, int) or actual_attempt < 1:
        raise RuntimeError("GitHub CLI did not return a valid workflow-run attempt")
    if attempt is not None and actual_attempt != attempt:
        raise RuntimeError(
            f"GitHub returned workflow-run attempt {actual_attempt}, expected {attempt}"
        )
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub CLI did not return a valid workflow-run head SHA")
    if not isinstance(status, str) or not status:
        raise RuntimeError("GitHub CLI did not return a workflow-run status")
    conclusion = result.get("conclusion")
    url = result.get("url")
    return (
        actual_attempt,
        head_sha,
        status,
        conclusion if isinstance(conclusion, str) else None,
        url if isinstance(url, str) else None,
    )


@mcp.tool(
    title="List workflow run jobs",
    description=(
        "Read-only: return one bounded page of jobs and step metadata for an exact "
        "GitHub Actions run attempt. This downloads no logs, performs no watching or "
        "workflow dispatch, and never modifies GitHub."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_run_jobs(
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
    *,
    ctx: Context[AppContext],
    attempt: Annotated[
        int | None,
        Field(ge=1, description="Exact run attempt; omit for the latest attempt."),
    ] = None,
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based result page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Jobs per page, capped by server policy."),
    ] = None,
) -> WorkflowJobsPage:
    """Return one bounded page of jobs pinned to an exact run attempt."""

    logger.info("MCP tool invocation reached server: tool=gh_list_run_jobs")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    resolved_attempt, head_sha, _, _, _ = await _get_run_snapshot(app, owner, repo, run_id, attempt)
    limit = min(app.client.clamp_max_results(per_page), 100)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{resolved_attempt}/jobs",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={limit}",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return structured workflow jobs")
    raw_jobs = result.get("jobs")
    if not isinstance(raw_jobs, list):
        raise RuntimeError("GitHub did not return a workflow jobs list")
    total_count = result.get("total_count")
    if not isinstance(total_count, int) or total_count < 0:
        raise RuntimeError("GitHub did not return a valid workflow job count")

    jobs: list[WorkflowJob] = []
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        raw_steps = item.get("steps")
        steps = (
            [
                WorkflowJobStep(
                    number=step.get("number", 0),
                    name=str(step.get("name", "")),
                    status=str(step.get("status", "unknown")),
                    conclusion=step.get("conclusion"),
                    started_at=step.get("started_at"),
                    completed_at=step.get("completed_at"),
                )
                for step in raw_steps
                if isinstance(step, dict)
            ]
            if isinstance(raw_steps, list)
            else []
        )
        jobs.append(
            WorkflowJob(
                id=item.get("id", 0),
                name=str(item.get("name", "")),
                status=str(item.get("status", "unknown")),
                conclusion=item.get("conclusion"),
                started_at=item.get("started_at"),
                completed_at=item.get("completed_at"),
                url=item.get("html_url"),
                runner_name=item.get("runner_name"),
                steps=steps,
            )
        )

    verified_attempt, verified_sha, _, _, _ = await _get_run_snapshot(
        app, owner, repo, run_id, resolved_attempt
    )
    if (verified_attempt, verified_sha) != (resolved_attempt, head_sha):
        raise RuntimeError("Workflow run attempt changed during the jobs read; retry")
    return WorkflowJobsPage(
        run_id=run_id,
        attempt=resolved_attempt,
        head_sha=head_sha,
        page=page,
        per_page=limit,
        total_count=total_count,
        has_more=page * limit < total_count,
        jobs=jobs,
    )


@mcp.tool(
    title="Read failed workflow logs",
    description=(
        "Read-only: return bounded failed-step log text for one exact GitHub Actions "
        "run attempt, with truncation metadata and a SHA-256 fingerprint. This never "
        "reruns, cancels, deletes, or dispatches a workflow and never requests input."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_failed_run_logs(
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
    *,
    ctx: Context[AppContext],
    attempt: Annotated[
        int | None,
        Field(ge=1, description="Exact run attempt; omit for the latest attempt."),
    ] = None,
    max_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description=(
                "Maximum UTF-8 bytes returned, capped by MCP_GH_MAX_FAILED_RUN_LOG_BYTES."
            ),
        ),
    ] = None,
) -> WorkflowRunFailedLogs:
    """Return bounded failed-step logs pinned to an exact run attempt."""

    logger.info("MCP tool invocation reached server: tool=gh_get_failed_run_logs")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    resolved_attempt, head_sha, status, conclusion, url = await _get_run_snapshot(
        app, owner, repo, run_id, attempt
    )
    result = await app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--attempt",
        str(resolved_attempt),
        "--log-failed",
        json_output=False,
    )
    content = result.get("stdout") if isinstance(result, dict) else None
    if not isinstance(content, str):
        raise RuntimeError("GitHub CLI did not return failed-step log text")
    verified_attempt, verified_sha, _, _, _ = await _get_run_snapshot(
        app, owner, repo, run_id, resolved_attempt
    )
    if (verified_attempt, verified_sha) != (resolved_attempt, head_sha):
        raise RuntimeError("Workflow run attempt changed during the log read; retry")

    limit = min(
        max_bytes or app.settings.max_failed_run_log_bytes,
        app.settings.max_failed_run_log_bytes,
    )
    bounded, returned, total, truncated, digest = bounded_utf8(content, limit)
    return WorkflowRunFailedLogs(
        run_id=run_id,
        attempt=resolved_attempt,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
        url=url,
        content=bounded,
        truncated=truncated,
        bytes_returned=returned,
        total_bytes=total,
        sha256=digest,
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_watch_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    ctx: Context[AppContext],
    interval: int = 10,
    exit_status: bool = False,
    timeout_seconds: int = 1800,
) -> WorkflowRunWatchResult:
    """Poll a GitHub Actions workflow run until completion or timeout."""

    app = app_from_context(ctx)
    validate_repository(owner, repo)
    if interval < 1 or timeout_seconds < 1:
        raise ValueError("interval and timeout_seconds must be positive")

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        view_result = await app.client.run(
            "run",
            "view",
            str(run_id),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "status,conclusion,url",
        )
        status = view_result.get("status") if isinstance(view_result, dict) else None
        conclusion = view_result.get("conclusion") if isinstance(view_result, dict) else None
        url = view_result.get("url") if isinstance(view_result, dict) else None
        if status == "completed":
            if exit_status and conclusion != "success":
                raise RuntimeError(f"Run #{run_id} completed with conclusion: {conclusion}")
            return WorkflowRunWatchResult(
                run_id=run_id,
                conclusion=conclusion,
                status=status,
                url=url,
                message=f"Run #{run_id} completed with conclusion: {conclusion or 'unknown'}",
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"Run #{run_id} did not complete within {timeout_seconds}s")
        await asyncio.sleep(interval)
