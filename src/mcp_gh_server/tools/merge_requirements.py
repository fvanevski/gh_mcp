"""Read-only exact-head pull-request merge-requirement aggregation."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..merge_requirements_models import (
    MergeEvidenceStatus,
    MergeMethod,
    MergeRequirementEvidenceSource,
    PullRequestMergeRequirements,
    RequiredStatusCheck,
    RequiredStatusCheckObservation,
)
from ..merge_requirements_policy import (
    MergePolicy,
    read_effective_merge_policy_evidence,
    read_repository_merge_methods_evidence,
)
from ..models import PullRequestCheck
from ..pr_review_models import PullRequestReview, PullRequestReviewState
from ..request_governor import GitHubRequestError
from ..required_check_evidence import read_pinned_required_check_evidence
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


def _source(
    source: str,
    status: MergeEvidenceStatus,
    *,
    http_status: int | None = None,
    reason: str | None = None,
    blocks_policy: bool = False,
    blocks_checks: bool = False,
    blocks_methods: bool = False,
) -> MergeRequirementEvidenceSource:
    return MergeRequirementEvidenceSource(
        source=source,
        status=status,
        http_status=http_status,
        reason=reason,
        blocks_policy_evidence=blocks_policy,
        blocks_checks_evidence=blocks_checks,
        blocks_allowed_merge_methods=blocks_methods,
    )


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


def _current_valid_approvals(
    review_state: PullRequestReviewState,
    *,
    dismiss_stale_reviews_on_push: bool,
) -> list[PullRequestReview]:
    """Return latest decisive approvals that remain valid under effective merge policy."""

    decisive = [
        *review_state.current_head_approvals,
        *review_state.current_head_change_requests,
    ]
    if not dismiss_stale_reviews_on_push:
        decisive.extend(review_state.stale_approvals)
        decisive.extend(review_state.stale_change_requests)

    latest: dict[str, PullRequestReview] = {}
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


def _review_evidence_status(
    review_state: PullRequestReviewState,
) -> tuple[MergeEvidenceStatus, str]:
    """Explain incomplete review evidence without conflating truncation with head movement."""

    if not review_state.head_matches_expected:
        return (
            "head_mismatch",
            "Review/thread evidence did not remain bound to the expected pull-request head.",
        )
    if review_state.exact_head_evidence:
        return "complete", "Exact-head review/thread evidence is complete."
    reviews_truncated = (
        review_state.reviews_evidence is not None and review_state.reviews_evidence.truncated
    )
    threads_truncated = (
        review_state.review_threads_evidence is not None
        and review_state.review_threads_evidence.truncated
    )
    if reviews_truncated or threads_truncated:
        return (
            "truncated",
            "Review or review-thread evidence exceeded the configured result bound.",
        )
    return (
        "unavailable",
        "Exact-head review/thread evidence is incomplete for a non-identity reason.",
    )


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
    evidence_sources: list[MergeRequirementEvidenceSource] | None = None,
) -> PullRequestMergeRequirements:
    """Return identity only when head/base movement invalidates collected evidence."""

    sources = list(evidence_sources or [])
    sources.append(
        _source(
            "pull_request_identity",
            "head_mismatch",
            reason=reason,
            blocks_policy=True,
            blocks_checks=True,
            blocks_methods=True,
        )
    )
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
        evidence_sources=sources,
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


def _observation(
    check: PullRequestCheck,
    *,
    integration_id: int | None,
) -> RequiredStatusCheckObservation:
    return RequiredStatusCheckObservation.model_validate(
        {
            **check.model_dump(),
            "integration_id": integration_id,
        }
    )


def _missing_observation(required: RequiredStatusCheck) -> RequiredStatusCheckObservation:
    pinned = required.integration_id is not None
    return RequiredStatusCheckObservation(
        name=required.context,
        integration_id=required.integration_id,
        state="UNKNOWN",
        bucket="pending",
        description=(
            "No exact-head observation exists for this required check identity."
            if pinned
            else "Required status check has no observation at the exact head."
        ),
    )


def _reconcile_required_checks(
    requirements: list[RequiredStatusCheck],
    current_checks: list[PullRequestCheck],
    pinned_checks: list[RequiredStatusCheckObservation],
    *,
    absence_authoritative: bool,
) -> list[RequiredStatusCheckObservation]:
    """Represent every effective identity while retaining distinct logical pinned rows."""

    if not requirements:
        return []

    unpinned_contexts = {
        required.context for required in requirements if required.integration_id is None
    }
    pinned_identities = {
        (required.context, required.integration_id)
        for required in requirements
        if required.integration_id is not None
    }

    reconciled: list[RequiredStatusCheckObservation] = [
        _observation(check, integration_id=None)
        for check in current_checks
        if check.name in unpinned_contexts
    ]
    observed_unpinned = {check.name for check in reconciled}

    pinned_by_identity: dict[
        tuple[str, int],
        list[RequiredStatusCheckObservation],
    ] = {}
    for check in pinned_checks:
        integration_id = check.integration_id
        if integration_id is None:
            continue
        identity = (check.name, integration_id)
        if identity not in pinned_identities:
            continue
        pinned_by_identity.setdefault(identity, []).append(check)

    for required in requirements:
        if required.integration_id is None:
            if required.context not in observed_unpinned and absence_authoritative:
                reconciled.append(_missing_observation(required))
            continue

        observed = pinned_by_identity.get((required.context, required.integration_id), [])
        if observed:
            reconciled.extend(observed)
        elif absence_authoritative:
            reconciled.append(_missing_observation(required))

    return reconciled


def _effective_required_checks(policy: MergePolicy) -> list[RequiredStatusCheck]:
    """Normalize GitHub's any-app sentinel while preserving full effective identities."""

    normalized: dict[tuple[str, int | None], RequiredStatusCheck] = {}
    for required in policy.required_status_checks.values():
        integration_id = (
            None if required.integration_id == -1 else required.integration_id
        )
        key = (required.context, integration_id)
        normalized[key] = RequiredStatusCheck(
            context=required.context,
            integration_id=integration_id,
        )
    return list(normalized.values())


