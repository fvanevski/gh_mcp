"""Effective merge-policy reads for exact-head merge requirement aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .merge_requirements_models import (
    MergeEvidenceStatus,
    MergeMethod,
    MergeRequirementEvidenceSource,
    RequiredStatusCheck,
)
from .request_governor import GitHubRequestError
from .tooling import AppContext

_RULES_PER_PAGE_MAX = 100
_MERGE_METHODS: frozenset[MergeMethod] = frozenset({"merge", "squash", "rebase"})


@dataclass(slots=True)
class MergePolicy:
    """Effective merge policy after conservative rule layering."""

    required_status_checks: dict[tuple[str, int | None], RequiredStatusCheck] = field(
        default_factory=dict
    )
    required_approvals: int = 0
    dismiss_stale_reviews_on_push: bool = False
    code_owner_review_required: bool = False
    last_push_approval_required: bool = False
    conversation_resolution_required: bool = False
    up_to_date_required: bool = False
    allowed_merge_methods: set[MergeMethod] = field(default_factory=lambda: set(_MERGE_METHODS))
    linear_history_required: bool = False
    unmodeled_policy_requirements: set[str] = field(default_factory=set)


@dataclass(slots=True)
class MergePolicyRead:
    """Effective policy plus bounded source-level completeness diagnostics."""

    policy: MergePolicy | None
    complete: bool
    warnings: list[str]
    evidence_sources: list[MergeRequirementEvidenceSource]


@dataclass(slots=True)
class RepositoryMergeMethodsRead:
    """Repository merge switches plus bounded source-level diagnostics."""

    methods: set[MergeMethod]
    complete: bool
    warnings: list[str]
    evidence_source: MergeRequirementEvidenceSource


def _evidence(
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


def _read_bool(mapping: dict[str, Any], key: str, *, label: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise RuntimeError(f"GitHub returned malformed {label}")
    return value


def _read_nonnegative_int(mapping: dict[str, Any], key: str, *, label: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"GitHub returned malformed {label}")
    return value


def _enabled_value(value: Any, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return _read_bool(value, "enabled", label=label)
    raise RuntimeError(f"GitHub returned malformed {label}")


def _add_required_check(
    policy: MergePolicy,
    *,
    context: Any,
    integration_id: Any,
    label: str,
) -> None:
    if not isinstance(context, str) or not context:
        raise RuntimeError(f"GitHub returned {label} without a valid context")
    if integration_id is not None and (
        not isinstance(integration_id, int) or isinstance(integration_id, bool)
    ):
        raise RuntimeError(f"GitHub returned {label} with an invalid integration id")
    policy.required_status_checks[(context, integration_id)] = RequiredStatusCheck(
        context=context,
        integration_id=integration_id,
    )


def _apply_classic_protection(policy: MergePolicy, protection: dict[str, Any]) -> None:
    """Layer the one effective classic branch-protection rule into policy."""

    status_checks = protection.get("required_status_checks")
    if status_checks is not None:
        if not isinstance(status_checks, dict):
            raise RuntimeError("GitHub returned malformed classic required status checks")
        policy.up_to_date_required |= _read_bool(
            status_checks,
            "strict",
            label="classic required-status strictness",
        )
        raw_checks = status_checks.get("checks")
        if raw_checks is not None:
            if not isinstance(raw_checks, list):
                raise RuntimeError("GitHub returned malformed classic required check list")
            for item in raw_checks:
                if not isinstance(item, dict):
                    raise RuntimeError("GitHub returned a malformed classic required check")
                app_id = item.get("app_id")
                _add_required_check(
                    policy,
                    context=item.get("context"),
                    integration_id=None if app_id == -1 else app_id,
                    label="classic required check",
                )
        raw_contexts = status_checks.get("contexts", [])
        if not isinstance(raw_contexts, list):
            raise RuntimeError("GitHub returned malformed classic status-check contexts")
        for context in raw_contexts:
            if not isinstance(context, str) or not context:
                raise RuntimeError("GitHub returned an invalid classic status-check context")
            if not any(key[0] == context for key in policy.required_status_checks):
                _add_required_check(
                    policy,
                    context=context,
                    integration_id=None,
                    label="classic required check",
                )

    reviews = protection.get("required_pull_request_reviews")
    if reviews is not None:
        if not isinstance(reviews, dict):
            raise RuntimeError("GitHub returned malformed classic pull-request review protection")
        policy.required_approvals = max(
            policy.required_approvals,
            _read_nonnegative_int(
                reviews,
                "required_approving_review_count",
                label="classic required approval count",
            ),
        )
        policy.dismiss_stale_reviews_on_push |= _read_bool(
            reviews,
            "dismiss_stale_reviews",
            label="classic stale-review dismissal requirement",
        )
        policy.code_owner_review_required |= _read_bool(
            reviews,
            "require_code_owner_reviews",
            label="classic code-owner review requirement",
        )
        policy.last_push_approval_required |= _read_bool(
            reviews,
            "require_last_push_approval",
            label="classic last-push approval requirement",
        )

    conversation = protection.get("required_conversation_resolution")
    if conversation is not None:
        policy.conversation_resolution_required |= _enabled_value(
            conversation,
            label="classic conversation-resolution requirement",
        )

    linear_history = protection.get("required_linear_history")
    if linear_history is not None:
        policy.linear_history_required |= _enabled_value(
            linear_history,
            label="classic linear-history requirement",
        )


def _apply_ruleset_rules(policy: MergePolicy, rules: list[Any]) -> None:
    """Layer modeled active rules and record every unmodeled rule fail-closed."""

    for raw_rule in rules:
        if not isinstance(raw_rule, dict):
            raise RuntimeError("GitHub returned a malformed active ruleset rule")
        rule_type = raw_rule.get("type")
        if not isinstance(rule_type, str) or not rule_type:
            raise RuntimeError("GitHub returned an active ruleset rule without a valid type")

        if rule_type == "required_linear_history":
            policy.linear_history_required = True
            continue

        if rule_type == "required_status_checks":
            parameters = raw_rule.get("parameters")
            if not isinstance(parameters, dict):
                raise RuntimeError("GitHub returned malformed ruleset required-status parameters")
            strict = _read_bool(
                parameters,
                "strict_required_status_checks_policy",
                label="ruleset required-status strictness",
            )
            checks = parameters.get("required_status_checks")
            if not isinstance(checks, list):
                raise RuntimeError("GitHub returned malformed ruleset required status checks")
            if strict and checks:
                policy.up_to_date_required = True
            for item in checks:
                if not isinstance(item, dict):
                    raise RuntimeError("GitHub returned a malformed ruleset required check")
                _add_required_check(
                    policy,
                    context=item.get("context"),
                    integration_id=item.get("integration_id"),
                    label="ruleset required check",
                )
            continue

        if rule_type != "pull_request":
            policy.unmodeled_policy_requirements.add(f"ruleset:{rule_type}")
            continue

        parameters = raw_rule.get("parameters")
        if not isinstance(parameters, dict):
            raise RuntimeError("GitHub returned malformed ruleset pull-request parameters")
        methods = parameters.get("allowed_merge_methods")
        if not isinstance(methods, list) or not methods:
            raise RuntimeError("GitHub returned malformed ruleset allowed merge methods")
        normalized_methods: set[MergeMethod] = set()
        for method in methods:
            if method not in _MERGE_METHODS:
                raise RuntimeError("GitHub returned an unknown ruleset merge method")
            normalized_methods.add(method)
        policy.allowed_merge_methods.intersection_update(normalized_methods)

        policy.required_approvals = max(
            policy.required_approvals,
            _read_nonnegative_int(
                parameters,
                "required_approving_review_count",
                label="ruleset required approval count",
            ),
        )
        policy.dismiss_stale_reviews_on_push |= _read_bool(
            parameters,
            "dismiss_stale_reviews_on_push",
            label="ruleset stale-review dismissal requirement",
        )
        policy.code_owner_review_required |= _read_bool(
            parameters,
            "require_code_owner_review",
            label="ruleset code-owner review requirement",
        )
        policy.last_push_approval_required |= _read_bool(
            parameters,
            "require_last_push_approval",
            label="ruleset last-push approval requirement",
        )
        policy.conversation_resolution_required |= _read_bool(
            parameters,
            "required_review_thread_resolution",
            label="ruleset conversation-resolution requirement",
        )
        required_reviewers = parameters.get("required_reviewers", [])
        if not isinstance(required_reviewers, list):
            raise RuntimeError("GitHub returned malformed ruleset required reviewers")
        if required_reviewers:
            policy.unmodeled_policy_requirements.add("pull_request.required_reviewers")


def _visibility_warning(label: str, error: GitHubRequestError) -> str:
    status = f"HTTP {error.status_code}" if error.status_code is not None else "unknown status"
    return f"{label} is unavailable ({status}); merge-policy evidence is incomplete."


async def _read_active_rules(
    app: AppContext,
    owner: str,
    repo: str,
    base_ref: str,
) -> tuple[
    list[Any],
    bool,
    list[str],
    MergeRequirementEvidenceSource,
]:
    """Read one bounded active-rule page and prove whether more policy exists."""

    endpoint = f"repos/{owner}/{repo}/rules/branches/{quote(base_ref, safe='')}"
    limit = max(1, min(app.settings.hard_max_results, _RULES_PER_PAGE_MAX))
    try:
        result = await app.client.run(
            "api",
            endpoint,
            "-X",
            "GET",
            "-f",
            "page=1",
            "-f",
            f"per_page={limit}",
        )
    except GitHubRequestError as exc:
        return (
            [],
            False,
            [_visibility_warning("Active ruleset policy", exc)],
            _evidence(
                "effective_branch_rules",
                "unavailable",
                http_status=exc.status_code,
                reason="Active rules for the exact base branch could not be read.",
                blocks_policy=True,
                blocks_checks=True,
                blocks_methods=True,
            ),
        )

    if not isinstance(result, list) or len(result) > limit:
        raise RuntimeError("GitHub returned malformed or over-bound active ruleset evidence")
    if len(result) < limit:
        return (
            result,
            True,
            [],
            _evidence(
                "effective_branch_rules",
                "complete",
                reason="All active rules for the exact base branch were read within the bound.",
            ),
        )

    try:
        following = await app.client.run(
            "api",
            endpoint,
            "-X",
            "GET",
            "-f",
            f"page={limit + 1}",
            "-f",
            "per_page=1",
        )
    except GitHubRequestError as exc:
        return (
            [],
            False,
            [_visibility_warning("Active ruleset pagination probe", exc)],
            _evidence(
                "effective_branch_rules",
                "unavailable",
                http_status=exc.status_code,
                reason="The active-rules completeness probe could not be read.",
                blocks_policy=True,
                blocks_checks=True,
                blocks_methods=True,
            ),
        )
    if not isinstance(following, list) or len(following) > 1:
        raise RuntimeError("GitHub returned malformed active-rules pagination evidence")
    if following:
        return (
            [],
            False,
            [
                "Active ruleset policy exceeds the configured evidence bound; "
                "merge-policy evidence is incomplete."
            ],
            _evidence(
                "effective_branch_rules",
                "truncated",
                reason="Active branch rules exceed the configured evidence bound.",
                blocks_policy=True,
                blocks_checks=True,
                blocks_methods=True,
            ),
        )
    return (
        result,
        True,
        [],
        _evidence(
            "effective_branch_rules",
            "complete",
            reason="All active rules for the exact base branch were read within the bound.",
        ),
    )


async def _verify_classic_admin_read(
    app: AppContext,
    owner: str,
    repo: str,
) -> tuple[bool, list[str], MergeRequirementEvidenceSource]:
    """Prove Administration(read) independently of classic-protection existence."""

    endpoint = f"repos/{owner}/{repo}/rulesets/rule-suites"
    try:
        result = await app.client.run(
            "api",
            endpoint,
            "-X",
            "GET",
            "-f",
            "page=1",
            "-f",
            "per_page=1",
        )
    except GitHubRequestError as exc:
        return (
            False,
            [
                _visibility_warning(
                    "Classic-protection Administration(read) permission probe",
                    exc,
                )
            ],
            _evidence(
                "classic_protection_admin_permission",
                "unavailable",
                http_status=exc.status_code,
                reason=(
                    "Repository Administration(read) could not be independently verified "
                    "after the classic-protection 404."
                ),
                blocks_policy=True,
                blocks_checks=True,
                blocks_methods=True,
            ),
        )
    if not isinstance(result, list) or len(result) > 1:
        raise RuntimeError("GitHub returned malformed classic-protection permission evidence")
    return (
        True,
        [],
        _evidence(
            "classic_protection_admin_permission",
            "complete",
            reason=(
                "Repository Administration(read) was independently verified through "
                "the rule-suites read endpoint."
            ),
        ),
    )


async def _read_classic_protection(
    app: AppContext,
    owner: str,
    repo: str,
    base_ref: str,
) -> tuple[
    dict[str, Any] | None,
    bool,
    list[str],
    list[MergeRequirementEvidenceSource],
]:
    """Read classic protection and distinguish verified absence from unreadability."""

    branch_endpoint = f"repos/{owner}/{repo}/branches/{quote(base_ref, safe='')}"
    try:
        branch = await app.client.run("api", branch_endpoint, "-X", "GET")
    except GitHubRequestError as exc:
        return (
            None,
            False,
            [_visibility_warning("Base-branch protection marker", exc)],
            [
                _evidence(
                    "classic_branch_protection",
                    "unavailable",
                    http_status=exc.status_code,
                    reason="The exact base branch could not be read before protection evaluation.",
                    blocks_policy=True,
                    blocks_checks=True,
                    blocks_methods=True,
                )
            ],
        )
    if not isinstance(branch, dict):
        raise RuntimeError("GitHub returned malformed base-branch metadata")

    protected = branch.get("protected")
    if protected is not None and not isinstance(protected, bool):
        raise RuntimeError("GitHub returned malformed base-branch protection state")
    if protected is False:
        return (
            None,
            True,
            [],
            [
                _evidence(
                    "classic_branch_protection",
                    "absent",
                    reason="The exact base branch authoritatively reports protected=false.",
                )
            ],
        )

    endpoint = f"repos/{owner}/{repo}/branches/{quote(base_ref, safe='')}/protection"
    try:
        protection = await app.client.run("api", endpoint, "-X", "GET")
    except GitHubRequestError as exc:
        if exc.status_code != 404:
            return (
                None,
                False,
                [_visibility_warning("Classic branch protection", exc)],
                [
                    _evidence(
                        "classic_branch_protection",
                        "unavailable",
                        http_status=exc.status_code,
                        reason="Classic branch protection could not be read.",
                        blocks_policy=True,
                        blocks_checks=True,
                        blocks_methods=True,
                    )
                ],
            )

        (
            permission_verified,
            permission_warnings,
            permission_source,
        ) = await _verify_classic_admin_read(app, owner, repo)
        if permission_verified:
            return (
                None,
                True,
                [],
                [
                    _evidence(
                        "classic_branch_protection",
                        "absent",
                        http_status=404,
                        reason=(
                            "Classic protection returned HTTP 404 for the existing exact "
                            "base branch, and repository Administration(read) was independently "
                            "verified."
                        ),
                    ),
                    permission_source,
                ],
            )
        return (
            None,
            False,
            [
                "Classic branch protection returned HTTP 404, but absence cannot be "
                "established because repository Administration(read) was not independently "
                "verified; merge-policy evidence is incomplete.",
                *permission_warnings,
            ],
            [
                _evidence(
                    "classic_branch_protection",
                    "unavailable",
                    http_status=404,
                    reason=(
                        "HTTP 404 is ambiguous without independent repository "
                        "Administration(read) proof."
                    ),
                    blocks_policy=True,
                    blocks_checks=True,
                    blocks_methods=True,
                ),
                permission_source,
            ],
        )
    if not isinstance(protection, dict):
        raise RuntimeError("GitHub returned malformed classic branch protection")
    return (
        protection,
        True,
        [],
        [
            _evidence(
                "classic_branch_protection",
                "present",
                reason="Classic branch protection was read successfully.",
            )
        ],
    )


async def read_effective_merge_policy_evidence(
    app: AppContext,
    owner: str,
    repo: str,
    base_ref: str,
) -> MergePolicyRead:
    """Read effective policy with source-level completeness diagnostics."""

    rules, rules_complete, warnings, rules_source = await _read_active_rules(
        app, owner, repo, base_ref
    )
    classic, classic_complete, classic_warnings, classic_sources = await _read_classic_protection(
        app,
        owner,
        repo,
        base_ref,
    )
    warnings.extend(classic_warnings)
    evidence_sources = [rules_source, *classic_sources]
    if not (rules_complete and classic_complete):
        return MergePolicyRead(None, False, warnings, evidence_sources)

    policy = MergePolicy()
    _apply_ruleset_rules(policy, rules)
    if classic is not None:
        _apply_classic_protection(policy, classic)
    if policy.unmodeled_policy_requirements:
        unmodeled = ", ".join(sorted(policy.unmodeled_policy_requirements))
        warnings.append(
            "Active ruleset policy contains requirements this aggregate does not model "
            f"({unmodeled}); merge-policy evidence is incomplete."
        )
        evidence_sources.append(
            _evidence(
                "policy_composition",
                "unavailable",
                reason=f"Unmodeled effective policy requirements: {unmodeled}",
                blocks_policy=True,
                blocks_checks=True,
                blocks_methods=True,
            )
        )
        return MergePolicyRead(None, False, warnings, evidence_sources)
    if policy.linear_history_required:
        policy.allowed_merge_methods.discard("merge")
    evidence_sources.append(
        _evidence(
            "policy_composition",
            "complete",
            reason="All observed merge-relevant policy requirements were modeled.",
        )
    )
    return MergePolicyRead(policy, True, warnings, evidence_sources)


async def read_effective_merge_policy(
    app: AppContext,
    owner: str,
    repo: str,
    base_ref: str,
) -> tuple[MergePolicy | None, bool, list[str]]:
    """Compatibility wrapper returning the historical policy tuple."""

    result = await read_effective_merge_policy_evidence(app, owner, repo, base_ref)
    return result.policy, result.complete, result.warnings


async def read_repository_merge_methods_evidence(
    app: AppContext,
    owner: str,
    repo: str,
) -> RepositoryMergeMethodsRead:
    """Read repository merge switches with source-level completeness diagnostics."""

    try:
        result = await app.client.run("api", f"repos/{owner}/{repo}", "-X", "GET")
    except GitHubRequestError as exc:
        return RepositoryMergeMethodsRead(
            set(),
            False,
            [_visibility_warning("Repository merge-method settings", exc)],
            _evidence(
                "repository_merge_settings",
                "unavailable",
                http_status=exc.status_code,
                reason="Repository merge-method switches could not be read.",
                blocks_methods=True,
            ),
        )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub returned malformed repository metadata")

    mapping: tuple[tuple[str, MergeMethod], ...] = (
        ("allow_merge_commit", "merge"),
        ("allow_squash_merge", "squash"),
        ("allow_rebase_merge", "rebase"),
    )
    methods: set[MergeMethod] = set()
    for field_name, method in mapping:
        value = result.get(field_name)
        if not isinstance(value, bool):
            warning = (
                "Repository merge-method settings are not visible in the response; "
                "allowed merge methods are indeterminate."
            )
            return RepositoryMergeMethodsRead(
                set(),
                False,
                [warning],
                _evidence(
                    "repository_merge_settings",
                    "unavailable",
                    reason=f"Repository field {field_name} is absent or malformed.",
                    blocks_methods=True,
                ),
            )
        if value:
            methods.add(method)
    return RepositoryMergeMethodsRead(
        methods,
        True,
        [],
        _evidence(
            "repository_merge_settings",
            "complete",
            reason="Repository merge/squash/rebase switches were read successfully.",
        ),
    )


async def read_repository_merge_methods(
    app: AppContext,
    owner: str,
    repo: str,
) -> tuple[set[MergeMethod], bool, list[str]]:
    """Compatibility wrapper returning the historical repository-method tuple."""

    result = await read_repository_merge_methods_evidence(app, owner, repo)
    return result.methods, result.complete, result.warnings
