# PR #79 validation-return remediation — 2026-08-18

This record captures the first complete local validation pass of PR #79 after the
independent-review remediation, the failures returned to Central ChatGPT, and the
subsequent remediation boundary. It does not claim that the post-remediation head has
passed validation; a fresh exact-head rerun remains authoritative.

## Validated input revision

The local OpenCode evidence collector validated exact PR head
`8f6765aa636e8baf369afd5db2b6c59789b602e4` against base
`eb9e92a33b24c1df50147f4eb7464479623c3ac4` in a clean detached worktree.

Reported results at that exact head:

- Ruff lint, changed scope: PASS;
- Ruff lint, whole repository: PASS;
- Ruff format check, changed scope: FAIL — four files required canonical reflow;
- Ruff format check, whole repository: FAIL — the same four files required reflow;
- Pyrefly changed scope including tests: FAIL — one unsafe optional `output_schema`
  subscript in `tests/test_issue_78_merge_policy_evidence.py`;
- Pyrefly no-argument project scope: FAIL — six pre-existing errors in four production
  files that were byte-identical between base and the validated PR head;
- focused pytest: PASS, 54 tests;
- broader release/schema/contract pytest: PASS, 85 tests;
- full pytest: PASS, 695 tests;
- `git diff --check`, exact-head identity, and worktree cleanliness: PASS.

No local source, test, configuration, baseline, or suppression was modified during that
validation pass.

## Central semantic remediation

The failed Pyrefly gates cannot be made authoritative by excluding debt, adding a baseline,
changing checker configuration, or substituting mypy. Central therefore repaired the
underlying typing defects.

### Commit `471dbb18dfbde5df727e3fcce1bd1c49ce1d9bb5`

- `tests/test_issue_78_merge_policy_evidence.py`
  - establishes `output_schema is not None` before subscripting it;
  - removes the changed-scope Pyrefly error without a suppression.
- `src/mcp_gh_server/pr_write_models.py`
  - separates the shared review-result fields from the action-bearing internal/public
    result types;
  - avoids narrowing a mutable inherited `action` attribute in three subclasses while
    preserving each public action-specific result schema.
- `src/mcp_gh_server/tools/issue_state.py`
  - proves the GitHub JSON issue number is a non-boolean integer equal to the expected
    issue number before constructing the typed snapshot.
- `src/mcp_gh_server/tools/pr_draft_state.py`
  - applies the same non-boolean integer identity proof to pull-request draft-state
    readback.

### Commit `13e782955f1dd0387476efefad69b0a0e7bf9cf1`

- `src/mcp_gh_server/tools/workflow_dispatch.py`
  - proves the workflow-run readback ID is a non-boolean integer equal to the expected
    run ID before constructing the typed run record.

The exact delta from `8f6765aa636e8baf369afd5db2b6c59789b602e4` through
`13e782955f1dd0387476efefad69b0a0e7bf9cf1` is two commits and five files. No Pyrefly
configuration, checker pin, baseline, or suppression was weakened.

## Remaining local mechanical remediation

Ruff formatting is intentionally assigned to the local agent because the repository-pinned
Ruff executable is the formatting authority and the permitted local-agent scope includes
mechanical lint/format repairs.

At the new exact remote head, the local agent must:

1. fetch and require the remote PR head to match the supplied handoff SHA;
2. run `uv run ruff format --check --diff .` before changing anything;
3. run `uv run ruff format` only as a mechanical canonical-format operation on the files
   Ruff reports as requiring formatting;
4. inspect the resulting native Git diff and verify that every local change is formatter-only;
5. commit only that formatting delta to the PR branch;
6. record the new exact commit SHA and clean worktree;
7. rerun the complete validation matrix below on that new exact head.

If Ruff reports a change that is not mechanical formatting, or if any semantic edit appears
necessary, stop and return the evidence to Central rather than modifying behavior.

## Required final validation matrix

After the formatting-only commit, rerun every family rather than only the previously failing
gates:

1. changed-scope Ruff lint;
2. whole-repository Ruff lint;
3. changed-scope Ruff format check;
4. whole-repository Ruff format check;
5. changed-scope repository-pinned Pyrefly, explicitly including changed tests;
6. no-argument full-project repository-pinned Pyrefly;
7. focused #78 / merge-policy / release-gate pytest authority;
8. broader release/schema/contract/reviewer/protocol pytest authority;
9. full pytest;
10. base-to-head `git diff --check`, raw exact HEAD, and clean worktree state.

Report each family independently with decisive output and exit status. Any failure remains
evidence and returns to Central; do not weaken the checker, formatter, tests, scope, baseline,
or suppressions to manufacture a pass.
