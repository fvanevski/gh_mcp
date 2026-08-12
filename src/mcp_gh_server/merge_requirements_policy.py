"""Effective merge-policy reads for exact-head merge requirement aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .merge_requirements_models import MergeMethod, RequiredStatusCheck
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
                _add_required_check(
                    policy,
                    context=item.get("context"),
                    integration_id=item.get("app_id"),
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
            # The active-rules endpoint can add new rule types over time. Any rule this
            # aggregate does not explicitly model is treated as material uncertainty,
            # rather than silently interpreted as "no merge requirement".
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
) -> tuple[list[Any], bool, list[str]]:
    """Read one bounded active-rule page and probe whether additional policy exists."""

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
        return [], False, [_visibility_warning("Active ruleset policy", exc)]

    if not isinstance(result, list) or len(result) > limit:
        raise RuntimeError("GitHub returned malformed or over-bound active ruleset evidence")
    if len(result) < limit:
        return result, True, []

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
        return [], False, [_visibility_warning("Active ruleset pagination probe", exc)]
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
        )
    return result, True, []


async def _read_classic_protection(
    app: AppContext,
    owner: str,
    repo: str,
    base_ref: str,
) -> tuple[dict[str, Any] | None, bool, list[str]]:
    """Read classic protection when the branch reports any active protection."""

    branch_endpoint = f"repos/{owner}/{repo}/branches/{quote(base_ref, safe='')}"
    try:
        branch = await app.client.run("api", branch_endpoint, "-X", "GET")
    except GitHubRequestError as exc:
        branch = None
        warnings = [_visibility_warning("Base-branch protection marker", exc)]
    else:
        warnings = []
        if not isinstance(branch, dict):
            raise RuntimeError("GitHub returned malformed base-branch metadata")

    protected = branch.get("protected") if isinstance(branch, dict) else None
    if protected is not None and not isinstance(protected, bool):
        raise RuntimeError("GitHub returned malformed base-branch protection state")
    if protected is False:
        return None, True, warnings

    endpoint = f"repos/{owner}/{repo}/branches/{quote(base_ref, safe='')}/protection"
    try:
        protection = await app.client.run("api", endpoint, "-X", "GET")
    except GitHubRequestError as exc:
        warnings.append(_visibility_warning("Classic branch protection", exc))
        return None, False, warnings
    if not isinstance(protection, dict):
        raise RuntimeError("GitHub returned malformed classic branch protection")
    return protection, protected is not None, warnings


async def read_effective_merge_policy(
    app: AppContext,
    owner: str,
    repo: str,
    base_ref: str,
) -> tuple[MergePolicy | None, bool, list[str]]:
    """Read and combine only fully modeled rulesets plus classic protection."""

    rules, rules_complete, warnings = await _read_active_rules(app, owner, repo, base_ref)
    classic, classic_complete, classic_warnings = await _read_classic_protection(
        app,
        owner,
        repo,
        base_ref,
    )
    warnings.extend(classic_warnings)
    if not (rules_complete and classic_complete):
        return None, False, warnings

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
        return None, False, warnings
    if policy.linear_history_required:
        policy.allowed_merge_methods.discard("merge")
    return policy, True, warnings


async def read_repository_merge_methods(
    app: AppContext,
    owner: str,
    repo: str,
) -> tuple[set[MergeMethod], bool, list[str]]:
    """Read repository-level merge-method switches without guessing omitted settings."""

    try:
        result = await app.client.run("api", f"repos/{owner}/{repo}", "-X", "GET")
    except GitHubRequestError as exc:
        return set(), False, [_visibility_warning("Repository merge-method settings", exc)]
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
            return (
                set(),
                False,
                [
                    "Repository merge-method settings are not visible in the response; "
                    "allowed merge methods are indeterminate."
                ],
            )
        if value:
            methods.add(method)
    return methods, True, []
