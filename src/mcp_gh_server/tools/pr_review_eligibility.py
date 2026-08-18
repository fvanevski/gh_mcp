"""Read-only exact-head eligibility preflight for formal pull-request reviews."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from ..pr_review_eligibility_models import PullRequestReviewEligibility
from ..reviewer_auth import ReviewerPrincipal
from ..tooling import (
    OBJECT_SHA_RE,
    OWNER_RE,
    READ_EXTERNAL,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    mcp,
    require_write_enabled,
    validate_repository,
)


async def _pull_request_identity(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
) -> tuple[str, str]:
    value = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}",
        "-X",
        "GET",
    )
    if not isinstance(value, dict):
        raise RuntimeError("GitHub did not return pull-request metadata")
    head = value.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub did not return a valid pull-request head SHA")
    user = value.get("user")
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


def _policy_warning(app: AppContext, owner: str, repo: str) -> str | None:
    try:
        require_write_enabled(app, owner, repo, action="pr_review")
    except RuntimeError as exc:
        return str(exc)
    return None


@mcp.tool(
    title="Get pull request review eligibility",
    description=(
        "Read-only exact-head preflight: report the pull-request author, ordinary GitHub "
        "identity, configured reviewer identity, and whether an independent APPROVED review "
        "or ordinary COMMENTED review is currently eligible. This advisory call performs no "
        "review write and never mints a reviewer installation token."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_pr_review_eligibility(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    number: Annotated[int, Field(ge=1, description="Positive pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA whose review eligibility is requested.",
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> PullRequestReviewEligibility:
    """Return fail-closed review eligibility for one exact PR revision."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr_review_eligibility")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    expected = expected_head_sha.casefold()

    current_head, author = await _pull_request_identity(app, owner, repo, number)
    if current_head != expected:
        return PullRequestReviewEligibility(
            number=number,
            expected_head_sha=expected,
            current_head_sha=current_head,
            head_matches_expected=False,
            pr_author_login=author,
            approval_eligible=False,
            comment_review_available=False,
            reason="head_mismatch",
            warning=(
                f"Expected head {expected} does not match current head {current_head}; "
                "review eligibility was not evaluated."
            ),
        )

    policy_warning = _policy_warning(app, owner, repo)
    try:
        ordinary = await _ordinary_login(app)
    except RuntimeError as exc:
        return PullRequestReviewEligibility(
            number=number,
            expected_head_sha=expected,
            current_head_sha=current_head,
            head_matches_expected=True,
            pr_author_login=author,
            approval_eligible=False,
            comment_review_available=False,
            reason="ordinary_identity_unavailable",
            warning=str(exc),
        )

    principal = ReviewerPrincipal.from_settings(app.settings)
    reviewer_login: str | None = None
    reviewer_kind: Literal["github_app", "static_token"] | None = None
    reviewer_warning: str | None = None
    if principal is not None:
        try:
            reviewer = await principal.resolve_identity_read_only(owner, repo)
        except RuntimeError as exc:
            reviewer_warning = str(exc)
        else:
            reviewer_login = reviewer.login
            reviewer_kind = reviewer.kind

    verified_head, verified_author = await _pull_request_identity(app, owner, repo, number)
    if verified_head != expected or author.casefold() != verified_author.casefold():
        return PullRequestReviewEligibility(
            number=number,
            expected_head_sha=expected,
            current_head_sha=verified_head,
            head_matches_expected=False,
            pr_author_login=verified_author,
            ordinary_login=ordinary,
            reviewer_login=reviewer_login,
            reviewer_kind=reviewer_kind,
            approval_eligible=False,
            comment_review_available=False,
            reason="head_mismatch",
            warning=(
                "Pull request head or author changed during eligibility evaluation; "
                "all eligibility conclusions were discarded."
            ),
        )

    comment_available = policy_warning is None
    if policy_warning is not None:
        return PullRequestReviewEligibility(
            number=number,
            expected_head_sha=expected,
            current_head_sha=verified_head,
            head_matches_expected=True,
            pr_author_login=verified_author,
            ordinary_login=ordinary,
            reviewer_login=reviewer_login,
            reviewer_kind=reviewer_kind,
            approval_eligible=False,
            comment_review_available=False,
            reason="write_policy_denied",
            warning=policy_warning,
        )

    if principal is None:
        return PullRequestReviewEligibility(
            number=number,
            expected_head_sha=expected,
            current_head_sha=verified_head,
            head_matches_expected=True,
            pr_author_login=verified_author,
            ordinary_login=ordinary,
            approval_eligible=False,
            comment_review_available=comment_available,
            reason="reviewer_not_configured",
        )

    if reviewer_login is None:
        return PullRequestReviewEligibility(
            number=number,
            expected_head_sha=expected,
            current_head_sha=verified_head,
            head_matches_expected=True,
            pr_author_login=verified_author,
            ordinary_login=ordinary,
            reviewer_kind=reviewer_kind,
            approval_eligible=False,
            comment_review_available=comment_available,
            reason="reviewer_identity_unavailable",
            warning=reviewer_warning,
        )

    if reviewer_login.casefold() == verified_author.casefold():
        return PullRequestReviewEligibility(
            number=number,
            expected_head_sha=expected,
            current_head_sha=verified_head,
            head_matches_expected=True,
            pr_author_login=verified_author,
            ordinary_login=ordinary,
            reviewer_login=reviewer_login,
            reviewer_kind=reviewer_kind,
            approval_eligible=False,
            comment_review_available=comment_available,
            reason="reviewer_is_pr_author",
        )

    return PullRequestReviewEligibility(
        number=number,
        expected_head_sha=expected,
        current_head_sha=verified_head,
        head_matches_expected=True,
        pr_author_login=verified_author,
        ordinary_login=ordinary,
        reviewer_login=reviewer_login,
        reviewer_kind=reviewer_kind,
        approval_eligible=True,
        comment_review_available=comment_available,
        reason="eligible",
        warning=(
            "Eligibility is advisory; gh_approve_pr independently verifies the exact head "
            "and authenticated reviewer login immediately before the review POST."
        ),
    )
