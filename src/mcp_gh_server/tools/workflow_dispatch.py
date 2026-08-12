"""Exact-ref guarded GitHub Actions workflow dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..request_governor import GitHubRequestError, GitHubRequestResult
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
    combine_warnings,
    execute_write_readback,
    require_write_precondition,
    run_api_json_write_with_metadata,
)
from .actions import gh_list_runs
from .git import gh_get_ref


class WorkflowDispatchDuplicateError(RuntimeError):
    """A guarded workflow dispatch already exists for the exact workflow/head/event."""


class WorkflowDispatchRefAmbiguityError(RuntimeError):
    """A short dispatch name identifies both a branch and tag, so dispatch is unsafe."""


class WorkflowDispatchUncertainError(RuntimeError):
    """A prior local dispatch cannot yet be proven absent or safely retried."""


@dataclass(frozen=True, slots=True)
class _WorkflowDispatchRun:
    run_id: int
    workflow_id: int
    url: str
    status: str
    head_sha: str
    event: str


@dataclass(frozen=True, slots=True)
class _WorkflowDispatchReadback:
    matching_run_count: int
    run: _WorkflowDispatchRun | None


@dataclass(frozen=True, slots=True)
class _WorkflowDispatchReceipt:
    run_id: int
    api_url: str
    html_url: str


@dataclass(frozen=True, slots=True)
class _WorkflowDispatchReservation:
    run_id: int | None
    outcome_unknown: bool


_DispatchKey = tuple[str, str, int, str]
_WORKFLOW_DISPATCH_LOCK = asyncio.Lock()
_WORKFLOW_DISPATCH_RESERVATIONS: dict[_DispatchKey, _WorkflowDispatchReservation] = {}


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


def _dispatch_key(owner: str, repo: str, workflow_id: int, expected_ref_sha: str) -> _DispatchKey:
    """Return the server-local duplicate identity used while GitHub discovery may lag."""

    return owner.casefold(), repo.casefold(), workflow_id, expected_ref_sha.casefold()


def _counterpart_ref(ref: str) -> str:
    """Return the branch/tag ref that shares the requested short dispatch name."""

    namespace, separator, name = ref.partition("/")
    if not separator or namespace not in {"heads", "tags"} or not name:
        raise ValueError("ref must be one exact heads/<branch> or tags/<tag> path")
    counterpart = "tags" if namespace == "heads" else "heads"
    return f"{counterpart}/{name}"


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


async def _require_unambiguous_dispatch_name(
    owner: str,
    repo: str,
    ref: str,
    *,
    ctx: Context[AppContext],
) -> None:
    """Reject a short ref name that GitHub could resolve as either a branch or tag."""

    counterpart = _counterpart_ref(ref)
    result = await gh_get_ref(owner, repo, counterpart, ctx=ctx)
    if not result.found:
        return
    short_name = ref.split("/", 1)[1]
    raise WorkflowDispatchRefAmbiguityError(
        f"workflow dispatch name {short_name!r} exists as both refs/{ref} and "
        f"refs/{counterpart}; no write was attempted"
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


def _parse_dispatch_receipt(raw: object) -> _WorkflowDispatchReceipt:
    """Validate GitHub's return_run_details response without guessing a created run."""

    if not isinstance(raw, dict):
        raise RuntimeError("GitHub workflow dispatch returned a non-object run-detail response")
    run_id = raw.get("workflow_run_id")
    api_url = raw.get("run_url")
    html_url = raw.get("html_url")
    if not isinstance(run_id, int) or run_id < 1:
        raise RuntimeError("GitHub workflow dispatch returned no positive workflow run id")
    if not isinstance(api_url, str) or not api_url:
        raise RuntimeError("GitHub workflow dispatch returned no workflow run API URL")
    if not isinstance(html_url, str) or not html_url:
        raise RuntimeError("GitHub workflow dispatch returned no workflow run HTML URL")
    return _WorkflowDispatchReceipt(run_id=run_id, api_url=api_url, html_url=html_url)


