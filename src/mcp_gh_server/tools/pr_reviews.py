"""Read-only pull-request review and exact-head review-state tools."""

from __future__ import annotations

from typing import Annotated, Any, cast

from mcp.server.mcpserver import Context
from pydantic import Field

from ..evidence import PaginationEvidence, pagination_evidence
from ..pr_review_models import (
    PullRequestReview,
    PullRequestReviewsPage,
    PullRequestReviewState,
    PullRequestReviewThread,
    ReviewDecision,
)
from ..tooling import (
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repository,
)
from .pull_requests import _extract_pr_shas, _get_pr_metadata

_GITHUB_REVIEWS_PER_PAGE_MAX = 100
_GITHUB_THREADS_PER_PAGE_MAX = 100
_REVIEW_DECISIONS = {"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
_REVIEW_THREADS_QUERY = """
query PullRequestReviewState(
  $owner: String!
  $repo: String!
  $number: Int!
  $first: Int!
) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewDecision
      reviewThreads(first: $first) {
        totalCount
        pageInfo {
          hasNextPage
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          comments(first: 1) {
            totalCount
          }
        }
      }
    }
  }
}
""".strip()


def _review_from_payload(payload: Any) -> PullRequestReview:
    """Validate one REST review without dropping its commit provenance."""

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned a malformed pull-request review")

    review_id = payload.get("id")
    if not isinstance(review_id, int) or isinstance(review_id, bool) or review_id < 1:
        raise RuntimeError("GitHub returned a review without a valid id")

    state = payload.get("state")
    if not isinstance(state, str) or not state:
        raise RuntimeError("GitHub returned a review without a valid state")

    commit_id = payload.get("commit_id")
    if not isinstance(commit_id, str) or not OBJECT_SHA_RE.fullmatch(commit_id):
        raise RuntimeError("GitHub returned a review without a valid commit id")

    user = payload.get("user")
    if user is not None and not isinstance(user, dict):
        raise RuntimeError("GitHub returned a review with malformed reviewer identity")
    reviewer = user.get("login") if isinstance(user, dict) else None
    if reviewer is not None and not isinstance(reviewer, str):
        raise RuntimeError("GitHub returned a review with malformed reviewer login")

    submitted_at = payload.get("submitted_at")
    if submitted_at is not None and not isinstance(submitted_at, str):
        raise RuntimeError("GitHub returned a review with malformed submitted timestamp")

    body = payload.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise RuntimeError("GitHub returned a review with malformed body")

    author_association = payload.get("author_association")
    if author_association is not None and not isinstance(author_association, str):
        raise RuntimeError("GitHub returned a review with malformed author association")

    return PullRequestReview(
        id=review_id,
        reviewer=reviewer,
        state=state.upper(),
        commit_id=commit_id.casefold(),
        submitted_at=submitted_at,
        body=body,
        author_association=author_association,
    )


async def _read_reviews_page(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
    *,
    page: int,
    per_page: int,
) -> list[PullRequestReview]:
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}/reviews",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={per_page}",
    )
    if not isinstance(result, list):
        raise RuntimeError("GitHub did not return a pull-request review list")
    if len(result) > per_page:
        raise RuntimeError("GitHub returned more reviews than the requested page bound")
    return [_review_from_payload(item) for item in result]


async def _page_has_more_reviews(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
    *,
    page: int,
    per_page: int,
    returned_count: int,
) -> bool:
    if returned_count < per_page:
        return False
    following = await _read_reviews_page(
        app,
        owner,
        repo,
        number,
        page=page + 1,
        per_page=per_page,
    )
    return bool(following)


def _aggregate_pagination(
    *,
    returned_count: int,
    limit: int,
    has_more: bool,
    total_count: int | None = None,
    label: str,
) -> PaginationEvidence:
    warning = None
    if has_more:
        if total_count is None:
            warning = f"{label} exceeds the aggregate limit of {limit}; additional evidence exists."
        else:
            warning = (
                f"{label} was truncated: returned {returned_count} of {total_count} records "
                f"under the aggregate limit of {limit}."
            )
    return PaginationEvidence(
        total_count=total_count,
        page=1,
        per_page=limit,
        returned_count=returned_count,
        has_more=has_more,
        truncated=has_more,
        warning=warning,
    )


