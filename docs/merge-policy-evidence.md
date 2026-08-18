# Merge-policy evidence completeness

`gh_get_merge_requirements` is a read-only exact-head aggregate. It must distinguish
authoritative policy absence from policy unreadability; neither HTTP 404 nor an empty
required-check list is sufficient by itself.

## Classic branch protection

For the exact base branch, classic protection has three semantic states:

- `present`: the classic protection endpoint returned a valid protection object.
- `absent`: either the branch metadata authoritatively reports `protected=false`, or the
  protection endpoint returned HTTP 404 **and** repository `Administration: read` was
  independently proven.
- `unavailable`: branch/protection evidence could not be read, or a protection HTTP 404
  could not be disambiguated with independent `Administration: read` proof.

The independent permission proof uses the repository rule-suites read endpoint with a
single bounded item. It is used only after a classic-protection 404. A failed or malformed
probe never upgrades the 404 to absence.

## Effective rulesets

Applicable active rules are read independently for the exact base branch from the
branch-rules endpoint. Pagination is bounded and explicitly probed for overflow. Truncated
or unavailable rule evidence keeps policy, required-check, and allowed-method evidence
incomplete.

Modeled active rules are combined with classic protection. Required status checks are
unioned by their full `(context, integration_id)` identity, review requirements are
conservatively strengthened, and merge-method restrictions are intersected. Unknown
merge-relevant rule types remain fail-closed.

## Exact-head required checks

Required-check policy and current state are separate evidence layers.

`required_status_checks` preserves the policy identity `(context, integration_id)`.
A `null` integration ID is a context-only requirement. A positive integration ID pins the
requirement to a specific GitHub App. Classic protection's documented `app_id=-1` sentinel
is normalized to the context-only form because it explicitly permits any app to provide
the status.

The ordinary `gh pr checks --required` projection is used for exact-head current state and
GitHub-requiredness, but it does not expose the GitHub App identity needed to distinguish
integration-pinned requirements. When effective policy contains a pinned identity,
`gh_get_merge_requirements` therefore performs an additional bounded, read-only GraphQL
read of the exact head. For required `CheckRun` nodes it records
`checkSuite.app.databaseId`; GitHub defines `CheckSuite.app` as the app that created the
suite, and that database ID is matched to the policy `integration_id`.

`current_required_checks` exposes the resolved `integration_id` alongside the check state.
Full tuple identity is preserved: two effective requirements with the same context but
different integration IDs remain two distinct current-state observations.

A missing required identity is represented as `state="UNKNOWN"` / `bucket="pending"` only
after all evidence needed to establish its absence has been read completely and remained
bound to the expected base/head. Missing is therefore an authoritative unsatisfied state,
not evidence incompleteness.

If pinned identity evidence is unavailable, truncated, inconsistent with the composed
policy, or moves to a different base/head, the aggregate fails closed. It must not
synthesize absence or report `checks_evidence_complete=true` for an identity whose
provenance is unresolved. A required legacy `StatusContext` on a context that is pinned to
a GitHub App is likewise insufficient because a status context carries no check-suite app
identity.

## Repository merge methods

Repository `allow_merge_commit`, `allow_squash_merge`, and `allow_rebase_merge` settings
are read independently. The reported allowed methods are the intersection of those
switches and the effective policy's allowed methods; classic or ruleset linear history
removes merge commits.

## Source diagnostics

The result includes bounded `evidence_sources`. Each entry reports `source`, `status`,
optional `http_status`, bounded `reason`, and whether that source blocks
`policy_evidence_complete`, `checks_evidence_complete`, or
`allowed_merge_methods_complete`.

Expected source names include `effective_branch_rules`, `classic_branch_protection`,
`classic_protection_admin_permission` when needed, `policy_composition`,
`repository_merge_settings`, `current_required_checks`, `base_freshness`,
`reviews_threads`, and `pull_request_identity`.

The diagnostic list explains incompleteness; it does not weaken the aggregate's existing
booleans. Head/base movement still invalidates collected evidence.

## Schema and release boundary

Issue #78 adds structured `evidence_sources` and exact identity on
`current_required_checks.integration_id`. Both are public result-schema changes. They must
be reflected in the project/package/server/tool-schema version and deployed plugin schema
before a release containing this implementation is published. The version transition is a
release-gate concern; it is not evidence that the implementation itself is incomplete.

## Safety boundary

This contract adds no branch-protection/ruleset mutation, administrator bypass, implicit
merge action, or change to `gh_merge_pr`. All REST and GraphQL operations described above
are read-only. Missing permission or source visibility remains incomplete evidence rather
than being interpreted as no requirement.

Issue authority: #78.