def _parse_dispatch_run(
    raw: object,
    *,
    expected_run_id: int,
) -> _WorkflowDispatchRun:
    """Validate one exact workflow-run readback by the ID returned from dispatch."""

    if not isinstance(raw, dict):
        raise RuntimeError("GitHub returned a non-object workflow dispatch run readback")
    run_id = raw.get("id")
    workflow_id = raw.get("workflow_id")
    url = raw.get("html_url")
    status = raw.get("status")
    head_sha = raw.get("head_sha")
    event = raw.get("event")
    if run_id != expected_run_id:
        raise RuntimeError("GitHub workflow dispatch readback returned a different run id")
    if not isinstance(workflow_id, int) or workflow_id < 1:
        raise RuntimeError("GitHub returned no positive workflow id during dispatch readback")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub returned no workflow run URL during dispatch readback")
    if not isinstance(status, str) or not status:
        raise RuntimeError("GitHub returned no workflow run status during dispatch readback")
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError(
            "GitHub returned no exact workflow run head SHA during dispatch readback"
        )
    if not isinstance(event, str) or not event:
        raise RuntimeError("GitHub returned no workflow run event during dispatch readback")
    return _WorkflowDispatchRun(
        run_id=run_id,
        workflow_id=workflow_id,
        url=url,
        status=status,
        head_sha=head_sha.casefold(),
        event=event,
    )


async def _read_dispatch_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    ctx: Context[AppContext],
) -> _WorkflowDispatchRun:
    """Read exactly the workflow run identified by GitHub's dispatch response."""

    app = app_from_context(ctx)
    raw = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/runs/{run_id}",
        "-X",
        "GET",
    )
    return _parse_dispatch_run(raw, expected_run_id=run_id)


