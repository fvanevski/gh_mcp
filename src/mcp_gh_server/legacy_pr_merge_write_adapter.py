"""0.6.x compatibility adapter for exact-head pull-request merges."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import PullRequestMerge
from .request_governor import GitHubRequestResult
from .tooling import OBJECT_SHA_RE, AppContext, app_from_context, require_write_enabled
from .write_contracts import (
    execute_write_readback,
    legacy_write_status,
    require_write_precondition,
)

logger = logging.getLogger("mcp_gh_server.server")


async def gh_merge_pr(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA authorized for merge.",
        ),
    ],
    method: Annotated[
        Literal["merge", "squash", "rebase"],
        Field(description="Repository-supported merge strategy."),
    ],
    *,
    ctx: Context[AppContext],
    subject: Annotated[
        str | None,
        Field(max_length=256, description="Optional merge commit subject."),
    ] = None,
    body: Annotated[
        str,
        Field(max_length=65_536, description="Optional merge commit body."),
    ] = "",
) -> PullRequestMerge:
    """Merge one exact PR head through the shared precondition/readback executor."""

    logger.info("MCP tool invocation reached server: tool=gh_merge_pr")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_merge")
    expected = expected_head_sha.lower()

    async def current_head() -> str:
        metadata = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/pulls/{number}",
            "-X",
            "GET",
        )
        head = metadata.get("head") if isinstance(metadata, dict) else None
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
            raise RuntimeError("GitHub did not return a valid pull-request head SHA")
        return head_sha.lower()

    async def precondition() -> Any:
        return await require_write_precondition(
            current_head,
            expected,
            label="Pull request head",
        )

    args = [
        "pr",
        "merge",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        f"--{method}",
        "--match-head-commit",
        expected,
        "--body-file",
        "-",
    ]
    if subject is not None:
        args.extend(["--subject", subject])

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(
            app.client,
            *args,
            json_output=False,
            stdin_text=body,
        )

    async def readback() -> dict[str, Any]:
        value = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number,url,state,mergedAt,mergeCommit,headRefOid,mergeStateStatus,autoMergeRequest",
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object pull-request merge readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        head_sha = value.get("headRefOid")
        if not isinstance(head_sha, str) or head_sha.lower() != expected:
            return False
        state = str(value.get("state", "UNKNOWN")).upper()
        merged_at = value.get("mergedAt")
        if state == "MERGED" or isinstance(merged_at, str):
            return True
        merge_state_status = value.get("mergeStateStatus")
        if isinstance(merge_state_status, str) and merge_state_status.upper() == "QUEUED":
            # GitHub's merge queue controls the final merge strategy.
            return True
        auto_merge = value.get("autoMergeRequest")
        if not isinstance(auto_merge, dict):
            return False
        configured_method = auto_merge.get("mergeMethod")
        return (
            isinstance(configured_method, str) and configured_method.casefold() == method.casefold()
        )

    execution = await execute_write_readback(
        resource="Pull request merge",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    outcome = execution.outcome
    status = legacy_write_status(outcome)
    pull_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    result = execution.readback_value

    if result is None:
        warning = status.warning or (
            "Pull request merge readback is unavailable; re-read the pull request before retrying."
        )
        return PullRequestMerge(
            number=number,
            method=method,
            head_sha=expected,
            state="UNKNOWN",
            merged=False,
            url=pull_url,
            write_completed=status.write_completed,
            readback_completed=status.readback_completed,
            warning=warning,
            message=warning,
        )

    merge_commit = result.get("mergeCommit")
    merge_commit_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    merged_at = result.get("mergedAt")
    state = str(result.get("state", "UNKNOWN"))
    merge_state_status = result.get("mergeStateStatus")
    merged = state.upper() == "MERGED" or isinstance(merged_at, str)
    queued = isinstance(merge_state_status, str) and merge_state_status.upper() == "QUEUED"
    auto_merge_enabled = isinstance(result.get("autoMergeRequest"), dict)
    if status.warning is not None:
        message = status.warning
    elif merged:
        message = "Pull request merged successfully."
    elif queued or auto_merge_enabled:
        message = "Merge command completed; the pull request is queued or awaiting requirements."
    else:
        message = f"Merge command completed; pull request state is {state}."

    return PullRequestMerge(
        number=number,
        method=method,
        head_sha=str(result.get("headRefOid", expected)),
        state=state,
        merged=merged,
        merge_queued=queued,
        auto_merge_enabled=auto_merge_enabled,
        merged_at=merged_at,
        merge_commit_sha=merge_commit_sha,
        merge_state_status=merge_state_status,
        url=str(result.get("url", pull_url)),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
