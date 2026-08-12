"""Read-only exact-head pull-request merge-requirement aggregation."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..merge_requirements_models import MergeMethod, PullRequestMergeRequirements
from ..merge_requirements_policy import (
    MergePolicy,
    read_effective_merge_policy,
    read_repository_merge_methods,
)
from ..models import PullRequestCheck
from ..pr_review_models import PullRequestReview, PullRequestReviewState
from ..request_governor import GitHubRequestError
from ..tooling import (
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repository,
)
from .pr_reviews import gh_get_pr_review_state
from .pull_requests import _extract_pr_shas, _get_pr_metadata, gh_get_pr_checks

_MERGE_METHOD_ORDER: tuple[MergeMethod, ...] = ("merge", "squash", "rebase")


def _pr_snapshot(metadata: dict[str, Any]) -> tuple[str, str, str, bool | None, str | None]:
    """Validate the PR identity and mergeability fields used by the aggregate."""

    base_sha, head_sha = _extract_pr_shas(metadata)
    base = metadata.get("base")
    base_ref = base.get("ref") if isinstance(base, dict) else None
    if not isinstance(base_ref, str) or not base_ref:
        raise RuntimeError("GitHub did not return a valid pull-request base ref")

    mergeable = metadata.get("mergeable")
    if mergeable is not None and not isinstance(mergeable, bool):
        raise RuntimeError("GitHub returned a malformed pull-request mergeable value")
    merge_state = metadata.get("mergeable_state")
    if merge_state is not None and (not isinstance(merge_state, str) or not merge_state):
        raise RuntimeError("GitHub returned a malformed pull-request merge state")
    return base_ref, base_sha.casefold(), head_sha.casefold(), mergeable, merge_state


async def _read_up_to_date(
    app: AppContext,
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
) -> tuple[bool | None, bool, list[str]]:
    """Compare exact base/head commits while returning only bounded freshness metadata."""

    try:
        result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
            "-X",
            "GET",
            "--jq",
            "{behind_by: .behind_by}",
        )
    except GitHubRequestError as exc:
        status = f"HTTP {exc.status_code}" if exc.status_code is not None else "unknown status"
        return None, False, [f"Base/head freshness evidence is unavailable ({status})."]
    if not isinstance(result, dict):
        raise RuntimeError("GitHub returned malformed base/head comparison evidence")
    behind_by = result.get("behind_by")
    if not isinstance(behind_by, int) or isinstance(behind_by, bool) or behind_by < 0:
        raise RuntimeError("GitHub returned malformed base/head behind count")
    return behind_by == 0, True, []


def _current_valid_approvals(review_state: PullRequestReviewState) -> list[PullRequestReview]:
    """Return latest decisive exact-head approval per identifiable reviewer."""

    latest: dict[str, PullRequestReview] = {}
    decisive = [
        *review_state.current_head_approvals,
        *review_state.current_head_change_requests,
    ]
    for review in decisive:
        if review.reviewer is None:
            continue
        key = review.reviewer.casefold()
        previous = latest.get(key)
        if previous is None or (
            review.submitted_at or "",
            review.id,
        ) > (
            previous.submitted_at or "",
            previous.id,
        ):
            latest[key] = review
    return sorted(
        (review for review in latest.values() if review.state.upper() == "APPROVED"),
        key=lambda review: review.id,
    )


def _review_requirements_result(
    policy: MergePolicy | None,
    policy_fields_known: bool,
    review_state: PullRequestReviewState | None,
    approval_count: int | None,
) -> bool | None:
    """Combine policy count with GitHub's exact-head review decision conservatively."""

    if (
        not policy_fields_known
        or policy is None
        or review_state is None
        or not review_state.exact_head_evidence
        or approval_count is None
    ):
        return None
    if approval_count < policy.required_approvals:
        return False
    if review_state.requirements_satisfied is False:
        return False
    if review_state.requirements_satisfied is True:
        return True
    return None


