"""0.6.x compatibility adapters for the shared exact-write contract.

These wrappers preserve existing public MCP input/output schemas while projecting
nontrivial writes through the shared internal outcome model. New 0.7.x write tools
must return ``ExactWriteResult``-derived schemas directly instead of adding new
compatibility adapters here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from .models import CommitFile, CommitFilesResult, PullRequestMerge, PullRequestReviewSubmission
from .request_governor import GitHubRequestResult
from .tooling import (
    OBJECT_SHA_RE,
    AppContext,
    api_json_write,
    app_from_context,
    require_write_enabled,
)
from .tools.repositories import gh_commit_files as _legacy_commit_files
from .write_contracts import (
    execute_write_readback,
    legacy_write_status,
    make_write_outcome,
    require_write_precondition,
)


def _review_state(action: Literal["approve", "request_changes", "comment"]) -> str:
    return {
        "approve": "APPROVED",
        "request_changes": "CHANGES_REQUESTED",
        "comment": "COMMENTED",
    }[action]


async def gh_submit_pr_review(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA that was reviewed.",
        ),
    ],
    action: Annotated[
        Literal["approve", "request_changes", "comment"],
        Field(description="Formal GitHub review disposition."),
    ],
    *,
    ctx: Context[AppContext],
    body: Annotated[
        str,
        Field(
            max_length=65_536,
            description=(
                "Review body. Required for request_changes and comment; optional for approve."
            ),
        ),
    ] = "",
) -> PullRequestReviewSubmission:
    """Submit an exact-head review while preserving the frozen 0.6.x result schema."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_review")
    if action in {"request_changes", "comment"} and not body.strip():
        raise ValueError(f"a non-empty review body is required for {action}")

    expected = expected_head_sha.lower()
    resource = "Pull request review"
    viewer_login: str | None = None
    if action == "approve":
        viewer = await app.client.run("api", "user", "-X", "GET")
        login = viewer.get("login") if isinstance(viewer, dict) else None
        viewer_login = login if isinstance(login, str) else None

    metadata_holder: dict[str, Any] = {}

    async def current_head() -> str:
        metadata = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/pulls/{number}",
            "-X",
            "GET",
        )
        if not isinstance(metadata, dict):
            raise RuntimeError("GitHub did not return pull-request metadata")
        head = metadata.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
            raise RuntimeError("GitHub did not return a valid pull-request head SHA")
        metadata_holder["value"] = metadata
        return head_sha.lower()

    async def precondition() -> Any:
        check = await require_write_precondition(
            current_head,
            expected,
            label="Pull request head",
        )
        if action == "approve":
            metadata = metadata_holder["value"]
            author = metadata.get("user")
            author_login = author.get("login") if isinstance(author, dict) else None
            if (
                viewer_login is not None
                and isinstance(author_login, str)
                and viewer_login.casefold() == author_login.casefold()
            ):
                raise ValueError(
                    f"authenticated GitHub account {viewer_login!r} is the pull request author "
                    "and cannot approve its own pull request; no review was attempted"
                )
        return check

    event = {
        "approve": "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }[action]
    review_id: int | None = None
    review_url = f"https://github.com/{owner}/{repo}/pull/{number}"

    async def write() -> GitHubRequestResult[dict[str, Any]]:
        nonlocal review_id, review_url
        created = await api_json_write(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/pulls/{number}/reviews",
            {"body": body, "event": event, "commit_id": expected},
        )
        raw_id = created.get("id")
        review_id = raw_id if isinstance(raw_id, int) else None
        review_url = str(created.get("html_url", review_url))
        # Existing 0.6.x callers use GhClient.run(), which intentionally returns only
        # the value. New 0.7.x exact writes must retain run_with_metadata() directly.
        return GitHubRequestResult(value=created)

    async def readback() -> dict[str, Any]:
        if review_id is None:
            raise RuntimeError("GitHub did not return the review id needed for readback")
        review = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}",
            "-X",
            "GET",
        )
        if not isinstance(review, dict):
            raise RuntimeError("GitHub returned a non-object review readback")
        return review

    def state_matches_requested(review: dict[str, Any]) -> bool:
        return (
            review_id is not None
            and review.get("id") == review_id
            and str(review.get("state", "")).upper() == _review_state(action)
            and str(review.get("commit_id", "")).lower() == expected
            and str(review.get("body", "")) == body
        )

    execution = await execute_write_readback(
        resource=resource,
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=state_matches_requested,
    )
    outcome = execution.outcome
    if (
        execution.error is not None
        and outcome.write_completed is False
        and outcome.state_matches_requested is not True
    ):
        raise execution.error

    status = legacy_write_status(outcome)
    review = execution.readback_value
    created = execution.write_value or {}
    if review is None:
        warning = status.warning or (
            "Pull request review readback is unavailable; re-read reviews before retrying."
        )
        return PullRequestReviewSubmission(
            number=number,
            review_id=review_id or 0,
            action=action,
            state=str(created.get("state", event)),
            body=str(created.get("body", body)),
            commit_sha=str(created.get("commit_id", expected)),
            url=review_url,
            write_completed=status.write_completed,
            readback_completed=status.readback_completed,
            warning=warning,
            message=warning,
        )

    user = review.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    if execution.error is not None or outcome.state_matches_requested is False:
        message = status.warning or (
            "Pull request review state was not verified; re-read before retrying."
        )
    else:
        message = f"Formal pull request review submitted with state {review.get('state', event)}."
    return PullRequestReviewSubmission(
        number=number,
        review_id=review_id or 0,
        action=action,
        state=str(review.get("state", event)),
        body=str(review.get("body", body)),
        author=author,
        submitted_at=review.get("submitted_at"),
        commit_sha=str(review.get("commit_id", expected)),
        url=str(review.get("html_url", review_url)),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )


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

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_merge")
    expected = expected_head_sha.lower()
    resource = "Pull request merge"

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
        value = await app.client.run(*args, json_output=False, stdin_text=body)
        # Existing 0.6.x callers use GhClient.run(), which intentionally returns only
        # the value. New 0.7.x exact writes must retain run_with_metadata() directly.
        return GitHubRequestResult(value=value)

    async def readback() -> dict[str, Any]:
        value = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            (
                "number,url,state,mergedAt,mergeCommit,headRefOid,"
                "mergeStateStatus,autoMergeRequest"
            ),
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object pull-request merge readback")
        return value

    def state_matches_requested(value: dict[str, Any]) -> bool:
        head_sha = value.get("headRefOid")
        state = str(value.get("state", "UNKNOWN"))
        merged_at = value.get("mergedAt")
        merge_state_status = value.get("mergeStateStatus")
        merged = state.upper() == "MERGED" or isinstance(merged_at, str)
        queued = (
            isinstance(merge_state_status, str) and merge_state_status.upper() == "QUEUED"
        )
        auto_merge_enabled = isinstance(value.get("autoMergeRequest"), dict)
        return (
            isinstance(head_sha, str)
            and head_sha.lower() == expected
            and (merged or queued or auto_merge_enabled)
        )

    execution = await execute_write_readback(
        resource=resource,
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=state_matches_requested,
    )
    outcome = execution.outcome
    if (
        execution.error is not None
        and outcome.write_completed is False
        and outcome.state_matches_requested is not True
    ):
        raise execution.error

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
    if execution.error is not None or outcome.state_matches_requested is False:
        message = status.warning or f"Pull request merge state is {state}; re-read before retrying."
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


