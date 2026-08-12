# Exact workflow dispatch contract

`gh_run_workflow_exact` is the release-grade workflow-dispatch primitive introduced for issue
#16. It remains separate from the compatibility-oriented `gh_run_workflow` tool.

## Safety sequence

For one `(owner, repo, workflow_id, expected_ref_sha)` dispatch key, the tool:

1. enforces the existing master write gate, repository/owner allowlist, and
   `MCP_GH_ALLOW_WORKFLOW_DISPATCH` fine gate;
2. enters a server-local critical section so two concurrent invocations in the same MCP server
   cannot both pass the duplicate check and POST;
3. reconciles any prior same-process reservation before a new attempt;
4. resolves the exact canonical `heads/...` or `tags/...` ref and requires its peeled commit SHA
   to equal `expected_ref_sha`;
5. queries `gh_list_runs` using the exact workflow ID, head SHA, and `workflow_dispatch` event;
6. rejects a same-name branch/tag counterpart because GitHub's dispatch API accepts only the
   short branch-or-tag name and cannot otherwise preserve the caller's namespace identity;
7. re-resolves the requested exact ref immediately before mutation;
8. sends one POST with `return_run_details=true` and never retries that mutation automatically;
9. when GitHub returns run details, reads back exactly the returned workflow-run ID and verifies
   its workflow ID, head SHA, and event;
10. when transport fails before a run ID is available, uses the exact filtered run query only as
    ambiguity readback and preserves `write_completed=None` when the transport outcome is unknown.

A successful or transport-ambiguous local attempt leaves a same-process reservation. This closes
the propagation window in which GitHub has accepted a dispatch but the workflow-runs list has not
yet exposed it. A later exact invocation cannot dispatch again merely because list discovery is
stale. A known run reservation is reconciled by exact run ID; if that run has been deleted, the
normal exact duplicate query is required before the reservation can be cleared. An unresolved
transport-ambiguous reservation remains fail-closed.

## GitHub API atomicity boundary

GitHub's workflow-dispatch REST endpoint accepts a branch or tag **name**. It does not expose a
server-side compare-and-swap precondition tying that name to a caller-supplied commit SHA. As a
result, no client can make the preflight ref read and the subsequent dispatch POST one atomic
GitHub operation.

`gh_run_workflow_exact` therefore does not claim an impossible atomic-ref guarantee. It minimizes
the race by re-reading the exact ref immediately before POST, requests the exact created run ID,
and verifies the returned run's head SHA. If the ref moves after the final preflight read but
before GitHub resolves the POST, the mutation is **not** reported as matching the request; the
returned run is surfaced with `state_matches_requested=false`, a warning explains the ref-movement
possibility, and no retry is attempted.

For release workflows where an external actor can move a branch or tag concurrently, callers
should use repository governance that makes the selected release ref operationally immutable.
The MCP server cannot manufacture an atomic GitHub ref precondition without additional repository
mutations or workflow-specific cooperation, both of which are outside issue #16's contract.

## Duplicate scope

The authoritative remote duplicate identity is:

- exact workflow ID;
- exact expected head SHA;
- `workflow_dispatch` event;
- any run status.

The server-local reservation uses the same workflow/head identity. It is an additional fail-closed
coordination layer, not a replacement for the authoritative `gh_list_runs` query. Coordination is
process-local; independently running MCP server processes and unrelated GitHub actors do not share
this in-memory reservation. Once GitHub exposes a run, the exact remote duplicate query provides
the cross-process evidence available from the public API.

## Readback and ambiguity

A normal successful dispatch must return `workflow_run_id`, `run_url`, and `html_url` from GitHub's
`return_run_details` response. The tool does not guess which run it created from a list query. If
GitHub confirms the POST but returns malformed/missing run-detail metadata, the result reports the
write as completed but the readback as incomplete.

For a transport-ambiguous POST, the tool performs one exact filtered re-read and never performs a
second POST. The standardized exact-write fields retain their existing meanings:

- `precondition_checked` — the exact-state precondition completed successfully before mutation;
- `write_completed` — `true`, `false`, or `null` when transport leaves completion unknown;
- `readback_completed` — authoritative readback completed;
- `state_matches_requested` — readback verified the requested semantic state;
- `warning` / `request_id` — ambiguity and GitHub request evidence.

## Regression requirements

Issue #16 coverage must include stale refs, existing duplicates, same-name branch/tag ambiguity,
annotated-tag peeling, successful returned-run readback, returned-run identity mismatch,
post-dispatch head mismatch, known dispatch failure, delayed readback, malformed run-detail
responses, transport ambiguity, multiple fallback matches, concurrent same-key invocation, write
fine-gate denial, duplicate workflow inputs, and the no-double-dispatch invariant.