def _invalid_result(
    *,
    number: int,
    base_ref: str,
    base_sha: str,
    expected_head_sha: str,
    current_head_sha: str,
    mergeable: bool | None,
    merge_state: str | None,
    reason: str,
) -> PullRequestMergeRequirements:
    """Return identity only when head/base movement invalidates collected evidence."""

    return PullRequestMergeRequirements(
        number=number,
        base_ref=base_ref,
        base_sha=base_sha,
        expected_head_sha=expected_head_sha,
        current_head_sha=current_head_sha,
        head_matches_expected=current_head_sha == expected_head_sha,
        exact_head_evidence=False,
        mergeable=mergeable,
        merge_state=merge_state,
        policy_evidence_complete=False,
        checks_evidence_complete=False,
        review_evidence_complete=False,
        up_to_date_evidence_complete=False,
        allowed_merge_methods_complete=False,
        warning=reason,
    )


def _allowed_methods(
    policy: MergePolicy | None,
    policy_complete: bool,
    repository_methods: set[MergeMethod],
    repository_methods_complete: bool,
) -> tuple[list[MergeMethod], bool]:
    if not policy_complete or policy is None or not repository_methods_complete:
        return [], False
    return (
        [
            method
            for method in _MERGE_METHOD_ORDER
            if method in repository_methods and method in policy.allowed_merge_methods
        ],
        True,
    )


