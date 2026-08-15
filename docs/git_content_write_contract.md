# Git and content write contract

Issue #60 migrates the public Git-reference and repository-content writes away from legacy compatibility adapters and onto the shared exact-outcome contract. Issue #73 narrows the `gh_commit_files` post-CAS verification contract so transient read-after-write staleness cannot be misreported as a known failed branch update.

This document records the invariants that must remain true for `gh_create_branch`, `gh_create_branch_from_sha`, and `gh_commit_files`.

## Shared exact-outcome contract

The migrated tools expose the shared `ExactWriteResult` outcome fields directly:

- `precondition_checked`: whether the required exact-state precondition ran successfully immediately before the mutation.
- `write_completed`: `True` for a known completed mutation, `False` for a known failure before completion, and `None` when transport or application behavior leaves mutation completion ambiguous.
- `readback_completed`: whether authoritative post-write evidence conclusively established whether the requested state matches.
- `state_matches_requested`: `True` or `False` only when authoritative readback is conclusive; otherwise `None`.
- `warning`: fail-closed ambiguity, readback, or semantic-mismatch guidance.
- `request_id`: GitHub request identity when supplied by the governed transport.

An ambiguous mutation is attempted exactly once. Authoritative readback may establish the resulting state, but it does not rewrite `write_completed=None` into known transport success or failure. Callers must not blindly replay an ambiguous write.

A syntactically successful read can still be inconclusive for a tool-specific postcondition. In that case `readback_completed=False` and `state_matches_requested=None` are truthful when the tool has not yet obtained conclusive authoritative evidence.

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

The ref mutation is never replayed. After that single CAS attempt, the tool performs bounded read-only reconciliation using fresh exact `git/ref/heads/<branch>` reads. The current bound is three exact-ref reads with small bounded backoff between provisional stale observations.

For a CAS whose transport/application completion is either known successful or ambiguous, the readback classification is:

- observing the newly created `commit_sha` conclusively verifies the requested branch state;
- observing the exact `previous_head_sha` is provisionally stale/inconclusive while reconciliation attempts remain;
- observing only `previous_head_sha` through the full bound leaves the branch update unresolved, with `ref_updated=None`, `readback_completed=False`, and `state_matches_requested=None`;
- observing a third SHA distinct from both the previous head and created commit is a conclusive semantic mismatch, with `ref_updated=False` and the observed SHA returned;
- a readback error or malformed exact-ref response fails closed as unverifiable and does not become a known failed branch update.

If the CAS is instead known to have failed before completion (`write_completed=False`), an authoritative read of the unchanged `previous_head_sha` is conclusive evidence that the requested new branch state was not installed. That known-failure path preserves the existing #60 semantics and does not consume the stale-read retry bound unnecessarily.

An immediate observation of the pre-write head is therefore not proof that a successful or transport-ambiguous CAS failed. Only authoritative observation of `commit_sha` establishes verified success. A distinct third-party head establishes a verified mismatch. Exhausted stale-old-head observations after successful or ambiguous CAS remain unresolved and require an external authoritative re-read before any retry decision.

For an ambiguous CAS transport/application result, `write_completed` remains `None` even when later reconciliation verifies `commit_sha`. Readback establishes final requested state; it does not retroactively establish transport completion.

`CommitFilesResult` includes the exact previous head, created commit/tree, tri-state `ref_updated`, final valid `observed_head_sha`, and exact-ref `readback_attempts` so callers can diagnose the result without an immediate second tool call.

## Public routing and regressions

`write_tool_schema.py` remains the host-facing schema/metadata facade. The three migrated public writes delegate directly to canonical implementations under `src/mcp_gh_server/tools/`, not to the legacy Git/repository adapters.

Regression coverage must include, at minimum:

- exact branch-from-SHA success, conflicting-ref failure, ambiguity, and authoritative ref readback;
- stale content head rejection before any content mutation;
- exact `beforeOid`/`afterOid` content CAS with `force=False`;
- immediate new-head content readback;
- stale old-head followed by new-head on the second or final bounded read;
- exhausted old-head-only reconciliation after successful or ambiguous CAS as unresolved rather than failed;
- known non-ambiguous CAS failure plus authoritative unchanged-head readback as a conclusive failed requested state;
- third-party head reconciliation as a conclusive mismatch;
- readback failure after an old-head observation as unresolved/fail-closed;
- ambiguous content CAS followed by eventual new-head verification or exhausted old-head ambiguity, without mutation replay;
- GraphQL application-error handling without replay;
- exact issue-linked branch association including target repository identity;
- an ambiguous issue-branch mutation followed by a same-ref/same-SHA linked branch from a different repository, which must produce `state_matches_requested=False` and no retry;
- public exact-outcome schema fields, `CommitFilesResult` reconciliation fields, and canonical facade provenance.

Async tests added for this contract rely on the repository's configured automatic asyncio mode and do not add explicit `@pytest.mark.asyncio` markers.
