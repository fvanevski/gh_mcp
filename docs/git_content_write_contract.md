# Git and content write contract

Issue #60 migrates the public Git-reference and repository-content writes away from legacy compatibility adapters and onto the shared exact-outcome contract.

This document records the invariants that must remain true for `gh_create_branch`, `gh_create_branch_from_sha`, and `gh_commit_files`.

## Shared exact-outcome contract

The migrated tools expose the shared `ExactWriteResult` outcome fields directly:

- `precondition_checked`: whether the required exact-state precondition ran successfully immediately before the mutation.
- `write_completed`: `True` for a known completed mutation, `False` for a known failure before completion, and `None` when transport or application behavior leaves mutation completion ambiguous.
- `readback_completed`: whether authoritative post-write state was obtained.
- `state_matches_requested`: `True` or `False` only when authoritative readback completed; otherwise `None`.
- `warning`: fail-closed ambiguity, readback, or semantic-mismatch guidance.
- `request_id`: GitHub request identity when supplied by the governed transport.

An ambiguous mutation is attempted exactly once. Authoritative readback may establish the resulting state, but it does not rewrite `write_completed=None` into known transport success or failure. Callers must not blindly replay an ambiguous write.

## `gh_create_branch_from_sha`

`gh_create_branch_from_sha` accepts one exact 40-character commit SHA and never moves an existing ref.

The server resolves the requested commit immediately before mutation, performs one Git-ref creation attempt, and then reads the exact branch ref back. A pre-existing branch at the same exact SHA is a safe no-write result. A conflicting branch remains unchanged and cannot be upgraded to success.

## `gh_create_branch`

`gh_create_branch` accepts a branch-name base, resolves that base to an exact commit, and rechecks the base immediately before one `createLinkedBranch` mutation. The mutation is bound to exact `issueId`, `repositoryId`, base `oid`, and branch `name` inputs.

The post-write GraphQL readback is bounded to at most 10 pages of 100 linked branches. Verified requested state requires all of the following authoritative evidence:

- the queried repository node ID equals the repository targeted by the mutation;
- the issue node ID equals the requested issue;
- the linked branch exposes a stable node ID;
- the linked `Ref.repository.id` equals the repository targeted by the mutation;
- the linked ref is the exact requested `refs/heads/<name>`;
- the linked ref target is the exact resolved base commit SHA.

Repository identity is part of the association invariant, not merely transport metadata. A linked branch with the same ref name and target SHA in another repository is a semantic mismatch and must never be reported as verified requested state. Missing or malformed repository/ref identity fails closed. On an ambiguous mutation, the server performs authoritative readback without replaying the mutation; `write_completed` remains `None` even if readback later matches.

GitHub's GraphQL schema defines `CreateLinkedBranchInput.repositoryId` as the repository in which to create the branch, and `Ref.repository` as the repository to which the ref belongs. The implementation deliberately binds those identities during readback rather than inferring repository ownership from ref name or target SHA.

## `gh_commit_files`

`gh_commit_files` validates the ordinary write gate and the content-commit fine gate, branch/path constraints, duplicate paths, file count, per-file byte limits, aggregate byte limits, and commit-message bounds before content mutation.

The current branch head must equal `expected_head_sha` before any blob/tree/commit objects are created. The tool then constructs the requested Git objects and performs exactly one GraphQL `updateRefs` compare-and-swap using the observed head as `beforeOid`, the new commit as `afterOid`, and `force=False`.

The exact branch ref is read back after the CAS attempt. A mismatched or ambiguous CAS cannot be upgraded to success. A commit object may exist even when it was not installed on the branch; the result reports that distinction and instructs callers to re-read state rather than retry blindly.

## Public routing and regressions

`write_tool_schema.py` remains the host-facing schema/metadata facade. The three migrated public writes delegate directly to canonical implementations under `src/mcp_gh_server/tools/`, not to the legacy Git/repository adapters.

Regression coverage must include, at minimum:

- exact branch-from-SHA success, conflicting-ref failure, ambiguity, and authoritative ref readback;
- stale content head rejection before any content mutation;
- exact `beforeOid`/`afterOid` content CAS with `force=False`;
- ambiguous content CAS and GraphQL application-error handling without replay;
- exact issue-linked branch association including target repository identity;
- an ambiguous issue-branch mutation followed by a same-ref/same-SHA linked branch from a different repository, which must produce `state_matches_requested=False` and no retry;
- public exact-outcome schema fields and canonical facade provenance.

Async tests added for this contract rely on the repository's configured automatic asyncio mode and do not add explicit `@pytest.mark.asyncio` markers.