def _thread_from_payload(payload: Any) -> PullRequestReviewThread | None:
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned a malformed pull-request review thread")

    thread_id = payload.get("id")
    path = payload.get("path")
    is_resolved = payload.get("isResolved")
    is_outdated = payload.get("isOutdated")
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("GitHub returned a review thread without a valid id")
    if not isinstance(path, str) or not path:
        raise RuntimeError("GitHub returned a review thread without a valid path")
    if not isinstance(is_resolved, bool) or not isinstance(is_outdated, bool):
        raise RuntimeError("GitHub returned a review thread with malformed state")

    line = payload.get("line")
    original_line = payload.get("originalLine")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
        raise RuntimeError("GitHub returned a review thread with malformed line")
    if original_line is not None and (
        not isinstance(original_line, int) or isinstance(original_line, bool)
    ):
        raise RuntimeError("GitHub returned a review thread with malformed original line")

    comments = payload.get("comments")
    if not isinstance(comments, dict):
        raise RuntimeError("GitHub returned a review thread without comment completeness")
    comment_count = comments.get("totalCount")
    if not isinstance(comment_count, int) or isinstance(comment_count, bool) or comment_count < 0:
        raise RuntimeError("GitHub returned a review thread with invalid comment count")

    if is_resolved:
        return None
    return PullRequestReviewThread(
        id=thread_id,
        path=path,
        line=line,
        original_line=original_line,
        is_outdated=is_outdated,
        comment_count=comment_count,
    )


async def _read_thread_state(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
    *,
    limit: int,
) -> tuple[list[PullRequestReviewThread], PaginationEvidence, ReviewDecision | None]:
    result = await app.client.run(
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"repo={repo}",
        "-F",
        f"number={number}",
        "-F",
        f"first={limit}",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return structured GraphQL review-thread evidence")
    errors = result.get("errors")
    if errors:
        raise RuntimeError("GitHub GraphQL returned errors while reading review-thread evidence")

    data = result.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(pull_request, dict):
        raise RuntimeError("GitHub GraphQL did not return the requested pull request")

    raw_decision = pull_request.get("reviewDecision")
    if raw_decision is not None and (
        not isinstance(raw_decision, str) or raw_decision not in _REVIEW_DECISIONS
    ):
        raise RuntimeError("GitHub returned an unknown pull-request review decision")
    decision = cast(ReviewDecision | None, raw_decision)

    connection = pull_request.get("reviewThreads")
    if not isinstance(connection, dict):
        raise RuntimeError("GitHub did not return a review-thread connection")
    total_count = connection.get("totalCount")
    nodes = connection.get("nodes")
    page_info = connection.get("pageInfo")
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < 0:
        raise RuntimeError("GitHub returned an invalid review-thread total count")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise RuntimeError("GitHub returned malformed review-thread pagination evidence")
    if len(nodes) > limit:
        raise RuntimeError("GitHub returned more review threads than the requested bound")

    has_next_page = page_info.get("hasNextPage")
    if not isinstance(has_next_page, bool):
        raise RuntimeError("GitHub returned malformed review-thread page information")
    authoritative_has_more = total_count > len(nodes)
    if has_next_page != authoritative_has_more:
        raise RuntimeError("GitHub review-thread pageInfo conflicts with totalCount")

    unresolved: list[PullRequestReviewThread] = []
    for node in nodes:
        thread = _thread_from_payload(node)
        if thread is not None:
            unresolved.append(thread)

    evidence = _aggregate_pagination(
        returned_count=len(nodes),
        limit=limit,
        has_more=authoritative_has_more,
        total_count=total_count,
        label="Review-thread evidence",
    )
    return unresolved, evidence, decision


async def _read_requested_reviewers(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
) -> tuple[list[str], list[str]]:
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}/requested_reviewers",
        "-X",
        "GET",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return structured requested-reviewer evidence")
    users = result.get("users")
    teams = result.get("teams")
    if not isinstance(users, list) or not isinstance(teams, list):
        raise RuntimeError("GitHub returned malformed requested-reviewer evidence")

    reviewer_logins: list[str] = []
    for user in users:
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or not login:
            raise RuntimeError("GitHub returned a requested reviewer without a valid login")
        reviewer_logins.append(login)

    team_slugs: list[str] = []
    for team in teams:
        slug = team.get("slug") if isinstance(team, dict) else None
        if not isinstance(slug, str) or not slug:
            raise RuntimeError("GitHub returned a requested team without a valid slug")
        team_slugs.append(slug)
    return reviewer_logins, team_slugs


