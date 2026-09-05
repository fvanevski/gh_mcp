# Exact workflow dispatch contract

`gh_run_workflow_exact` is the sole public workflow-dispatch write. Issue #55 retires the weaker
`gh_run_workflow` surface and hardens the exact tool without weakening its existing exact-ref,
duplicate, mutation-attempt, reservation, ambiguity, or readback semantics.

## Workflow identity and authorization

The public tool requires both:

- `workflow_id`: a positive numeric GitHub workflow ID; and
- `expected_workflow_path`: the exact, case-sensitive canonical path
  `.github/workflows/<file>.yml` or `.github/workflows/<file>.yaml` expected to identify that ID.

Before any GitHub read or mutation request, the server enforces the master write gate, normal
repository/owner policy, `MCP_GH_ALLOW_WORKFLOW_DISPATCH`, and exact
`MCP_GH_ALLOWED_WORKFLOW_DISPATCH_TARGETS` policy. The exact target list defaults to empty and
therefore fails closed.

An allowlist entry may name either the numeric workflow ID or the canonical workflow path. This
does not create an aliasing fallback: immediately before the mutation, the server re-reads the
numeric workflow endpoint and requires GitHub to return the caller-supplied `workflow_id`, the
exact `expected_workflow_path`, and `state="active"`. Therefore either configured identity can
authorize only the verified ID/path pair; a mismatched ID, mismatched path, or inactive workflow
performs no mutation.

The verified numeric workflow ID is used for duplicate detection, the server-local reservation
key, dispatch endpoint construction, and authoritative run readback.

## Workflow inputs

Optional `inputs` are a typed JSON object mapping string keys to string values. The public schema
and runtime contract reject:

- more than 25 input entries;
- empty or non-string keys;
- non-string values; and
- more than 65,535 aggregate key-plus-value characters across the object.

The server does not expose generic `fields: list[str]`, arbitrary JSON payloads, or command-line
argument forwarding for workflow dispatch.

## Safety sequence

For one authorized `(owner, repo, workflow_id, expected_workflow_path, expected_ref_sha)` request,
the tool:

1. enforces the master write gate, repository/owner allowlist,
   `MCP_GH_ALLOW_WORKFLOW_DISPATCH`, and exact repository/workflow target policy;
2. validates the bounded typed input object before GitHub discovery;
3. enters a server-local critical section so two concurrent invocations in the same MCP server
   cannot both pass the duplicate check and POST;
4. reconciles any prior same-process reservation before a new attempt;
5. resolves the exact canonical `heads/...` or `tags/...` ref and requires its peeled commit SHA
   to equal `expected_ref_sha`;
6. queries `gh_list_runs` first with the exact workflow ID, head SHA, `workflow_dispatch` event,
   and `status=completed`, then repeats the same exact query without a status filter; it rejects
   when the resulting counts prove that at least one matching run is still nonterminal, and when
   positive counts are equal it re-reads the completed count and requires that terminal population
   to remain stable before dispatching; counter-regression, replacement, and search-boundary races
   are uncertain and perform no mutation;
7. rejects a same-name branch/tag counterpart because GitHub's dispatch API accepts only the
   short branch-or-tag name and cannot otherwise preserve the caller's namespace identity;
8. re-resolves the requested exact ref immediately before mutation;
9. re-reads the exact numeric workflow immediately before mutation and requires the exact
   caller-supplied ID/path pair plus `state="active"`;
10. sends one POST to the numeric workflow dispatch endpoint with `return_run_details=true` and
    the validated typed `inputs` object when present, and never retries that mutation
    automatically;
11. when GitHub returns run details, reads back exactly the returned workflow-run ID and verifies
    its workflow ID, head SHA, and `workflow_dispatch` event; and
12. when transport fails before a run ID is available, uses the exact filtered run query only as
    ambiguity readback and preserves `write_completed=None` when the transport outcome is unknown.

A successful or transport-ambiguous local attempt leaves a same-process reservation. This closes
the propagation window in which GitHub has accepted a dispatch but the workflow-runs list has not
yet exposed it. A later exact invocation cannot dispatch again merely because list discovery is
stale. A known run reservation is reconciled by exact run ID: while that run is nonterminal it
blocks another dispatch; once its status is `completed`, the local reservation is released and the
normal exact remote precondition decides whether another intentional dispatch may proceed. If a
known reserved run has been deleted, the normal exact duplicate query is required before the
reservation can be cleared. An unresolved transport-ambiguous reservation remains fail-closed.

## GitHub API atomicity boundary

GitHub's workflow-dispatch REST endpoint accepts a branch or tag **name**. It does not expose a
server-side compare-and-swap precondition tying that name to a caller-supplied commit SHA. The
workflow metadata precondition likewise cannot be made atomic with the subsequent dispatch POST.

`gh_run_workflow_exact` therefore does not claim impossible GitHub-side atomicity. It minimizes the
ref race by re-reading the exact ref immediately before the final workflow metadata precondition,
then performs the single POST. It requests the exact created run ID and verifies the returned
run's workflow ID, event, and head SHA. If the ref moves after the final preflight read but before
GitHub resolves the POST, the mutation is **not** reported as matching the request; the returned
run is surfaced with `state_matches_requested=false`, a warning explains the ref-movement
possibility, and no retry is attempted.

