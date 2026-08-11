"""0.6.x compatibility adapter for exact-head pull-request reviews."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from .legacy_write_support import raise_known_unapplied, run_json_write_with_metadata
from .models import PullRequestReviewSubmission
from .request_governor import GitHubRequestResult
from .tooling import OBJECT_SHA_RE, AppContext, app_from_context, require_write_enabled
from .write_contracts import (
    execute_write_readback,
    legacy_write_status,
    require_write_precondition,
)

logger = logging.getLogger("mcp_gh_server.server")


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
        result = await run_json_write_with_metadata(
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
    raise_known_unapplied(execution)
    outcome = execution.outcome
    status = legacy_write_status(outcome)
    review = execution.readback_value
    created = execution.write_value if isinstance(execution.write_value, dict) else {}

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
    message = (
        status.warning
        or f"Formal pull request review submitted with state {review.get('state', event)}."
    )
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