async def _read_matching_dispatch(
    owner: str,
    repo: str,
    workflow_id: int,
    expected_ref_sha: str,
    *,
    ctx: Context[AppContext],
) -> _WorkflowDispatchReadback:
    """Fallback exact-filter readback for writes that returned no run identity."""

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
        return _WorkflowDispatchReadback(matching_run_count=0, run=None)
    if runs.total_count > 1:
        return _WorkflowDispatchReadback(matching_run_count=runs.total_count, run=None)
    if len(runs.items) != 1:
        raise RuntimeError(
            "GitHub reported one exact workflow dispatch run but returned no unique readback item"
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
        raise RuntimeError(
            "GitHub returned no exact workflow run head SHA during dispatch readback"
        )
    if not isinstance(event, str) or not event:
        raise RuntimeError("GitHub returned no workflow run event during dispatch readback")

    return _WorkflowDispatchReadback(
        matching_run_count=1,
        run=_WorkflowDispatchRun(
            run_id=run_id,
            workflow_id=workflow_id,
            url=url,
            status=status,
            head_sha=head_sha.casefold(),
            event=event,
        ),
    )


async def _require_no_local_reservation(
    key: _DispatchKey,
    owner: str,
    repo: str,
    workflow_id: int,
    expected_ref_sha: str,
    *,
    ctx: Context[AppContext],
) -> None:
    """Reconcile a same-process dispatch reservation before allowing another write."""

    reservation = _WORKFLOW_DISPATCH_RESERVATIONS.get(key)
    if reservation is None:
        return

    if reservation.run_id is not None:
        try:
            run = await _read_dispatch_run(owner, repo, reservation.run_id, ctx=ctx)
        except GitHubRequestError as exc:
            if exc.status_code != 404:
                raise
            await _require_no_matching_dispatch(
                owner,
                repo,
                workflow_id,
                expected_ref_sha,
                ctx=ctx,
            )
            del _WORKFLOW_DISPATCH_RESERVATIONS[key]
            return

        if (
            run.workflow_id == workflow_id
            and run.head_sha == expected_ref_sha
            and run.event == "workflow_dispatch"
        ):
            raise WorkflowDispatchDuplicateError(
                "matching workflow_dispatch was already issued by this server for "
                f"workflow {workflow_id} at head {expected_ref_sha} (run {run.run_id}); "
                "no write was attempted"
            )
        raise WorkflowDispatchUncertainError(
            f"prior workflow dispatch run {run.run_id} no longer matches the exact requested "
            "workflow/head/event identity; no retry was attempted"
        )

    await _require_no_matching_dispatch(
        owner,
        repo,
        workflow_id,
        expected_ref_sha,
        ctx=ctx,
    )
    state = (
        "transport outcome is unknown"
        if reservation.outcome_unknown
        else "run identity is unknown"
    )
    raise WorkflowDispatchUncertainError(
        f"a prior local workflow dispatch for workflow {workflow_id} at head {expected_ref_sha} "
        f"has an unresolved {state}; re-read authoritative state before any retry"
    )


@mcp.tool(
    title="Dispatch workflow at exact ref",
    description=(
        "Destructive write: under one server-local critical section, verify an exact canonical "
        "branch/tag ref against expected_ref_sha, reject same-name branch/tag ambiguity, and "
        "reject any existing workflow_dispatch run for the workflow/head before one dispatch. "
        "GitHub's dispatch API has no atomic ref compare-and-swap, so returned run details are "
        "required and authoritative readback is bound to that exact run ID; any head mismatch "
        "is reported fail-closed and the tool never redispatches automatically."
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
    dispatch_receipt: _WorkflowDispatchReceipt | None = None
    receipt_warning: str | None = None
    key = _dispatch_key(owner, repo, workflow_id, normalized_expected_sha)

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
        await _require_unambiguous_dispatch_name(owner, repo, ref, ctx=ctx)
        return await require_write_precondition(
            read_ref_sha,
            normalized_expected_sha,
            label=f"workflow dispatch ref refs/{ref}",
        )

    async def write() -> GitHubRequestResult[Any]:
        nonlocal dispatch_receipt, receipt_warning
        payload: dict[str, Any] = {
            "ref": dispatch_ref,
            "return_run_details": True,
        }
        if inputs:
            payload["inputs"] = inputs
        result = await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            payload,
        )
        try:
            dispatch_receipt = _parse_dispatch_receipt(result.value)
        except RuntimeError as exc:
            receipt_warning = (
                "GitHub confirmed the workflow dispatch but did not return a valid "
                f"return_run_details identity: {exc}. The created run is not guessed from "
                "filtered discovery."
            )
        return result

    async def readback() -> _WorkflowDispatchReadback:
        if dispatch_receipt is not None:
            run = await _read_dispatch_run(owner, repo, dispatch_receipt.run_id, ctx=ctx)
            return _WorkflowDispatchReadback(matching_run_count=1, run=run)
        if receipt_warning is not None:
            raise RuntimeError(
                "successful workflow dispatch returned no authoritative run identity for readback"
            )
        return await _read_matching_dispatch(
            owner,
            repo,
            workflow_id,
            normalized_expected_sha,
            ctx=ctx,
        )

    async with _WORKFLOW_DISPATCH_LOCK:
        await _require_no_local_reservation(
            key,
            owner,
            repo,
            workflow_id,
            normalized_expected_sha,
            ctx=ctx,
        )
        execution = await execute_write_readback(
            resource=(f"Workflow {workflow_id} dispatch at refs/{ref} ({normalized_expected_sha})"),
            precondition=precondition,
            write=write,
            readback=readback,
            state_matches_requested=lambda readback_result: (
                readback_result.matching_run_count == 1
                and readback_result.run is not None
                and readback_result.run.workflow_id == workflow_id
                and readback_result.run.head_sha == normalized_expected_sha
                and readback_result.run.event == "workflow_dispatch"
            ),
        )
        if execution.outcome.write_completed is not False:
            _WORKFLOW_DISPATCH_RESERVATIONS[key] = _WorkflowDispatchReservation(
                run_id=dispatch_receipt.run_id if dispatch_receipt is not None else None,
                outcome_unknown=execution.outcome.write_completed is None,
            )

    readback_result = execution.readback_value
    run = readback_result.run if readback_result is not None else None
    outcome = execution.outcome
    warning = combine_warnings(outcome.warning, receipt_warning)
    if (
        run is not None
        and dispatch_receipt is not None
        and run.head_sha != normalized_expected_sha
    ):
        warning = combine_warnings(
            warning,
            "GitHub created the returned workflow run at a different head SHA than the exact-ref "
            "precondition. The ref may have moved between the final preflight read and GitHub's "
            "server-side dispatch resolution; no retry was attempted.",
        )

    return WorkflowDispatchExactResult(
        workflow_id=workflow_id,
        ref=ref,
        expected_ref_sha=normalized_expected_sha,
        resolved_ref_sha=resolved_ref_sha,
        matching_run_count=(
            readback_result.matching_run_count if readback_result is not None else None
        ),
        run_id=run.run_id if run is not None else None,
        run_url=run.url if run is not None else None,
        run_status=run.status if run is not None else None,
        run_head_sha=run.head_sha if run is not None else None,
        precondition_checked=outcome.precondition_checked,
        write_completed=outcome.write_completed,
        readback_completed=outcome.readback_completed,
        state_matches_requested=outcome.state_matches_requested,
        warning=warning,
        request_id=outcome.request_id,
    )
