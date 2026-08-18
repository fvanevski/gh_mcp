# Merge-policy evidence completeness

`gh_get_merge_requirements` is a read-only exact-head aggregate. It must distinguish authoritative policy absence from policy unreadability; neither HTTP 404 nor an empty required-check list is sufficient by itself.

## Classic branch protection

For the exact base branch, classic protection has three semantic states:

- `present`: the classic protection endpoint returned a valid protection object.
- `absent`: either the branch metadata authoritatively reports `protected=false`, or the protection endpoint returned HTTP 404 **and** repository `Administration: read` was independently proven.
- `unavailable`: branch/protection evidence could not be read, or a protection HTTP 404 could not be disambiguated with independent `Administration: read` proof.

The independent permission proof uses the repository rule-suites read endpoint with a single bounded item. It is used only after a classic-protection 404. A failed or malformed probe never upgrades the 404 to absence.

## Effective rulesets

Applicable active rules are read independently for the exact base branch from the branch-rules endpoint. Pagination is bounded and explicitly probed for overflow. Truncated or unavailable rule evidence keeps policy, required-check, and allowed-method evidence incomplete.

Modeled active rules are combined with classic protection. Required status checks are unioned, review requirements are conservatively strengthened, and merge-method restrictions are intersected. Unknown merge-relevant rule types remain fail-closed.

## Repository merge methods

Repository `allow_merge_commit`, `allow_squash_merge`, and `allow_rebase_merge` settings are read independently. The reported allowed methods are the intersection of those switches and the effective policy's allowed methods; classic or ruleset linear history removes merge commits.

## Source diagnostics

The result includes bounded `evidence_sources`. Each entry reports `source`, `status`, optional `http_status`, bounded `reason`, and whether that source blocks `policy_evidence_complete`, `checks_evidence_complete`, or `allowed_merge_methods_complete`.

Expected source names include `effective_branch_rules`, `classic_branch_protection`, `classic_protection_admin_permission` when needed, `policy_composition`, `repository_merge_settings`, `current_required_checks`, `base_freshness`, `reviews_threads`, and `pull_request_identity`.

The diagnostic list explains incompleteness; it does not weaken the aggregate's existing booleans. Head/base movement still invalidates collected evidence.

## Safety boundary

This contract adds no branch-protection/ruleset mutation, administrator bypass, implicit merge action, or change to `gh_merge_pr`. All REST reads remain explicit GETs. Missing permission or source visibility remains incomplete evidence rather than being interpreted as no requirement.

Issue authority: #78.
