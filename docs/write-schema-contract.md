# Public write-schema and host-legibility contract

This document defines the current unreleased 0.8.0-development public MCP contract for the
18 GitHub write tools. It is a schema and metadata contract only: it does not replace or
relax the existing execution, authorization, exact-state, mutation-attempt, or readback
semantics. Released 0.7.x inventory counts remain governed by their immutable release-gate
documents and tests.

## Composition boundary

`src/mcp_gh_server/write_tool_schema.py` is the canonical host-facing facade for every
public write. It owns:

- the public function signature used to generate the MCP input schema;
- the tool title and action-specific description;
- truthful MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, and
  `openWorldHint`).

Each facade wrapper delegates to the authoritative execution implementation in a canonical
domain module, an exact-state tool module, or a still-frozen compatibility adapter. Those
implementation modules remain authoritative for write gates, fine-grained action gates,
GitHub mutation behavior, exact-state/SHA checks, authoritative readback, ambiguity
handling, and the no-blind-retry policy.

Issue-domain create/edit writes are canonicalized in `tools/issue_writes.py` and return the
shared tri-state write/readback metadata directly. The legacy `gh_upsert_label` public
surface is retired: label creation never overwrites an existing label, while label edits
remain an explicit separate operation.

Canonical metadata-aware mutation helpers trust structured `GitHubRequestError` ambiguity
metadata emitted by the real `GhClient`; they do not infer ambiguity from bare exception
message text. Frozen legacy adapters may still normalize transport-like bare `RuntimeError`
messages for non-`GhClient` compatibility test doubles. Those doubles are deliberately
exercised through their historical `run()` interface even when they expose a convenience
`run_with_metadata()` method, so test compatibility cannot alter the production `GhClient`
contract.

`src/mcp_gh_server/server.py` registers all public writes uniformly from
`PUBLIC_WRITE_TOOLS` and `WRITE_TOOL_METADATA`. Public write descriptions are not
replaced with compatibility text at the composition root. The historical
`gh_list_milestones` description is a read-only compatibility override and is unrelated
to this write contract.

## Host-facing schema requirements

A host must be able to classify the target and material effect from the advertised tool
without inferring hidden generic capabilities. Public write schemas therefore use
bounded, typed parameters appropriate to the operation, including:

- canonical repository-owner and repository-name patterns;
- exact 40-character SHA patterns for immutable object identities;
- positive integer bounds for GitHub object identifiers;
- bounded strings, arrays, maps, and file payloads;
- finite enums for issue state, milestone state, review actions, and merge strategies;
- an explicit six-hex-digit label-color pattern;
- the bounded `@me` exception only for assignee selectors, without broadening reviewer
  logins;
- exact workflow identity as either a positive numeric workflow ID or canonical
  `.github/workflows/*.yml|yaml` path plus an exact expected resolved SHA; and
- no generic `args`, `argv`, shell, arbitrary API endpoint, arbitrary JSON payload,
  approval, confirmation, force, bypass, retry, or administrator fields.

High-risk writes additionally advertise the fine gate required by their execution path:
repository creation, release creation, workflow dispatch, repository-content commits, and
PR merge. Exact-state operations surface their expected state or SHA inputs directly rather
than hiding them behind server-side guessing.

## Regression authority

The independent schema-policy and protocol tests are release evidence, not generated
mirrors of the implementation:

- `tests/test_write_schema_policy.py` maintains an independent expected write inventory,
  verifies exact metadata and annotations, recursively checks bounded schemas and nested
  objects, rejects generic executor/bypass fields, pins the narrow `@me` assignee-selector
  exception without loosening reviewer logins, requires each high-risk write description
  to name its actual fine gate, and pins exact-state/payload constraints;
- `tests/test_write_target_policy.py` pins the new high-risk target gates, including
  zero-GitHub-call target mismatches, public ID-or-path workflow schema, exact case-preserving
  path resolution to numeric workflow identity, release-gate preservation, and unchanged
  ordinary repository policy; and
- write-wrapper tests verify that facade calls still delegate to the authoritative execution
  implementations, including symbolic-assignee readback semantics.

Repository-creation regression coverage additionally distinguishes required readback fields
from optional initialization evidence: missing identity, visibility, or description must
produce a semantic mismatch, while missing initialization evidence leaves `initialized`
unknown rather than inventing state.

Async schema-policy and target-policy tests follow the repository's
`asyncio_mode = "auto"` convention and do not carry explicit `@pytest.mark.asyncio`
decorators.

A new public write or a material schema/annotation change must update the independent
policy set and the relevant exact snapshot intentionally. Green tests alone are not a
reason to weaken this contract.
