# `gh_get_merge_requirements` policy-completeness contract

This document records the development contract for the read-only
`gh_get_merge_requirements` tool introduced by issue #21 for the planned 0.7.1
surface. The 0.7.0 release inventory remains historical release authority until
the 0.7.1 version/schema/docs gate is completed.

## Exact-head scope

The aggregate is evidence for one caller-supplied `expected_head_sha`. It reports
the PR base/head identity, mergeability state, required checks, review evidence,
conversation requirements, base freshness, and allowed merge methods. If the PR
base or head moves during the read, collected readiness evidence is discarded.

## Conservative policy completeness

`policy_evidence_complete=true` means every active ruleset rule returned for the
base branch was either explicitly modeled by the aggregate or no active rules
were returned, and any applicable classic branch-protection policy was visible
and modeled.

The currently modeled active-ruleset rule types are:

- `required_linear_history`;
- `required_status_checks`;
- `pull_request`, except that non-empty `required_reviewers` remains
  intentionally unmodeled.

**Every other active rule type is fail-closed.** The aggregate records the
unmodeled rule identity internally, returns `policy_evidence_complete=false`,
leaves policy-derived requirement fields indeterminate, and emits a warning.
This applies both to known rule types not represented by the current result
model (for example deployment, merge-queue, required-workflow, or code-scanning
requirements) and to future rule types added by GitHub.

This conservative behavior is intentional. An unmodeled active rule must never
be silently reinterpreted as “no merge requirement.” A future implementation
may model additional rule types, but it must add their semantics and regression
coverage before treating policy evidence as complete.

## Related completeness fields

- `checks_evidence_complete` can be true only when policy evidence is complete,
  the required-check read is complete, and the check snapshot matches the same
  exact base/head identity.
- `review_evidence_complete` is inherited from the exact-head review aggregate.
- `up_to_date_evidence_complete` reports whether the exact base/head comparison
  was readable.
- `allowed_merge_methods_complete` requires both complete policy evidence and
  visible repository merge-method settings.

Missing permissions, bounded/truncated policy evidence, unsupported active
rules, malformed GitHub responses, or head/base movement must not be converted
into affirmative merge-readiness evidence.

## Validation invariant

Regression coverage must include:

- modeled rules remaining complete;
- unmodeled `required_reviewers` becoming incomplete;
- known unmodeled merge-affecting rules becoming incomplete;
- an unknown/future active rule becoming incomplete;
- missing policy visibility and bounded policy reads remaining incomplete;
- exact-head movement invalidating collected evidence.

The repository-wide release gate remains:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Any validation claim must be bound to the exact final PR head.
