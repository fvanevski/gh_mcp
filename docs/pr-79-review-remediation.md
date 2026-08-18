# PR #79 independent-review remediation and local-validation contract

This document records the Central remediation for the independent review of PR #79
(issue #78). It is implementation authority for the remediation scope, not evidence that
host-dependent validation has passed.

The authoritative handoff revision is always the exact 40-character PR head re-read from
GitHub after the Central remediation commit. Do not infer the handoff head from an older
PR-body validation section.

## Findings disposition

### Blocking: distinct required CheckRuns sharing one pinned policy identity

Resolved in `required_check_evidence.py` and `tools/merge_requirements.py`.

A policy identity such as `(lint, 42)` can have more than one required CheckRun at one
exact head. GitHub CLI keeps distinct CheckRun rows by check name plus workflow/event and
deduplicates reruns only within that logical row. The merge-requirements reader now
mirrors that safety property:

- the exact-head GraphQL identity read collects workflow name and event;
- pinned observations are keyed by `(name, integration_id, workflow, event)`;
- only a newer rerun of that same logical row replaces an older observation;
- reconciliation groups by the policy identity but emits every retained logical row.

A passing row therefore cannot erase a failing or pending required row merely because
both have the same check name and GitHub App ID.

Regression authority:
`tests/test_issue_78_review_remediation.py::test_pinned_required_check_keeps_distinct_workflow_event_rows`
and
`::test_pinned_required_check_deduplicates_only_same_logical_rerun`.

### Blocking: mixed context-only and integration-pinned identities

Resolved in `required_check_evidence.py` and `merge_requirements_policy.py`.

The integration-identity read now receives the complete normalized required-identity set,
not only pinned identities. A required StatusContext, app-less CheckRun, or same-context
CheckRun from another app is therefore compatible when the effective policy also contains
`(context, None)`. Exact app evidence remains mandatory for every pinned identity.

Classic branch-protection composition is source-aware. GitHub's legacy `contexts` field
can mirror a context already represented more precisely in that same classic source's
`checks` field, so that duplicate projection is not promoted to a second any-app
requirement. A classic context that is *not* represented in the classic `checks` list
remains an independent context-only requirement even when an active ruleset has already
contributed a same-context pinned identity.

Regression authority:
`tests/test_issue_78_review_remediation.py::test_mixed_context_only_and_pinned_observations_are_compatible`,
`::test_pinned_only_context_rejects_required_row_without_compatible_app`,
`::test_classic_context_only_requirement_survives_same_context_ruleset_pin`, and
`::test_classic_context_projection_does_not_duplicate_same_source_pinned_check`.

### Blocking: review/thread truncation mislabeled as head movement

Resolved in `tools/merge_requirements.py`.

`reviews_threads` source diagnostics now distinguish:

- `complete` when exact-head review and thread evidence is complete;
- `head_mismatch` only when the review-state read reports head movement;
- `truncated` when review or thread pagination exceeds the configured bound while the
  expected head remains stable;
- `unavailable` for other non-identity incompleteness.

The existing fail-closed review-evidence behavior is unchanged.

Regression authority:
`tests/test_issue_78_review_remediation.py::test_review_evidence_status_reports_stable_head_truncation`,
`::test_review_evidence_status_reserves_head_mismatch_for_identity_movement`, and
`::test_review_evidence_status_reports_complete_exact_head`.

### Blocking: Pyrefly validation authority

The repository now commits an explicit Pyrefly policy:

- `requirements-typecheck.txt` pins `pyrefly==1.1.1`;
- `[tool.pyrefly]` in `pyproject.toml` is the committed configuration;
- no Pyrefly baseline or broad suppression was added;
- no-argument project scope covers production source;
- changed test files remain mandatory explicit arguments during review validation.

`mypy` may remain installed by the existing development dependency lock for historical
developer use, but it is not CI/review static-type authority and must never substitute for
Pyrefly.

This Central change establishes the policy. It does **not** claim that Pyrefly, Ruff, or
pytest passed at the remediation head; those are host-dependent exact-head validation
steps owned by the local OpenCode evidence collector.

## Important non-blocking findings

None were identified in the independent review. No additional design cleanup is folded
into this remediation.

## Codex Review automated suggestions

At Central remediation preflight, GitHub exposed:

- zero PR conversation comments;
- zero review threads;
- no formal review authored by a Codex/Copilot automated reviewer.

Accordingly, there was no concrete Codex Review suggestion to implement or dismiss.
Unseen suggestions are not treated as resolved. Central must re-read reviews, threads,
comments, and checks after the remediation commit and again before final review closure;
any newly observable automated suggestion must receive an explicit disposition.

## Test/documentation gap

The pre-remediation PR body described `8252afbc8850baa3290b9caa554609a4bc1f80c8`
as current/validated even though the reviewed head had advanced to
`94e365789b11ef1436c54dd11ff5f4e1d0811e1f`, and it reported mypy as type-validation
evidence.

After this Central commit, the PR body must be rewritten to:

- identify the new exact Central remediation head;
- describe the independent-review findings and implemented fixes;
- identify Pyrefly as the sole static type authority;
- mark local exact-head validation as **PENDING**, not inferred from prior heads;
- record the current Codex-review visibility result without inventing unseen suggestions.

## Local OpenCode validation handoff

The local agent is an evidence collector. It must not redesign or remediate production
behavior during this validation pass.

### Tool contract

- **Serena (`no-memories`)**: first-line changed-symbol/declaration/reference/dependency
  inspection and diagnostics before execution; audit the same surfaces after any allowed
  purely mechanical local operation.
- **RTK**: routine successful Ruff/Pyrefly/pytest/search output when filtering preserves
  decisive evidence.
- **OpenViking**: bounded historical rationale only; never authority for source, Git,
  GitHub, CI, runtime, or validation state.
- **Native tools**: authoritative `git`, exact SHA/diff/worktree evidence, failures, and
  repository-native test/type/lint execution.

### Exact-head preparation

1. `git fetch origin` using native Git.
2. Resolve PR #79's current 40-character head and require it to equal the Central handoff
   SHA supplied with the handoff.
3. Review detached or in an isolated worktree.
4. Report raw:
   - `git rev-parse HEAD`;
   - base SHA `eb9e92a33b24c1df50147f4eb7464479623c3ac4`;
   - complete ACMR changed-file list.
5. If the remote PR head differs, stop and return the mismatch; do not validate an
   approximate branch revision.

### Required validation sequence

Define the exact existing ACMR Python paths from base to handoff head and use that same
path set for changed-scope checks.

1. Ruff changed scope:
   - `uv run ruff check <ACMR.py ...>`
   - `uv run ruff format --check --diff <ACMR.py ...>`
2. Pyrefly changed scope, including changed tests explicitly:
   - install/run exactly the version pinned by `requirements-typecheck.txt`;
   - `pyrefly check <ACMR.py ...>`
3. Focused pytest, starting with:
   - `tests/test_issue_78_review_remediation.py`
   - `tests/test_issue_78_merge_policy_evidence.py`
   - `tests/test_merge_requirements.py`
   - `tests/test_merge_requirements_policy.py`
4. Full-project Pyrefly:
   - `pyrefly check`
5. Relevant broader repository authorities, including the complete pytest suite and any
   schema/return-model/release-gate tests implicated by this PR.
6. Final integrity:
   - `git diff --check eb9e92a33b24c1df50147f4eb7464479623c3ac4...HEAD`
   - raw `git rev-parse HEAD`
   - clean `git status --short`.

Report every validation family separately. Changed-scope Pyrefly does not replace the
no-argument full-project check, and static checks do not replace runtime tests.

A failure is evidence. Do not modify production code, tests, `pyproject.toml`,
`requirements-typecheck.txt`, add a Pyrefly baseline/suppression, weaken scope, or change
the pinned checker merely to obtain a passing result. Return failures to Central for
analysis.
