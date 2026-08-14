"""Canonical pull-request write implementations."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context

from ..pr_write_models import (
    PullRequestCreate,
    PullRequestEdit,
    PullRequestMerge,
    PullRequestReviewSubmission,
)
from ..request_governor import GitHubRequestResult
from ..tooling import (
    OBJECT_SHA_RE,
    AppContext,
    app_from_context,
    logger,
    optional_created_url,
    require_write_enabled,
    trailing_number,
)
from ..write_contracts import (
    WriteOutcomeMetadata,
    execute_write_readback,
    require_write_precondition,
    run_api_json_write_with_metadata,
)


async def _resolve_assignee_groups(
    client: Any,
    *groups: list[str] | None,
) -> tuple[set[str], ...]:
    """Resolve @me so authoritative readback compares concrete GitHub logins."""

    self_login: str | None = None
    if any(group and "@me" in group for group in groups):
        account = await client.run("api", "user")
        login = account.get("login") if isinstance(account, dict) else None
        if not isinstance(login, str) or not login:
            raise RuntimeError("Unable to resolve @me to the authenticated GitHub login")
        self_login = login

    resolved: list[set[str]] = []
    for group in groups:
        names: set[str] = set()
        for value in group or []:
            names.add(self_login if value == "@me" and self_login is not None else value)
        resolved.append(names)
    return tuple(resolved)


def _names(items: Any, key: str) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {
        str(item.get(key))
        for item in items
        if isinstance(item, dict) and isinstance(item.get(key), str)
    }


def _outcome_message(
    outcome: WriteOutcomeMetadata,
    *,
    success: str,
    unverified: str,
) -> str:
    if outcome.write_completed is True and outcome.state_matches_requested is True:
        return success
    return outcome.warning or unverified


def _review_state(action: Literal["approve", "request_changes", "comment"]) -> str:
    return {
        "approve": "APPROVED",
        "request_changes": "CHANGES_REQUESTED",
        "comment": "COMMENTED",
    }[action]


async def gh_create_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    *,
    ctx: Context[AppContext],
    draft: bool = False,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    review_users: list[str] | None = None,
) -> PullRequestCreate:
    """Create one pull request and semantically verify its stable created resource."""

    logger.info("MCP tool invocation reached server: tool=gh_create_pr")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_create")
    (expected_assignees,) = await _resolve_assignee_groups(app.client, assignees)
    args = [
        "pr",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
        "--body-file",
        "-",
        "--head",
        head,
        "--base",
        base,
    ]
    if draft:
        args.append("--draft")
    for label in labels or []:
        args.extend(["--label", label])
    for assignee in assignees or []:
        args.extend(["--assignee", assignee])
    for user in review_users or []:
        args.extend(["--reviewer", user])

    created_url: str | None = None

    async def write() -> GitHubRequestResult[Any]:
        nonlocal created_url
        result = await app.client.run_with_metadata(
            *args,
            json_output=False,
            stdin_text=body,
        )
        created_url = optional_created_url(result.value)
        return result

    async def readback() -> dict[str, Any]:
        if created_url is None:
            raise RuntimeError("pull request creation returned no stable URL for readback")
        fields = (
            "title,number,url,body,headRefName,baseRefName,isDraft,labels,assignees,reviewRequests"
        )
        value = await app.client.run(
            "pr",
            "view",
            created_url,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            fields,
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object pull request readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        if value.get("title") != title or str(value.get("body") or "") != body:
            return False
        if value.get("headRefName") != head and value.get("headRefName") != head.split(":", 1)[-1]:
            return False
        if value.get("baseRefName") != base:
            return False
        if bool(value.get("isDraft", False)) is not draft:
            return False
        if labels and not set(labels).issubset(_names(value.get("labels"), "name")):
            return False
        if expected_assignees and not expected_assignees.issubset(
            _names(value.get("assignees"), "login")
        ):
            return False
        if review_users and not set(review_users).issubset(
            _names(value.get("reviewRequests"), "login")
        ):
            return False
        return isinstance(value.get("number"), int) and bool(value.get("url"))

    execution = await execute_write_readback(
        resource="Pull request creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    outcome = execution.outcome
    value = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    raw_number = value.get("number")
    pr_number = (
        raw_number
        if isinstance(raw_number, int)
        else trailing_number(created_url)
        if created_url is not None
        else 0
    )
    return PullRequestCreate(
        number=pr_number,
        title=str(value.get("title") or title),
        url=str(value.get("url") or created_url or ""),
        message=_outcome_message(
            outcome,
            success="Pull request created and verified successfully.",
            unverified="Pull request creation was not verified.",
        ),
        **outcome.model_dump(),
    )


async def gh_edit_pr(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
    title: str | None = None,
    body: str | None = None,
    labels_add: list[str] | None = None,
    labels_remove: list[str] | None = None,
    assignees_add: list[str] | None = None,
    assignees_remove: list[str] | None = None,
    base: str | None = None,
) -> PullRequestEdit:
    """Edit one pull request's metadata and semantically verify requested fields."""

    logger.info("MCP tool invocation reached server: tool=gh_edit_pr")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_edit")
    if not any(
        (
            title is not None,
            body is not None,
            labels_add,
            labels_remove,
            assignees_add,
            assignees_remove,
            base is not None,
        )
    ):
        raise ValueError("at least one pull request edit must be provided")
    if title == "":
        raise ValueError("pull request title cannot be empty")
    if base == "":
        raise ValueError("pull request base cannot be empty")

    expected_assignees_add, expected_assignees_remove = await _resolve_assignee_groups(
        app.client,
        assignees_add,
        assignees_remove,
    )

    args = ["pr", "edit", str(number), "--repo", f"{owner}/{repo}"]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body-file", "-"])
    for label in labels_add or []:
        args.extend(["--add-label", label])
    for label in labels_remove or []:
        args.extend(["--remove-label", label])
    for assignee in assignees_add or []:
        args.extend(["--add-assignee", assignee])
    for assignee in assignees_remove or []:
        args.extend(["--remove-assignee", assignee])
    if base is not None:
        args.extend(["--base", base])

    async def write() -> GitHubRequestResult[Any]:
        return await app.client.run_with_metadata(
            *args,
            json_output=False,
            stdin_text=body if body is not None else None,
        )

    async def readback() -> dict[str, Any]:
        fields = ["title", "url"]
        if body is not None:
            fields.append("body")
        if base is not None:
            fields.append("baseRefName")
        if labels_add or labels_remove:
            fields.append("labels")
        if assignees_add or assignees_remove:
            fields.append("assignees")
        value = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            ",".join(fields),
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object pull request edit readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        if title is not None and value.get("title") != title:
            return False
        if body is not None and str(value.get("body") or "") != body:
            return False
        if base is not None and value.get("baseRefName") != base:
            return False
        current_labels = _names(value.get("labels"), "name")
        if labels_add and not set(labels_add).issubset(current_labels):
            return False
        if labels_remove and set(labels_remove) & current_labels:
            return False
        current_assignees = _names(value.get("assignees"), "login")
        if expected_assignees_add and not expected_assignees_add.issubset(current_assignees):
            return False
        if expected_assignees_remove & current_assignees:
            return False
        return bool(value.get("url"))

    execution = await execute_write_readback(
        resource="Pull request edit",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    outcome = execution.outcome
    value = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    return PullRequestEdit(
        number=number,
        title=str(value.get("title") or title or ""),
        url=str(value.get("url") or ""),
        message=_outcome_message(
            outcome,
            success="Pull request edited and verified successfully.",
            unverified="Pull request edit was not verified.",
        ),
        **outcome.model_dump(),
    )


async def gh_submit_pr_review(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    action: Literal["approve", "request_changes", "comment"],
    *,
    ctx: Context[AppContext],
    body: str = "",
) -> PullRequestReviewSubmission:
    """Submit one formal review only for the exact pull-request head."""

    logger.info("MCP tool invocation reached server: tool=gh_submit_pr_review")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_review")
    if action in {"request_changes", "comment"} and not body.strip():
        raise ValueError(f"a non-empty review body is required for {action}")

    expected = expected_head_sha.lower()
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

    async def write() -> GitHubRequestResult[Any]:
        nonlocal review_id, review_url
        result = await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/pulls/{number}/reviews",
            {"body": body, "event": event, "commit_id": expected},
        )
        created = result.value
        if isinstance(created, dict):
            raw_id = created.get("id")
            review_id = raw_id if isinstance(raw_id, int) else None
            review_url = str(created.get("html_url", review_url))
        return result

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

    def matches(review: dict[str, Any]) -> bool:
        return (
            review_id is not None
            and review.get("id") == review_id
            and str(review.get("state", "")).upper() == _review_state(action)
            and str(review.get("commit_id", "")).lower() == expected
            and str(review.get("body", "")) == body
        )

    execution = await execute_write_readback(
        resource="Pull request review",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    outcome = execution.outcome
    review = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    created = execution.write_value if isinstance(execution.write_value, dict) else {}
    user = review.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    return PullRequestReviewSubmission(
        number=number,
        review_id=review_id or 0,
        action=action,
        state=str(review.get("state") or created.get("state") or event),
        body=str(review.get("body") if "body" in review else created.get("body", body)),
        author=author,
        submitted_at=review.get("submitted_at"),
        commit_sha=str(review.get("commit_id") or created.get("commit_id") or expected),
        url=str(review.get("html_url") or created.get("html_url") or review_url),
        message=_outcome_message(
            outcome,
            success=f"Formal review submitted and verified as {_review_state(action)}.",
            unverified="Pull request review was not verified.",
        ),
        **outcome.model_dump(),
    )


async def gh_merge_pr(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    method: Literal["merge", "squash", "rebase"],
    *,
    ctx: Context[AppContext],
    subject: str | None = None,
    body: str = "",
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
        return await app.client.run_with_metadata(
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
    outcome = execution.outcome
    pull_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    result = execution.readback_value if isinstance(execution.readback_value, dict) else {}

    merge_commit = result.get("mergeCommit")
    merge_commit_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    merged_at = result.get("mergedAt")
    state = str(result.get("state", "UNKNOWN"))
    merge_state_status = result.get("mergeStateStatus")
    merged = state.upper() == "MERGED" or isinstance(merged_at, str)
    queued = isinstance(merge_state_status, str) and merge_state_status.upper() == "QUEUED"
    auto_merge_enabled = isinstance(result.get("autoMergeRequest"), dict)

    if outcome.warning is not None:
        message = outcome.warning
    elif outcome.write_completed is True and outcome.state_matches_requested is True:
        if merged:
            message = "Pull request merged and verified successfully."
        elif queued or auto_merge_enabled:
            message = "Merge request was verified as queued or awaiting requirements."
        else:
            message = f"Merge request was verified; pull request state is {state}."
    else:
        message = "Pull request merge was not verified."

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
        message=message,
        **outcome.model_dump(),
    )