For release workflows where an external actor can move a branch or tag concurrently, callers
should use repository governance that makes the selected release ref operationally immutable.
The MCP server does not manufacture an atomic GitHub ref precondition by performing additional
repository mutations or requiring workflow-specific cooperation.

## Duplicate scope

The authoritative remote duplicate identity is:

- exact numeric workflow ID;
- exact expected head SHA;
- `workflow_dispatch` event; and
- a nonterminal run status.

Historical `completed` runs do not permanently reserve a trusted controller/head. The tool derives
this without scanning historical pages: it reads the `status=completed` total first, then the total
count from the same exact workflow/head/event query without a status filter. A smaller completed
count proves at least one matching run is still nonterminal and therefore blocks the mutation.
Reading completed history first makes a concurrent new dispatch that appears between those probes
increase the later all-status count and therefore fail closed instead of being missed. A completed
count larger than the later all-status count is treated as uncertain state, such as a concurrent
deletion, and no mutation is attempted.

When the first two counts are equal and positive, equality alone is not sufficient evidence: a
completed run could have been deleted and replaced by a nonterminal run while preserving the same
total. The tool therefore performs a second exact `status=completed` count and requires it to equal
the first completed count. Any change in that terminal population is `UNCERTAIN` and no mutation is
attempted. This closes the observed equal-count replacement race while preserving the completed-
history allowance. As with the exact-ref precondition, GitHub does not expose a server-side atomic
compare-and-dispatch primitive; an unrelated actor can always mutate workflow-run state after the
last observation and before GitHub processes the POST, so the tool makes no stronger atomicity
claim.

GitHub limits filtered workflow-run searches to 1,000 results. If the initial completed count, the
all-status count, or the terminal recheck reaches that boundary, the tool cannot prove that the
observed history is complete and stable, so it returns an uncertain precondition and performs no
mutation rather than inferring that all matching runs are terminal.

The server-local reservation uses the same workflow/head identity. It is an additional fail-closed
coordination layer, not a replacement for the authoritative `gh_list_runs` query. Coordination is
process-local; independently running MCP server processes and unrelated GitHub actors do not share
this in-memory reservation. A known local reservation remains blocking while its exact run ID is
nonterminal and is released after that run becomes `completed`; an unresolved transport outcome
remains fail-closed regardless of filtered discovery.

## Readback and ambiguity

A normal successful dispatch must return `workflow_run_id`, `run_url`, and `html_url` from GitHub's
`return_run_details` response. The tool does not guess which run it created from a list query. If
GitHub confirms the POST but returns malformed or missing run-detail metadata, the result reports
the write as completed but the readback as incomplete.

For a transport-ambiguous POST, filtered fallback readback is permitted only when the exact
pre-dispatch workflow/head/event query proved that there was **no** matching historical run. If
completed matching history already existed, an identity-less post-write list query could not prove
which run (if any) came from the attempted mutation, so readback remains incomplete and the local
reservation stays fail-closed. This prevents a pre-existing completed run from being misreported as
the result of the new attempt.

When that zero-history condition holds, the tool performs one exact filtered re-read and never
performs a second POST. The standardized exact-write fields retain their existing meanings:

- `precondition_checked` — the exact-state precondition completed successfully before mutation;
- `write_completed` — `true`, `false`, or `null` when transport leaves completion unknown;
- `readback_completed` — authoritative readback completed;
- `state_matches_requested` — readback verified the requested semantic state; and
- `warning` / `request_id` — ambiguity and GitHub request evidence.

Successful exact readback also exposes `run_id`, `workflow_id`, `run_event`, and `run_head_sha` so
the caller receives the identities used to verify the created `workflow_dispatch` run.

## Regression requirements

Issue #55 coverage requires:

- the generic `gh_run_workflow` name to be absent from the public MCP registry and schema snapshot;
- wrong or disallowed workflow identity, stale ref SHA, same-name branch/tag ambiguity, and an
  existing matching nonterminal dispatch to fail before mutation while completed history does not;
- malformed input objects, more than 25 inputs, and aggregate input character overflow to fail
  before mutation;
- annotated-tag peeling and the final exact-ref precondition to remain intact;
- workflow ID/path/active-state verification to be the last GitHub precondition read before POST;
- successful readback to bind to the exact returned run ID and verify workflow ID,
  `workflow_dispatch` event, and head SHA;
- malformed run-detail responses, returned-run mismatch, delayed readback, transport ambiguity,
  historical-run fallback non-reuse, multiple fallback matches, concurrent same-key invocation,
  completed local-reservation release, equal-count completed-to-nonterminal replacement races,
  terminal-recheck search saturation, and cancellation to preserve the no-blind-retry invariant;
  and
- the master write gate, workflow-dispatch fine gate, exact repository/workflow target policy, and
  same-process reservation behavior to remain fail-closed.

Issue #6 remains outside this contract: exact workflow rerun/cancel is not introduced here.