def _required_check_policy_consistent(
    requirements: list[RequiredStatusCheck],
    current_checks: list[PullRequestCheck],
) -> tuple[bool, list[str]]:
    """Fail closed if GitHub reports required contexts absent from composed policy."""

    policy_contexts = {required.context for required in requirements}
    unexpected = sorted(
        {check.name for check in current_checks if check.name not in policy_contexts}
    )
    if not unexpected:
        return True, []
    rendered = ", ".join(unexpected)
    return (
        False,
        [
            "GitHub reports exact-head required checks that are absent from the composed "
            f"required-check policy ({rendered}); check evidence is incomplete."
        ],
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

    policy_read = await read_effective_merge_policy_evidence(
        app,
        owner,
        repo,
        base_ref,
    )
    policy = policy_read.policy
    policy_complete = policy_read.complete
    warnings = list(policy_read.warnings)
    evidence_sources = list(policy_read.evidence_sources)
    effective_requirements = (
        _effective_required_checks(policy)
        if policy_complete and policy is not None
        else []
    )

    methods_read = await read_repository_merge_methods_evidence(app, owner, repo)
    repository_methods = methods_read.methods
    repository_methods_complete = methods_read.complete
    warnings.extend(methods_read.warnings)
    evidence_sources.append(methods_read.evidence_source)

    checks_identity_matches = True
    checks_policy_consistent = True
    checks_truncated = False
    pinned_checks: list[RequiredStatusCheckObservation] = []

    if policy_complete and policy is not None and not policy.required_status_checks:
        current_checks: list[PullRequestCheck] = []
        checks_read_complete = True
        evidence_sources.append(
            _source(
                "current_required_checks",
                "complete",
                reason="Effective policy contains no required status checks.",
            )
        )
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
            evidence_sources.append(
                _source(
                    "current_required_checks",
                    "unavailable",
                    http_status=exc.status_code,
                    reason="The exact-head required-check read failed.",
                    blocks_checks=True,
                )
            )
        else:
            current_checks = check_state.checks
            checks_truncated = check_state.truncated
            checks_read_complete = not checks_truncated
            checks_identity_matches = (
                check_state.base_sha.casefold(),
                check_state.head_sha.casefold(),
            ) == (base_sha, initial_head)

            if policy_complete and policy is not None:
                checks_policy_consistent, consistency_warnings = (
                    _required_check_policy_consistent(effective_requirements, current_checks)
                )
                warnings.extend(consistency_warnings)

                all_required_identities = {
                    (required.context, required.integration_id)
                    for required in effective_requirements
                }
                has_pinned_requirements = any(
                    integration_id is not None
                    for _context, integration_id in all_required_identities
                )
                if (
                    has_pinned_requirements
                    and checks_read_complete
                    and checks_identity_matches
                ):
                    try:
                        pinned_read = await read_pinned_required_check_evidence(
                            app,
                            owner,
                            repo,
                            number,
                            base_sha=base_sha,
                            head_sha=initial_head,
                            required_identities=all_required_identities,
                            limit=min(app.settings.hard_max_results, 1_000),
                        )
                    except GitHubRequestError as exc:
                        status = (
                            f"HTTP {exc.status_code}"
                            if exc.status_code is not None
                            else "unknown status"
                        )
                        checks_read_complete = False
                        warnings.append(
                            "Integration-pinned required-check identity evidence is "
                            f"unavailable ({status})."
                        )
                    else:
                        pinned_checks = pinned_read.checks
                        checks_truncated |= pinned_read.truncated
                        checks_read_complete = (
                            checks_read_complete and pinned_read.complete
                        )
                        checks_identity_matches = (
                            checks_identity_matches and pinned_read.identity_matches
                        )
                        warnings.extend(pinned_read.warnings)

            check_status: MergeEvidenceStatus
            if checks_truncated:
                warnings.append(
                    "Current required-check evidence exceeds the configured result bound; "
                    "check evidence is incomplete."
                )
                check_status = "truncated"
                check_reason = "The required-check read exceeded the configured result bound."
            elif not checks_identity_matches:
                check_status = "head_mismatch"
                check_reason = "The required-check snapshot did not match the exact PR identity."
            elif not checks_read_complete or not checks_policy_consistent:
                check_status = "unavailable"
                check_reason = (
                    "Required-check state or integration identity could not be established "
                    "completely from the exact-head evidence."
                )
            else:
                check_status = "complete"
                check_reason = (
                    "The exact-head required-check read, including integration identity where "
                    "required by policy, is complete."
                )
            evidence_sources.append(
                _source(
                    "current_required_checks",
                    check_status,
                    reason=check_reason,
                    blocks_checks=(
                        not policy_complete
                        or not checks_read_complete
                        or not checks_identity_matches
                        or not checks_policy_consistent
                    ),
                )
            )

    up_to_date, freshness_complete, freshness_warnings = await _read_up_to_date(
        app,
        owner,
        repo,
        base_sha,
        initial_head,
    )
    warnings.extend(freshness_warnings)
    freshness_status: MergeEvidenceStatus = "complete" if freshness_complete else "unavailable"
    evidence_sources.append(
        _source(
            "base_freshness",
            freshness_status,
            reason=(
                "Exact base/head freshness comparison completed."
                if freshness_complete
                else "Exact base/head freshness comparison is unavailable."
            ),
        )
    )

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
        evidence_sources.append(
            _source(
                "reviews_threads",
                "unavailable",
                http_status=exc.status_code,
                reason="Exact-head review/thread evidence could not be read.",
            )
        )
    else:
        review_status, review_reason = _review_evidence_status(review_state)
        evidence_sources.append(
            _source(
                "reviews_threads",
                review_status,
                reason=review_reason,
            )
        )

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
            evidence_sources=evidence_sources,
        )

    evidence_sources.append(
        _source(
            "pull_request_identity",
            "complete",
            reason="Initial and final base/head identity snapshots match the expected exact head.",
        )
    )

    allowed_methods, methods_complete = _allowed_methods(
        policy,
        policy_complete,
        repository_methods,
        repository_methods_complete,
    )
    if policy_complete and policy is not None:
        policy_fields_known = True
        requirements = effective_requirements
        required_approvals = policy.required_approvals
        dismiss_stale_reviews_on_push: bool | None = policy.dismiss_stale_reviews_on_push
        code_owner_review_required = policy.code_owner_review_required
        last_push_approval_required = policy.last_push_approval_required
        conversation_resolution_required = policy.conversation_resolution_required
        up_to_date_required = policy.up_to_date_required
    else:
        policy_fields_known = False
        requirements = []
        required_approvals = None
        dismiss_stale_reviews_on_push = None
        code_owner_review_required = None
        last_push_approval_required = None
        conversation_resolution_required = None
        up_to_date_required = None

    check_evidence_complete = (
        policy_fields_known
        and checks_read_complete
        and checks_identity_matches
        and checks_policy_consistent
    )
    current_required_checks = _reconcile_required_checks(
        requirements,
        current_checks,
        pinned_checks,
        absence_authoritative=check_evidence_complete,
    )

    if review_state is None:
        current_approvals = []
        approval_count = None
        unresolved_threads = []
        review_decision = None
        review_complete = False
    else:
        current_approvals = _current_valid_approvals(
            review_state,
            dismiss_stale_reviews_on_push=(
                dismiss_stale_reviews_on_push if dismiss_stale_reviews_on_push is not None else True
            ),
        )
        approval_count = (
            len(current_approvals)
            if review_state.exact_head_evidence and dismiss_stale_reviews_on_push is not None
            else None
        )
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
        checks_evidence_complete=check_evidence_complete,
        review_evidence_complete=review_complete,
        up_to_date_evidence_complete=freshness_complete,
        required_status_checks=requirements,
        current_required_checks=current_required_checks,
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
        evidence_sources=evidence_sources,
        warning=warning,
    )
