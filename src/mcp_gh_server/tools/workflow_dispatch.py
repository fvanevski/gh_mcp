"""Exact-ref guarded GitHub Actions workflow dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..request_governor import GitHubRequestResult
from ..tooling import (
    MUTATE_EXTERNAL,
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    mcp,
    require_write_enabled,
)
from ..workflow_dispatch_models import WorkflowDispatchExactResult
from ..write_contracts import (
    WritePrecondition,
    execute_write_readback,
    require_write_precondition,
    run_api_json_write_with_metadata,
)
from .actions import gh_list_runs
from .git import gh_get_ref


class WorkflowDispatchDuplicateError(RuntimeError):
    """A guarded workflow dispatch already exists for the exact workflow/head/event."""


@dataclass(frozen=True, slots=True)
class _WorkflowDispatchRun:
    run_id: int
    url: str
    status: str
    head_sha: str
    event: str


def _workflow_inputs(fields: list[str] | None) -> dict[str, str]:
    """Convert the established key=value field surface into one deterministic JSON object."""

    inputs: dict[str, str] = {}
    for field in fields or []:
        if "=" not in field:
            raise ValueError("workflow fields must use non-empty key=value form")
        key, value = field.split("=", 1)
        if not key:
            raise ValueError("workflow fields must use non-empty key=value form")
        if key in inputs:
            raise ValueError(f"workflow field {key!r} was supplied more than once")
        inputs[key] = value
    return inputs


async def _read_exact_ref_commit_sha(
    owner: str,
    repo: str,
    ref: str,
    *,
    ctx: Context[AppContext],
) -> str:
    """Resolve one exact branch/tag ref to the commit GitHub will dispatch."""

    result = await gh_get_ref(owner, repo, ref, ctx=ctx)
    if not result.found:
        raise RuntimeError(f"exact workflow dispatch ref refs/{ref} does not exist")
    if result.object_type == "commit" and result.object_sha is not None:
        return result.object_sha.casefold()
    if result.object_type == "tag" and result.peeled_commit_sha is not None:
        return result.peeled_commit_sha.casefold()
    raise RuntimeError(
        f"exact workflow dispatch ref refs/{ref} does not resolve authoritatively to a commit"
    )


async def _require_no_matching_dispatch(
    owner: str,
    repo: str,
    workflow_id: int,
    expected_ref_sha: str,
    *,
    ctx: Context[AppContext],
) -> None:
    """Fail closed if any workflow_dispatch run already exists for the exact workflow/head."""

    runs = await gh_list_runs(
        owner,
        repo,
        ctx=ctx,
        workflow_id=workflow_id,
        head_sha=expected_ref_sha,
        event="workflow_dispatch",
        per_page=1,
        page=1,
    )
    if runs.total_count == 0:
        return

    detail = ""
    if runs.items and isinstance(runs.items[0], dict):
        item = runs.items[0]
        run_id = item.get("databaseId")
        status = item.get("status")
        if isinstance(run_id, int) and run_id > 0:
            detail = f" (run {run_id}"
            if isinstance(status, str) and status:
                detail += f", status {status}"
            detail += ")"
    raise WorkflowDispatchDuplicateError(
        "matching workflow_dispatch run already exists for "
        f"workflow {workflow_id} at head {expected_ref_sha}{detail}; no write was attempted"
    )


async def _read_matching_dispatch(
    owner: str,
    repo: str,
    workflow_id: int,
    expected_ref_sha: str,
    *,
    ctx: Context[AppContext],
) -> _WorkflowDispatchRun:
    """Read exactly one matching dispatch run or leave the write outcome unverified."""

    runs = await gh_list_runs(
        owner,
        repo,
        ctx=ctx,
        workflow_id=workflow_id,
        head_sha=expected_ref_sha,
        event="workflow_dispatch",
        per_page=2,
        page=1,
    )
    if runs.total_count == 0:
        raise RuntimeError(
            "workflow dispatch is not yet visible through exact workflow/head/event readback"
        )
    if runs.total_count != 1 or len(runs.items) != 1:
        raise RuntimeError(
            "workflow dispatch readback is ambiguous because multiple exact workflow/head/event "
            "runs are visible"
        )

    raw = runs.items[0]
    if not isinstance(raw, dict):
        raise RuntimeError("GitHub returned a malformed workflow dispatch readback item")
    run_id = raw.get("databaseId")
    url = raw.get("url")
    status = raw.get("status")
    head_sha = raw.get("headSha")
    event = raw.get("event")
    if not isinstance(run_id, int) or run_id < 1:
        raise RuntimeError("GitHub returned no positive workflow run id during dispatch readback")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub returned no workflow run URL during dispatch readback")
    if not isinstance(status, str) or not status:
        raise RuntimeError("GitHub returned no workflow run status during dispatch readback")
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub returned no exact workflow run head SHA during dispatch readback")
    if not isinstance(event, str) or not event:
        raise RuntimeError("GitHub returned no workflow run event during dispatch readback")

    return _WorkflowDispatchRun(
        run_id=run_id,
        url=url,
        status=status,
        head_sha=head_sha.casefold(),
        event=event,
    )


@mcp.tool(
    title="Dispatch workflow at exact ref",
    description=(
        "Destructive write: dispatch one workflow exactly once only when an exact canonical "
        "branch/tag ref still resolves to expected_ref_sha and no workflow_dispatch run already "
        "exists for that workflow/head. All existing run statuses are duplicate-relevant. The "
        "tool performs no automatic redispatch; authoritative exact-filter readback must bind "
        "the resulting run to the expected head SHA."
    ),
    annotations=MUTATE_EXTERNAL,
)
async def gh_run_workflow_exact(
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner or organization login.",
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
        ),
    ],
    repo: Annotated[
        str,
        Field(
            description="GitHub repository name without the owner prefix.",
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
        ),
    ],
    workflow_id: Annotated[int, Field(description="Exact positive workflow identifier.", ge=1)],
    ref: Annotated[
        str,
        Field(
            description=(
                "Exact Git ref path relative to refs/, formatted as heads/<branch> or tags/<tag>."
            ),
            min_length=6,
            max_length=1024,
            pattern=r"^(?:heads|tags)/.+$",
        ),
    ],
    expected_ref_sha: Annotated[
        str,
        Field(
            description="Exact 40-character commit SHA the ref must resolve to before dispatch.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    *,
    ctx: Context[AppContext],
    fields: list[str] | None = None,
) -> WorkflowDispatchExactResult:
    """Perform one exact-ref guarded workflow dispatch and verify exact run readback."""

    logger.info("MCP tool invocation reached server: tool=gh_run_workflow_exact")
    if workflow_id < 1:
        raise ValueError("workflow_id must be positive")
    if not OBJECT_SHA_RE.fullmatch(expected_ref_sha):
        raise ValueError("expected_ref_sha must be exactly 40 hexadecimal characters")

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="workflow_dispatch")
    inputs = _workflow_inputs(fields)
    normalized_expected_sha = expected_ref_sha.casefold()
    dispatch_ref = ref.split("/", 1)[1] if "/" in ref else ""
    if not dispatch_ref:
        raise ValueError("ref must be one exact heads/<branch> or tags/<tag> path")

    resolved_ref_sha = normalized_expected_sha

    async def read_ref_sha() -> str:
        nonlocal resolved_ref_sha
        resolved_ref_sha = await _read_exact_ref_commit_sha(owner, repo, ref, ctx=ctx)
        return resolved_ref_sha

    async def precondition() -> WritePrecondition[str]:
        await require_write_precondition(
            read_ref_sha,
            normalized_expected_sha,
            label=f"workflow dispatch ref refs/{ref}",
        )
        await _require_no_matching_dispatch(
            owner,
            repo,
            workflow_id,
            normalized_expected_sha,
            ctx=ctx,
        )
        return await require_write_precondition(
            read_ref_sha,
            normalized_expected_sha,
            label=f"workflow dispatch ref refs/{ref}",
        )

    async def write() -> GitHubRequestResult[Any]:
        payload: dict[str, Any] = {"ref": dispatch_ref}
        if inputs:
            payload["inputs"] = inputs
        return await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            payload,
        )

    async def readback() -> _WorkflowDispatchRun:
        return await _read_matching_dispatch(
            owner,
            repo,
            workflow_id,
            normalized_expected_sha,
            ctx=ctx,
        )

    execution = await execute_write_readback(
        resource=(
            f"Workflow {workflow_id} dispatch at refs/{ref} ({normalized_expected_sha})"
        ),
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=lambda run: (
            run.head_sha == normalized_expected_sha and run.event == "workflow_dispatch"
        ),
    )

    run = execution.readback_value
    outcome = execution.outcome
    return WorkflowDispatchExactResult(
        workflow_id=workflow_id,
        ref=ref,
        expected_ref_sha=normalized_expected_sha,
        resolved_ref_sha=resolved_ref_sha,
        run_id=run.run_id if run is not None else None,
        run_url=run.url if run is not None else None,
        run_status=run.status if run is not None else None,
        run_head_sha=run.head_sha if run is not None else None,
        precondition_checked=outcome.precondition_checked,
        write_completed=outcome.write_completed,
        readback_completed=outcome.readback_completed,
        state_matches_requested=outcome.state_matches_requested,
        warning=outcome.warning,
        request_id=outcome.request_id,
    )
