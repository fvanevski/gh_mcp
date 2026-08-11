"""0.6.x compatibility adapter for workflow dispatch writes."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import WorkflowRunCreate
from .request_governor import GitHubRequestResult
from .tooling import (
    AppContext,
    app_from_context,
    optional_created_url,
    require_write_enabled,
    workflow_run_id,
)
from .write_contracts import execute_write_readback, legacy_write_status


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

    created: str | None = None
    run_id: int | None = None

    async def write() -> GitHubRequestResult[Any]:
        nonlocal created, run_id
        result = await run_write_with_metadata(
            app.client,
            *args,
            json_output=False,
            stdin_text=stdin_text,
        )
        created = optional_created_url(result.value)
        if created is not None:
            try:
                run_id = workflow_run_id(created)
            except RuntimeError:
                run_id = None
        return result

    async def readback() -> dict[str, Any]:
        if run_id is None:
            raise RuntimeError(
                "workflow dispatch returned no stable run identity for authoritative readback"
            )
        value = await app.client.run(
            "run",
            "view",
            str(run_id),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "databaseId,url",
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object workflow run readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        return value.get("databaseId") == run_id and bool(value.get("url"))

    execution = await execute_write_readback(
        resource="Workflow dispatch",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    value = execution.readback_value or {}
    actual_id = value.get("databaseId")
    final_id = actual_id if isinstance(actual_id, int) else run_id
    final_url = value.get("url")
    message = status.warning or "Workflow dispatch triggered successfully."
    return WorkflowRunCreate(
        run_id=final_id,
        url=str(final_url) if isinstance(final_url, str) else created,
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