@mcp.tool(
    title="Get exact-head pull request merge requirements",
    description=(
        "Read-only: aggregate effective branch/ruleset merge policy, current required checks, "
        "exact-head review/thread state, base freshness, mergeability, and allowed merge methods "
        "for one expected pull-request head. Missing policy visibility or head movement is "
        "reported as incomplete evidence and never interpreted as no requirement."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_merge_requirements(
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
            description="Exact pull-request head SHA whose merge requirements are requested.",
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> PullRequestMergeRequirements:
    """Return conservative merge requirements bound to one unchanged PR head/base."""

    logger.info("MCP tool invocation reached server: tool=gh_get_merge_requirements")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    expected = expected_head_sha.casefold()

    initial_metadata = await _get_pr_metadata(app, owner, repo, number)
    base_ref, base_sha, initial_head, initial_mergeable, initial_merge_state = _pr_snapshot(
        initial_metadata
    )
    if initial_head != expected:
        return _invalid_result(
            number=number,
            base_ref=base_ref,
            base_sha=base_sha,
            expected_head_sha=expected,
            current_head_sha=initial_head,
            mergeable=initial_mergeable,
            merge_state=initial_merge_state,
            reason=(
                f"Expected head {expected} does not match current head {initial_head}; "
                "merge requirements were not evaluated."
            ),
        )

    policy, policy_complete, warnings = await read_effective_merge_policy(
        app,
        owner,
        repo,
        base_ref,
    )
    repository_methods, repository_methods_complete, method_warnings = (
        await read_repository_merge_methods(app, owner, repo)
    )
    warnings.extend(method_warnings)

    checks_identity_matches = True
    if policy_complete and policy is not None and not policy.required_status_checks:
        current_checks: list[PullRequestCheck] = []
        checks_read_complete = True
    else:
        try:
            check_state = await gh_get_pr_checks(
                owner,
                repo,
                number,
                ctx=ctx,
                required_only=True,
                max_checks=min(app.settings.hard_max_results, 1_000),
            )
        except GitHubRequestError as exc:
            status = f"HTTP {exc.status_code}" if exc.status_code is not None else "unknown status"
            current_checks = []
            checks_read_complete = False
            warnings.append(f"Current required-check evidence is unavailable ({status}).")
        else:
            current_checks = check_state.checks
            checks_read_complete = not check_state.truncated
            checks_identity_matches = (
                check_state.base_sha.casefold(),
                check_state.head_sha.casefold(),
            ) == (base_sha, initial_head)
            if check_state.truncated:
                warnings.append(
                    "Current required-check evidence exceeds the configured result bound; "
                    "check evidence is incomplete."
                )

    up_to_date, freshness_complete, freshness_warnings = await _read_up_to_date(
        app,
        owner,
        repo,
        base_sha,
        initial_head,
    )
    warnings.extend(freshness_warnings)

    review_state: PullRequestReviewState | None
    try:
        review_state = await gh_get_pr_review_state(
            owner,
            repo,
            number,
            expected,
            ctx=ctx,
        )
    except GitHubRequestError as exc:
        review_state = None
        status = f"HTTP {exc.status_code}" if exc.status_code is not None else "unknown status"
        warnings.append(f"Exact-head review evidence is unavailable ({status}).")

    final_metadata = await _get_pr_metadata(app, owner, repo, number)
    (
        final_base_ref,
        final_base_sha,
        final_head,
        mergeable,
        merge_state,
    ) = _pr_snapshot(final_metadata)
    identity_changed = (final_base_ref, final_base_sha, final_head) != (
        base_ref,
        base_sha,
        initial_head,
    )
    review_observed_movement = review_state is not None and not review_state.head_matches_expected
    if identity_changed or review_observed_movement or not checks_identity_matches:
        return _invalid_result(
            number=number,
            base_ref=final_base_ref,
            base_sha=final_base_sha,
            expected_head_sha=expected,
            current_head_sha=final_head,
            mergeable=mergeable,
            merge_state=merge_state,
            reason=(
                "Pull request base or head changed during the merge-requirements read; "
                "collected requirement evidence was discarded."
            ),
        )

    allowed_methods, methods_complete = _allowed_methods(
        policy,
        policy_complete,
        repository_methods,
        repository_methods_complete,
    )
    if policy_complete and policy is not None:
        policy_fields_known = True
        requirements = list(policy.required_status_checks.values())
        required_approvals = policy.required_approvals
        code_owner_review_required = policy.code_owner_review_required
        last_push_approval_required = policy.last_push_approval_required
        conversation_resolution_required = policy.conversation_resolution_required
        up_to_date_required = policy.up_to_date_required
    else:
        policy_fields_known = False
        requirements = []
        required_approvals = None
        code_owner_review_required = None
        last_push_approval_required = None
        conversation_resolution_required = None
        up_to_date_required = None

    if review_state is None:
        current_approvals = []
        approval_count = None
        unresolved_threads = []
        review_decision = None
        review_complete = False
    else:
        current_approvals = _current_valid_approvals(review_state)
        approval_count = len(current_approvals) if review_state.exact_head_evidence else None
        unresolved_threads = review_state.unresolved_review_threads
        review_decision = review_state.review_decision
        review_complete = review_state.exact_head_evidence
        if review_state.warning:
            warnings.append(review_state.warning)

    review_satisfied = _review_requirements_result(
        policy,
        policy_fields_known,
        review_state,
        approval_count,
    )
    warning = " ".join(dict.fromkeys(warnings)) or None
    return PullRequestMergeRequirements(
        number=number,
        base_ref=base_ref,
        base_sha=base_sha,
        expected_head_sha=expected,
        current_head_sha=initial_head,
        head_matches_expected=True,
        exact_head_evidence=True,
        mergeable=mergeable,
        merge_state=merge_state,
        policy_evidence_complete=policy_fields_known,
        checks_evidence_complete=(
            policy_fields_known and checks_read_complete and checks_identity_matches
        ),
        review_evidence_complete=review_complete,
        up_to_date_evidence_complete=freshness_complete,
        required_status_checks=requirements,
        current_required_checks=current_checks,
        required_approvals=required_approvals,
        current_valid_approvals=current_approvals,
        current_valid_approval_count=approval_count,
        review_decision=review_decision,
        review_requirements_satisfied=review_satisfied,
        code_owner_review_required=code_owner_review_required,
        last_push_approval_required=last_push_approval_required,
        conversation_resolution_required=conversation_resolution_required,
        unresolved_review_threads=unresolved_threads,
        up_to_date_required=up_to_date_required,
        up_to_date=up_to_date,
        allowed_merge_methods=allowed_methods,
        allowed_merge_methods_complete=methods_complete,
        warning=warning,
    )