def _invalid_exact_head_state(
    *,
    number: int,
    base_sha: str,
    expected_head_sha: str,
    current_head_sha: str,
    reason: str,
) -> PullRequestReviewState:
    return PullRequestReviewState(
        number=number,
        base_sha=base_sha.casefold(),
        expected_head_sha=expected_head_sha.casefold(),
        current_head_sha=current_head_sha.casefold(),
        head_matches_expected=current_head_sha.casefold() == expected_head_sha.casefold(),
        exact_head_evidence=False,
        requirements_satisfied=None,
        requirements_reason=reason,
        warning=reason,
    )


def _requirements_result(
    *,
    exact_head_evidence: bool,
    review_decision: ReviewDecision | None,
    current_approvals: list[PullRequestReview],
    requested_reviewers: list[str],
    requested_teams: list[str],
    unresolved_threads: list[PullRequestReviewThread],
) -> tuple[bool | None, str]:
    if not exact_head_evidence:
        return (
            None,
            "Exact-head review evidence is incomplete; review requirements cannot be determined.",
        )
    if review_decision == "CHANGES_REQUESTED":
        return False, "GitHub reports CHANGES_REQUESTED for the current pull request."
    if review_decision == "REVIEW_REQUIRED":
        return False, "GitHub reports REVIEW_REQUIRED for the current pull request."
    if review_decision != "APPROVED":
        return None, "GitHub did not report a review decision that establishes satisfaction."

    unresolved_visible_state = any((requested_reviewers, requested_teams, unresolved_threads))
    if not current_approvals or unresolved_visible_state:
        return (
            None,
            "GitHub reports APPROVED, but visible review evidence is insufficient for a "
            "conservative exact-head all-requirements conclusion.",
        )
    return (
        True,
        "GitHub reports APPROVED and complete exact-head review evidence contains a "
        "current-head approval with no outstanding requested reviewer or unresolved thread.",
    )


