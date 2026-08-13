# Public write-schema and host-legibility contract

This document defines the public MCP contract for the 21 GitHub write tools. It is a
schema and metadata contract only: it does not replace or relax the existing execution,
authorization, exact-state, mutation-attempt, or readback semantics.

## Composition boundary

`src/mcp_gh_server/write_tool_schema.py` is the canonical host-facing facade for every
public write. It owns:

- the public function signature used to generate the MCP input schema;
- the tool title and action-specific description;
- truthful MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, and
  `openWorldHint`).

Each facade wrapper delegates to the existing implementation in the legacy adapter or
exact-state tool module. Those implementation modules remain authoritative for write
gates, fine-grained action gates, GitHub mutation behavior, exact-state/SHA checks,
authoritative readback, ambiguity handling, and the no-blind-retry policy.

`src/mcp_gh_server/server.py` registers all public writes uniformly from
`PUBLIC_WRITE_TOOLS` and `WRITE_TOOL_METADATA`. Public write descriptions are not
replaced with compatibility text at the composition root. The historical
`gh_list_milestones` description is a read-only compatibility override and is unrelated
to this write contract.

## Host-facing schema requirements

A host must be able to classify the target and material effect from the advertised tool
without inferring hidden generic capabilities. Public write schemas therefore use
bounded, typed parameters appropriate to the operation, including:

- canonical owner and repository shapes;
- positive GitHub object identifiers;
- exact 40-character SHA patterns where immutable identity is required;
- bounded branch, tag, ref, title, body, description, and commit-message strings;
- finite enums for state/review/merge choices;
- bounded assignee, reviewer, label, workflow-input, and commit-file collections;
- bounded nested commit-file path/content/mode fields; and
- an explicit `REPO` or `OWNER/REPO` shape for repository creation.

Runtime validation may be stricter than JSON Schema where Git ref/path validity cannot be
expressed completely without changing accepted GitHub semantics. Runtime validators and
configured deployment limits remain authoritative after schema validation.

The public write surface must not expose a generic executor or host-policy escape hatch.
In particular, it does not add arbitrary shell/command/argv, arbitrary URL/API endpoint,
generic JSON payload, administrator/force/retry controls, or synthetic
confirmation/authorization/safety-justification parameters.

## Description and annotation requirements

Every public write has one action-specific description that states:

1. the single external GitHub effect;
2. important authorization, exact-state, or fine-gate preconditions; and
3. material capabilities the tool does not have, including separation from adjacent
   higher-risk actions when relevant.

Annotations remain semantic rather than host-policy workarounds:

- all 21 writes have `readOnlyHint=false`;
- additive writes have `destructiveHint=false`;
- state-changing/destructive writes have `destructiveHint=true`;
- writes do not claim idempotence; and
- GitHub writes remain open-world operations.

Changing annotations, removing exact-state constraints, or inventing confirmation fields
solely to influence a host classifier violates this contract.

## Execution invariants preserved behind the facade

Schema hardening does not authorize a second mutation path. Existing implementation
invariants continue to apply, including:

- global write authorization and repository allow policy;
- separate fine gates for repository creation, release creation, workflow dispatch,
  content commits, and pull-request merge;
- exact expected state/head/ref/target checks where the tool contract prescribes them;
- one governed mutation attempt for no-blind-retry operations;
- authoritative readback where a stable identity exists; and
- explicit ambiguous/partial-write reporting instead of automatic replay.

Legacy compatibility tools that intentionally lack an immutable precondition remain
truthful about that limitation and direct callers to the corresponding exact-state tool
where one exists.

## Host interception versus server defects

A host may reject or intercept a write before invoking `gh_mcp`. That is integration or
host-policy evidence, not evidence that the server rejected the request.

Use the server's invocation-reachability logging (or equivalent server-side evidence) to
determine whether an invocation reached `gh_mcp`. If it did not reach the server, do not
weaken annotations, schema constraints, descriptions, authorization gates, or exact-state
checks to force pass-through. If it did reach the server and the mutation outcome is
ambiguous, follow the tool's authoritative readback/no-blind-retry contract before any
subsequent action.

## Regression authority

The contract is enforced by complementary tests:

- `tests/test_tool_schema_snapshot.py` pins the 61-tool public surface and intentional
  schema/description snapshots;
- `tests/test_write_surface_contract.py` pins all 21 public write facades and their module
  provenance;
- `tests/test_write_schema_policy.py` independently enumerates the write surface, checks
  canonical metadata/annotations, recursively audits bounded schema leaves and nested
  objects, rejects generic executor/bypass fields, and pins high-risk exact-state and
  payload constraints; and
- write-wrapper tests verify that facade calls still delegate to the existing execution
  implementations.

A new public write or a material schema/annotation change must update the independent
policy set and the relevant exact snapshot intentionally. Green tests alone are not a
reason to weaken this contract.