async def gh_commit_files(
    owner: Annotated[
        str,
        Field(description="GitHub repository owner or organization login.", min_length=1),
    ],
    repo: Annotated[
        str,
        Field(description="GitHub repository name without the owner prefix.", min_length=1),
    ],
    branch: Annotated[
        str,
        Field(description="Existing branch to advance conditionally.", min_length=1),
    ],
    expected_head_sha: Annotated[
        str,
        Field(
            description="Exact 40-character branch head SHA required before the write.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    files: Annotated[
        list[CommitFile],
        Field(
            description="Complete UTF-8 file replacements to include in the atomic commit.",
            min_length=1,
            max_length=1000,
        ),
    ],
    commit_message: Annotated[
        str,
        Field(description="Git commit message.", min_length=1, max_length=65_536),
    ],
    *,
    ctx: Context[AppContext],
) -> CommitFilesResult:
    """Preserve the 0.6.x content-commit schema while standardizing outcome metadata."""

    result = await _legacy_commit_files(
        owner,
        repo,
        branch,
        expected_head_sha,
        files,
        commit_message,
        ctx=ctx,
    )

    if result.ref_updated is None:
        write_completed: bool | None = None
        readback_completed = False
        state_matches_requested: bool | None = None
    elif result.ref_updated is False:
        write_completed = result.write_completed
        # The legacy implementation reaches ref_updated=False only after an
        # authoritative ref readback proves the requested commit was not installed.
        readback_completed = True
        state_matches_requested = False
    elif result.readback_completed:
        write_completed = result.write_completed
        readback_completed = True
        state_matches_requested = True
    else:
        write_completed = result.write_completed
        readback_completed = False
        state_matches_requested = None

    outcome = make_write_outcome(
        resource="Repository content commit",
        precondition_checked=True,
        write_completed=write_completed,
        readback_completed=readback_completed,
        state_matches_requested=state_matches_requested,
        warning=result.warning,
    )
    status = legacy_write_status(outcome)
    message = result.message
    if status.warning is not None and status.warning != result.warning:
        message = status.warning
    return result.model_copy(
        update={
            "write_completed": status.write_completed,
            "readback_completed": status.readback_completed,
            "warning": status.warning,
            "message": message,
        }
    )
