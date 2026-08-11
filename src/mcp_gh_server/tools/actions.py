"""GitHub Actions workflow and run tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..evidence import pagination_evidence
from ..models import (
    SearchResults,
    WorkflowInfo,
    WorkflowJob,
    WorkflowJobsPage,
    WorkflowJobStep,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunFailedLogs,
    WorkflowRunsPage,
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

_GITHUB_RUNS_PER_PAGE_MAX = 100
_GITHUB_FILTERED_RUNS_MAX = 1_000


def _parse_run_created_boundary(value: str, *, field_name: str) -> datetime:
    """Parse one timezone-aware, whole-second ISO 8601 creation boundary."""

    if "T" not in value:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp with an explicit timezone")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an ISO 8601 timestamp with an explicit timezone"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone offset or Z")
    if parsed.microsecond:
        raise ValueError(f"{field_name} must use whole-second precision")
    return parsed.astimezone(UTC)


def _workflow_run_created_filter(
    created_from: str | None,
    created_to: str | None,
) -> str | None:
    """Build GitHub's inclusive created search expression in normalized UTC."""

    start = (
        _parse_run_created_boundary(created_from, field_name="created_from")
        if created_from is not None
        else None
    )
    end = (
        _parse_run_created_boundary(created_to, field_name="created_to")
        if created_to is not None
        else None
    )
    if start is not None and end is not None and start > end:
        raise ValueError("created_from must not be later than created_to")

    def render(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    if start is not None and end is not None:
        return f"{render(start)}..{render(end)}"
    if start is not None:
        return f">={render(start)}"
    if end is not None:
        return f"<={render(end)}"
    return None


def _workflow_run_search_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize the REST run shape to the historical gh-run-list item keys."""

    run_id = item.get("id")
    if not isinstance(run_id, int) or run_id < 1:
        raise RuntimeError("GitHub returned a workflow run without a valid id")
    name = item.get("name")
    workflow_name = name if isinstance(name, str) else ""
    display_title = item.get("display_title")
    return {
        "databaseId": run_id,
        "name": workflow_name,
        "displayTitle": display_title if isinstance(display_title, str) else "",
        "headBranch": item.get("head_branch"),
        "headSha": item.get("head_sha"),
        "conclusion": item.get("conclusion"),
        "status": item.get("status"),
        "event": item.get("event"),
        "url": item.get("html_url"),
        "createdAt": item.get("created_at"),
        "updatedAt": item.get("updated_at"),
        "startedAt": item.get("run_started_at"),
        "workflowName": workflow_name,
    }


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
    workflow_id: Annotated[
        int | None,
        Field(ge=1, description="Exact workflow identifier to scope the authoritative route."),
    ] = None,
    head_sha: Annotated[
        str | None,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact 40-character workflow-run head commit SHA.",
        ),
    ] = None,
    event: str | None = None,
    actor: str | None = None,
    created_from: Annotated[
        str | None,
        Field(description="Inclusive timezone-aware ISO 8601 creation lower bound."),
    ] = None,
    created_to: Annotated[
        str | None,
        Field(description="Inclusive timezone-aware ISO 8601 creation upper bound."),
    ] = None,
    check_suite_id: Annotated[
        int | None,
        Field(ge=1, description="Exact check-suite identifier."),
    ] = None,
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based result page.")] = 1,
) -> WorkflowRunsPage:
    """List one authoritative, bounded page of GitHub Actions workflow runs.

    Existing branch, status, and per_page callers remain supported. Exact workflow,
    head-SHA, event, actor, creation-range, and check-suite filters are sent to GitHub's
    workflow-runs REST route rather than applied locally.
    """

    app = app_from_context(ctx)
    validate_repository(owner, repo)
    if workflow_id is not None and workflow_id < 1:
        raise ValueError("workflow_id must be positive")
    if check_suite_id is not None and check_suite_id < 1:
        raise ValueError("check_suite_id must be positive")
    if head_sha is not None and not OBJECT_SHA_RE.fullmatch(head_sha):
        raise ValueError("head_sha must be exactly 40 hexadecimal characters")
    if page < 1:
        raise ValueError("page must be positive")
    if per_page is not None and per_page < 1:
        raise ValueError("per_page must be positive")

    created = _workflow_run_created_filter(created_from, created_to)
    requested_per_page = app.settings.default_max_results if per_page is None else per_page
    hard_per_page = min(app.settings.hard_max_results, _GITHUB_RUNS_PER_PAGE_MAX)
    effective_per_page = min(requested_per_page, hard_per_page)

    endpoint = f"repos/{owner}/{repo}/actions/runs"
    if workflow_id is not None:
        endpoint = f"repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs"

    args = [
        "api",
        endpoint,
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={effective_per_page}",
        "-f",
        "exclude_pull_requests=true",
    ]
    filters: list[tuple[str, str]] = []
    if branch:
        filters.append(("branch", branch))
    if status:
        filters.append(("status", status))
    if event:
        filters.append(("event", event))
    if actor:
        filters.append(("actor", actor))
    if created is not None:
        filters.append(("created", created))
    if check_suite_id is not None:
        filters.append(("check_suite_id", str(check_suite_id)))
    if head_sha is not None:
        filters.append(("head_sha", head_sha.casefold()))
    for key, value in filters:
        args.extend(["-f", f"{key}={value}"])

    result = await app.client.run(*args)
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return structured workflow runs")
    raw_runs = result.get("workflow_runs")
    if not isinstance(raw_runs, list):
        raise RuntimeError("GitHub did not return a workflow runs list")
    total_count = result.get("total_count")
    if not isinstance(total_count, int) or total_count < 0:
        raise RuntimeError("GitHub did not return a valid workflow run count")

    items: list[Any] = []
    for item in raw_runs:
        if not isinstance(item, dict):
            raise RuntimeError("GitHub returned a malformed workflow run item")
        items.append(_workflow_run_search_item(item))

    filtered_search = bool(filters)
    accessible_total = (
        min(total_count, _GITHUB_FILTERED_RUNS_MAX) if filtered_search else total_count
    )
    evidence = pagination_evidence(
        page=page,
        requested_per_page=per_page,
        default_per_page=app.settings.default_max_results,
        hard_max_results=hard_per_page,
        returned_count=len(items),
        total_count=accessible_total,
    )
    search_boundary_uncertain = filtered_search and total_count >= _GITHUB_FILTERED_RUNS_MAX
    warnings = [evidence.warning] if evidence.warning else []
    if search_boundary_uncertain:
        warnings.append(
            "GitHub limits workflow-run searches using actor/branch/check_suite_id/created/"
            "event/head_sha/status to at most 1,000 results; total_count at or above that "
            "boundary cannot establish completeness beyond the accessible search window."
        )

    return WorkflowRunsPage(
        total_count=total_count,
        items=items,
        truncated=evidence.truncated or search_boundary_uncertain,
        query=f"{owner}/{repo} runs",
        page=evidence.page,
        per_page=evidence.per_page,
        has_more=evidence.has_more,
        warning=" ".join(warnings) or None,
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
