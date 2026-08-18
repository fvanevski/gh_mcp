"""Action-specific formal pull-request review write implementations."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context

from ..gh_client import GhClient
from ..pr_write_models import PullRequestReviewSubmission
from ..request_governor import GitHubRequestResult
from ..reviewer_auth import ReviewerPrincipal
from ..tooling import (
    OBJECT_SHA_RE,
    AppContext,
    app_from_context,
    logger,
    require_write_enabled,
)
from ..write_contracts import (
    WriteOutcomeMetadata,
    execute_write_readback,
    require_write_precondition,
    run_api_json_write_with_metadata,
)

ReviewAction = Literal["approve", "request_changes", "comment"]

_REVIEW_EVENT: dict[ReviewAction, str] = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}
_REVIEW_STATE: dict[ReviewAction, str] = {
    "approve": "APPROVED",
    "request_changes": "CHANGES_REQUESTED",
    "comment": "COMMENTED",
}


def _outcome_message(
    outcome: WriteOutcomeMetadata,
    *,
    action: ReviewAction,
) -> str:
    if outcome.write_completed is True and outcome.state_matches_requested is True:
        return f"Formal review submitted and verified as {_REVIEW_STATE[action]}."
    return outcome.warning or "Pull request review was not verified."


async def _read_pr_metadata(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
) -> dict[str, Any]:
    value = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}",
        "-X",
        "GET",
    )
    if not isinstance(value, dict):
        raise RuntimeError("GitHub did not return pull-request metadata")
    return value


def _head_and_author(metadata: dict[str, Any]) -> tuple[str, str]:
    head = metadata.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub did not return a valid pull-request head SHA")
    user = metadata.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    if not isinstance(author, str) or not author:
        raise RuntimeError("GitHub did not return a valid pull-request author login")
    return head_sha.casefold(), author


async def _ordinary_login(app: AppContext) -> str:
    value = await app.client.run("api", "user", "-X", "GET")
    login = value.get("login") if isinstance(value, dict) else None
    if not isinstance(login, str) or not login:
        raise RuntimeError("GitHub did not return the ordinary authenticated login")
    return login


async def _submit_review(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    action: ReviewAction,
    *,
    ctx: Context[AppContext],
    body: str,
    expected_reviewer_login: str | None,
) -> PullRequestReviewSubmission:
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_review")
    if action in {"request_changes", "comment"} and not body.strip():
        raise ValueError(f"a non-empty review body is required for {action}")

    expected = expected_head_sha.casefold()
    if not OBJECT_SHA_RE.fullmatch(expected):
        raise ValueError("expected_head_sha must be an exact 40-character Git object id")

    reviewer_actions = {"approve", "request_changes"}
    if action in reviewer_actions and not expected_reviewer_login:
        raise ValueError(f"expected_reviewer_login is required for {action}")
    if action == "comment" and expected_reviewer_login is not None:
        raise ValueError("gh_comment_pr_review does not accept a reviewer identity")

    actor_login: str | None = None
    review_client: GhClient | None = None

    async def current_head() -> str:
        metadata = await _read_pr_metadata(app, owner, repo, number)
        current, _ = _head_and_author(metadata)
        return current

    async def precondition() -> Any:
        nonlocal actor_login, review_client
        metadata = await _read_pr_metadata(app, owner, repo, number)
        current, author = _head_and_author(metadata)
        check = await require_write_precondition(
            lambda: _return_value(current),
            expected,
            label="Pull request head",
        )

        if action == "comment":
            actor_login = await _ordinary_login(app)
            verified = await current_head()
            if verified != expected:
                raise RuntimeError(
                    f"Pull request head changed from expected {expected!r} to {verified!r}; "
                    "no review was attempted"
                )
            review_client = app.client
            return check

        principal = ReviewerPrincipal.from_settings(app.settings)
        if principal is None:
            raise RuntimeError(
                "reviewer_not_configured: an independent reviewer principal is required; "
                "no review was attempted"
            )

        assert expected_reviewer_login is not None
        advisory_identity = await principal.resolve_identity_read_only(owner, repo)
        if advisory_identity.login.casefold() != expected_reviewer_login.casefold():
            raise ValueError(
                f"configured reviewer login {advisory_identity.login!r} does not match "
                f"expected_reviewer_login {expected_reviewer_login!r}; no review was attempted"
            )

        if action == "approve" and author.casefold() == advisory_identity.login.casefold():
            raise ValueError(
                f"configured reviewer {advisory_identity.login!r} is the pull request author "
                "and cannot approve its own pull request; no review was attempted"
            )

        # For a GitHub App this mints one narrowly scoped installation token, then
        # proves the authenticated installation actor through GraphQL viewer.login.
        # The pull-request review POST has not occurred yet.
        review_client = await principal.client_for_review(
            owner,
            repo,
            expected_login=expected_reviewer_login,
        )
        actor_login = expected_reviewer_login

        # Re-read the PR after reviewer authentication so a force-push during token
        # minting/identity verification cannot race the actual formal review.
        verified = await current_head()
        if verified != expected:
            raise RuntimeError(
                f"Pull request head changed from expected {expected!r} to {verified!r}; "
                "no review was attempted"
            )
        return check

    review_id: int | None = None
    review_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    event = _REVIEW_EVENT[action]

    async def write() -> GitHubRequestResult[Any]:
        nonlocal review_id, review_url
        if review_client is None or actor_login is None:
            raise RuntimeError("review precondition did not establish an authenticated actor")
        result = await run_api_json_write_with_metadata(
            review_client,
            "POST",
            f"repos/{owner}/{repo}/pulls/{number}/reviews",
            {"body": body, "event": event, "commit_id": expected},
        )
        created = result.value
        if isinstance(created, dict):
            raw_id = created.get("id")
            review_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
            html_url = created.get("html_url")
            if isinstance(html_url, str) and html_url:
                review_url = html_url
        return result

    async def readback() -> dict[str, Any]:
        if review_id is None:
            raise RuntimeError(
                "GitHub did not return the immutable review id needed for authoritative readback"
            )
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
        user = review.get("user")
        readback_author = user.get("login") if isinstance(user, dict) else None
        return (
            review_id is not None
            and review.get("id") == review_id
            and str(review.get("state", "")).upper() == _REVIEW_STATE[action]
            and str(review.get("commit_id", "")).casefold() == expected
            and str(review.get("body", "")) == body
            and isinstance(readback_author, str)
            and actor_login is not None
            and readback_author.casefold() == actor_login.casefold()
        )

    execution = await execute_write_readback(
        resource=f"Pull request {_REVIEW_STATE[action]} review",
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
    submitted_at = review.get("submitted_at")
    return PullRequestReviewSubmission(
        number=number,
        review_id=review_id or 0,
        action=action,
        state=str(review.get("state") or created.get("state") or "UNKNOWN"),
        body=str(review.get("body") if "body" in review else created.get("body", body)),
        author=author if isinstance(author, str) else actor_login,
        submitted_at=submitted_at if isinstance(submitted_at, str) else None,
        commit_sha=str(review.get("commit_id") or created.get("commit_id") or expected),
        url=str(review.get("html_url") or created.get("html_url") or review_url),
        message=_outcome_message(outcome, action=action),
        **outcome.model_dump(),
    )


async def _return_value(value: str) -> str:
    return value


async def gh_approve_pr(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    expected_reviewer_login: str,
    *,
    ctx: Context[AppContext],
    body: str = "",
) -> PullRequestReviewSubmission:
    """Submit one exact-head APPROVED review through the reviewer-only principal."""

    logger.info("MCP tool invocation reached server: tool=gh_approve_pr")
    return await _submit_review(
        owner,
        repo,
        number,
        expected_head_sha,
        "approve",
        ctx=ctx,
        body=body,
        expected_reviewer_login=expected_reviewer_login,
    )


async def gh_request_pr_changes(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    expected_reviewer_login: str,
    *,
    ctx: Context[AppContext],
    body: str,
) -> PullRequestReviewSubmission:
    """Submit one exact-head CHANGES_REQUESTED review through the reviewer principal."""

    logger.info("MCP tool invocation reached server: tool=gh_request_pr_changes")
    return await _submit_review(
        owner,
        repo,
        number,
        expected_head_sha,
        "request_changes",
        ctx=ctx,
        body=body,
        expected_reviewer_login=expected_reviewer_login,
    )


async def gh_comment_pr_review(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    *,
    ctx: Context[AppContext],
    body: str,
) -> PullRequestReviewSubmission:
    """Submit one exact-head COMMENTED review through the ordinary principal."""

    logger.info("MCP tool invocation reached server: tool=gh_comment_pr_review")
    return await _submit_review(
        owner,
        repo,
        number,
        expected_head_sha,
        "comment",
        ctx=ctx,
        body=body,
        expected_reviewer_login=None,
    )
