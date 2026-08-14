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

- canonical owner and repository shapes;
- positive GitHub object identifiers;
- exact 40-character SHA patterns where immutable identity is required;
- bounded branch, tag, ref, title, body, description, and commit-message strings;
- finite enums for state/review/merge choices;
- bounded assignee, reviewer, label, workflow-input, and commit-file collections;
- assignee elements limited to a canonical GitHub login or the exact compatibility
  selector `@me`, while reviewer elements remain canonical GitHub logins only;
- bounded nested commit-file path/content/mode fields;
- separate canonical `owner` and `repo` inputs for repository creation, identifying one
  exact prospective `OWNER/REPO` target before any GitHub request; and
- for workflow dispatch, one exact positive workflow ID or a bounded canonical,
  case-sensitive `.github/workflows/<file>.yml|yaml` path.

The workflow selector union is intentionally narrow. A numeric selector remains a positive
GitHub workflow ID. A path selector names a file directly under `.github/workflows/`; the
execution layer first authorizes that exact caller-supplied selector and then performs a
read-only GitHub workflow lookup by file name. The returned workflow metadata must preserve
the exact requested path and case before the numeric workflow ID may be used for duplicate
detection, mutation, reservation state, or readback. The path option therefore adds no
generic repository-path, URL, or API surface.

The `@me` exception is intentionally narrow. GitHub CLI accepts it for issue/PR assignee
selection, and the write contract normalizes it to the authenticated concrete login for
authoritative readback. It is not a generic login syntax, is not accepted for review
requests, and must not be broadened into arbitrary symbolic selectors.

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

A tool governed by a separate operation fine gate must name that fine gate in its own
host-facing description. Exact-state and compatibility variants do not inherit this
requirement from an adjacent tool's documentation; each advertised operation must be
self-describing to the host.

Annotations remain semantic rather than host-policy workarounds:

- all 18 current writes have `readOnlyHint=false`;
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
- exact prospective-repository authorization for repository creation;
- exact repository/workflow-selector authorization for workflow dispatch before any
  workflow-resolution read or mutation request;
- exact expected state/head/ref/target checks where the tool contract prescribes them;
- preservation of the supported `@me` assignee selector and its concrete-login readback
  normalization for issue/PR create and assignee edits;
- one governed mutation attempt for no-blind-retry operations;
- authoritative readback where a stable identity exists; and
- explicit ambiguous/partial-write reporting instead of automatic replay.

Repository creation requires explicit authoritative readback evidence for repository
identity (`nameWithOwner`), visibility (`isPrivate`), and description. An explicit JSON
`null` description is authoritative evidence for a repository with no description; an
absent `description` field is incomplete evidence and must not be treated as equivalent to
`null`. Initialization is the sole requested property that may remain unknown when GitHub
does not expose boolean `isEmpty` evidence.

Repository creation and workflow dispatch therefore require both their ordinary repository
policy and their operation-specific exact target policy. The exact target lists default to
empty. Enabling a fine gate without a matching exact target remains fail-closed. Release
creation intentionally keeps its existing contract: master write gate, release fine gate,
and normal repository policy, without a new release-target list.

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

- `tests/test_tool_schema_snapshot.py` pins the current 58-tool development surface and
  intentional schema/description snapshots;
- `tests/test_write_surface_contract.py` pins all 18 current public write facades and their
  module provenance;
- `tests/test_write_schema_policy.py` independently enumerates the write surface, checks
  canonical metadata/annotations, recursively audits bounded schema leaves and nested
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