@mcp.tool(
    title="List pull request reviews",
    description=(
        "Read-only: return one bounded page of typed pull-request reviews with exact commit "
        "provenance and explicit pagination completeness for an unchanged PR head snapshot."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_pr_reviews(
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
    number: Annotated[int, Field(ge=1, description="Positive pull request number.")],
    *,
    ctx: Context[AppContext],
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based review page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Reviews per page, capped by server policy."),
    ] = None,
) -> PullRequestReviewsPage:
    """Return one review page while rejecting a PR head change during the read."""

    logger.info("MCP tool invocation reached server: tool=gh_list_pr_reviews")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    base_sha, head_sha = _extract_pr_shas(await _get_pr_metadata(app, owner, repo, number))
    requested = app.settings.default_max_results if per_page is None else per_page
    hard_limit = min(app.settings.hard_max_results, _GITHUB_REVIEWS_PER_PAGE_MAX)
    limit = min(requested, hard_limit)

    reviews = await _read_reviews_page(
        app,
        owner,
        repo,
        number,
        page=page,
        per_page=limit,
    )
    has_more = await _page_has_more_reviews(
        app,
        owner,
        repo,
        number,
        page=page,
        per_page=limit,
        returned_count=len(reviews),
    )
    verified = _extract_pr_shas(await _get_pr_metadata(app, owner, repo, number))
    if verified != (base_sha, head_sha):
        raise RuntimeError(
            "Pull request base or head changed during the review read; retry from a fresh snapshot"
        )

    evidence = pagination_evidence(
        page=page,
        requested_per_page=per_page,
        default_per_page=app.settings.default_max_results,
        hard_max_results=hard_limit,
        returned_count=len(reviews),
        has_more=has_more,
    )
    return PullRequestReviewsPage(
        number=number,
        base_sha=base_sha.casefold(),
        head_sha=head_sha.casefold(),
        page=evidence.page,
        per_page=evidence.per_page,
        returned_count=evidence.returned_count,
        has_more=evidence.has_more,
        truncated=evidence.truncated,
        warning=evidence.warning,
        reviews=reviews,
    )


@mcp.tool(
    title="Get exact-head pull request review state",
    description=(
        "Read-only: aggregate bounded review, requested-reviewer, and unresolved-thread "
        "evidence only for an exact expected PR head. Head mismatch or partial evidence "
        "prevents a definitive satisfied result."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_pr_review_state(
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
    number: Annotated[int, Field(ge=1, description="Positive pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA whose review state is requested.",
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> PullRequestReviewState:
    """Return conservative review-state evidence bound to one expected head SHA."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr_review_state")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    expected = expected_head_sha.casefold()
    if not OBJECT_SHA_RE.fullmatch(expected):
        raise ValueError("expected_head_sha must be an exact 40-character Git object id")

    initial_metadata = await _get_pr_metadata(app, owner, repo, number)
    initial_base, initial_head = _extract_pr_shas(initial_metadata)
    if initial_head.casefold() != expected:
        return _invalid_exact_head_state(
            number=number,
            base_sha=initial_base,
            expected_head_sha=expected,
            current_head_sha=initial_head,
            reason=(
                f"Expected head {expected} does not match current head {initial_head.casefold()}; "
                "exact-head review state was not evaluated."
            ),
        )

    limit = min(app.settings.hard_max_results, _GITHUB_REVIEWS_PER_PAGE_MAX)
    reviews = await _read_reviews_page(
        app,
        owner,
        repo,
        number,
        page=1,
        per_page=limit,
    )
    reviews_have_more = await _page_has_more_reviews(
        app,
        owner,
        repo,
        number,
        page=1,
        per_page=limit,
        returned_count=len(reviews),
    )
    reviews_evidence = _aggregate_pagination(
        returned_count=len(reviews),
        limit=limit,
        has_more=reviews_have_more,
        label="Review evidence",
    )

    requested_reviewers, requested_teams = await _read_requested_reviewers(app, owner, repo, number)
    thread_limit = min(app.settings.hard_max_results, _GITHUB_THREADS_PER_PAGE_MAX)
    unresolved_threads, threads_evidence, review_decision = await _read_thread_state(
        app,
        owner,
        repo,
        number,
        limit=thread_limit,
    )

    final_metadata = await _get_pr_metadata(app, owner, repo, number)
    final_base, final_head = _extract_pr_shas(final_metadata)
    if (final_base.casefold(), final_head.casefold()) != (
        initial_base.casefold(),
        initial_head.casefold(),
    ):
        return _invalid_exact_head_state(
            number=number,
            base_sha=final_base,
            expected_head_sha=expected,
            current_head_sha=final_head,
            reason=(
                "Pull request base or head changed during the review-state read; collected "
                "evidence was discarded and must not be interpreted as exact-head state."
            ),
        )

    current_approvals = [
        review for review in reviews if review.commit_id == expected and review.state == "APPROVED"
    ]
    current_change_requests = [
        review
        for review in reviews
        if review.commit_id == expected and review.state == "CHANGES_REQUESTED"
    ]
    current_comments = [
        review for review in reviews if review.commit_id == expected and review.state == "COMMENTED"
    ]
    stale_approvals = [
        review for review in reviews if review.commit_id != expected and review.state == "APPROVED"
    ]
    stale_change_requests = [
        review
        for review in reviews
        if review.commit_id != expected and review.state == "CHANGES_REQUESTED"
    ]

    exact_head_evidence = not reviews_evidence.truncated and not threads_evidence.truncated
    requirements_satisfied, requirements_reason = _requirements_result(
        exact_head_evidence=exact_head_evidence,
        review_decision=review_decision,
        current_approvals=current_approvals,
        requested_reviewers=requested_reviewers,
        requested_teams=requested_teams,
        unresolved_threads=unresolved_threads,
    )
    warnings = [
        warning
        for warning in (reviews_evidence.warning, threads_evidence.warning)
        if warning is not None
    ]
    return PullRequestReviewState(
        number=number,
        base_sha=initial_base.casefold(),
        expected_head_sha=expected,
        current_head_sha=initial_head.casefold(),
        head_matches_expected=True,
        exact_head_evidence=exact_head_evidence,
        reviews_evidence=reviews_evidence,
        review_threads_evidence=threads_evidence,
        current_head_approvals=current_approvals,
        current_head_change_requests=current_change_requests,
        current_head_comments=current_comments,
        stale_approvals=stale_approvals,
        stale_change_requests=stale_change_requests,
        requested_reviewers=requested_reviewers,
        requested_teams=requested_teams,
        unresolved_review_threads=unresolved_threads,
        review_decision=review_decision,
        requirements_satisfied=requirements_satisfied,
        requirements_reason=requirements_reason,
        warning=" ".join(warnings) or None,
    )
