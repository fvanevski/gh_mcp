# Public write-schema and host-legibility contract

Version 0.9.0 exposes 20 public GitHub write tools (61 total public MCP tools: 41 read-only and 20 write) through one canonical host-facing MCP facade. This document defines their schema, metadata, registration, authorization, and ambiguity invariants. It does not weaken execution, exact-state, mutation-attempt, or readback requirements.

Historical 0.7.0, 0.7.1, 0.8.0, and 0.8.1 inventories remain recorded by their release documents. The current 0.9.0 runtime authority is defined by `docs/release_gate_0_9_0.md` and `tests/test_release_gate_0_9_0.py`.

## Composition boundary

`src/mcp_gh_server/current_write_tool_schema.py` is the canonical host-facing facade for every public write. It owns:

- the public function signature used to generate the MCP input schema;
- the tool title and action-specific description;
- truthful MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`); and
- delegation to the authoritative canonical domain implementation.

For the 0.9.0 formal review writes, the facade delegates to `src/mcp_gh_server/pr_review_tool_schema.py`, which owns the bounded review-write schemas and action-specific metadata. The pre-review-split writes remain defined by `src/mcp_gh_server/write_tool_schema.py`, whose shared bounded types and metadata the review schema imports.

`src/mcp_gh_server/server.py` registers each function in `PUBLIC_WRITE_TOOLS` exactly once using the corresponding `WRITE_TOOL_METADATA` entry. It performs no compatibility `remove_tool`/re-add rebinding. Public write implementations under `src/mcp_gh_server/tools/` do not independently register the same public names with `@mcp.tool`.

The obsolete `legacy_*write*` adapters, `legacy_write_support.py`, `legacy_assignee_support.py`, and the `legacy_write_status` result projection are not part of the 0.9.0 architecture.

Canonical domain implementations include:

- issue/label/milestone writes in `tools/issue_writes.py`;
- stable-ID comment creation in `tools/issues.py`;
- pull-request writes in `tools/pr_writes.py`;
- action-specific formal review writes (`gh_approve_pr`, `gh_request_pr_changes`, `gh_comment_pr_review`) in `tools/pr_review_writes.py`;
- exact issue-state and PR-draft transitions in their focused modules;
- repository creation in `tools/repository_create.py`;
- exact Git/reference writes in `tools/git_writes.py` and `tools/issue_branch_writes.py`;
- content commits in `tools/content_writes.py`;
- exact release creation in `tools/release_exact.py`; and
- exact workflow dispatch in `tools/workflow_dispatch.py`.

Canonical metadata-aware mutation helpers trust structured `GitHubRequestError` ambiguity metadata emitted by `GhClient`. They do not infer mutation ambiguity from bare exception-message text.

## Host-facing schema requirements

A host must be able to classify the target and material effect from the advertised tool without inferring hidden generic capabilities. Public write schemas therefore use bounded, typed parameters appropriate to the operation, including:

- canonical owner and repository shapes;
- positive GitHub object identifiers;
- exact 40-character SHA patterns where immutable identity is required;
- bounded branch, tag, ref, title, body, description, and commit-message strings;
- finite enums for state/review/merge choices;
- bounded assignee, reviewer, label, workflow-input, and commit-file collections;
- assignee elements limited to a canonical GitHub login or the exact supported `@me` selector, while reviewer elements remain canonical GitHub logins only;
- bounded nested commit-file path/content/mode fields;
- separate canonical `owner` and `repo` inputs for repository creation, identifying one exact prospective `OWNER/REPO` target before mutation; and
- exact workflow identity, canonical workflow path, exact ref path, and expected ref SHA for workflow dispatch.

`gh_run_workflow_exact` deliberately requires a positive workflow ID plus the exact canonical case-sensitive `.github/workflows/<file>.yml|yaml` path that the ID must identify immediately before dispatch. Path resolution or target-policy aliases never authorize a different workflow.

The `@me` exception is intentionally narrow. GitHub CLI accepts it for issue/PR assignee selection, and canonical implementations normalize it to the authenticated concrete login for authoritative readback. It is not accepted for review requests and must not be broadened into arbitrary symbolic selectors.

Runtime validation may be stricter than JSON Schema where Git ref/path validity cannot be expressed completely without changing accepted GitHub semantics. Runtime validators and configured deployment limits remain authoritative after schema validation.

The public write surface must not expose a generic executor or host-policy escape hatch. In particular, it does not add arbitrary shell/command/argv, arbitrary URL/API endpoint, generic JSON payload, administrator/force/retry controls, or synthetic confirmation/authorization/safety-justification parameters.

## Description and annotation requirements

Every public write has one action-specific description that states:

1. the single external GitHub effect;
2. important authorization, exact-state, or fine-gate preconditions; and
3. material capabilities the tool does not have, including separation from adjacent higher-risk actions when relevant.

A tool governed by a separate operation fine gate names that fine gate in its host-facing description.

Annotations remain semantic rather than host-policy workarounds:

- all 20 writes have `readOnlyHint=false`;
- additive writes have `destructiveHint=false`;
- state-changing/destructive writes have `destructiveHint=true`;
- writes do not claim idempotence; and
- GitHub writes remain open-world operations.

Changing annotations, removing exact-state constraints, or inventing confirmation fields solely to influence a host classifier violates this contract.

## Execution invariants behind the facade

Schema hardening does not authorize a second mutation path. Canonical execution invariants include:

- global write authorization and repository/owner allow policy;
- separate fine gates for repository creation, release creation, workflow dispatch, content commits, and pull-request merge;
- exact prospective-repository authorization for repository creation;
- exact repository/workflow authorization for workflow dispatch before mutation;
- exact expected state/head/ref/target checks where prescribed;
- preservation of `@me` assignee normalization for issue/PR create and assignee edits;
- one governed mutation attempt for no-blind-retry operations;
- authoritative readback where a stable identity exists; and
- explicit ambiguous/partial-write reporting instead of automatic replay.

Repository creation requires authoritative readback evidence for repository identity (`nameWithOwner`), visibility (`isPrivate`), and description. Explicit JSON `null` is authoritative evidence for no description; an absent `description` field is incomplete evidence. Initialization may remain unknown when GitHub does not expose boolean `isEmpty` evidence.

Repository creation and workflow dispatch require their operation-specific exact target policy. Their exact target lists default to empty. Enabling a fine gate without a matching required target remains fail-closed. Release creation intentionally keeps master write gate + release fine gate + ordinary repository policy without inventing a release-target list.

`gh_create_comment` performs one issue-comments REST mutation and reads back the immutable returned comment ID, repository/issue association, canonical URLs, and requested body. A response without stable comment identity is not reported as verified success.

`gh_create_release_exact` uses the exact target commit, tag/release absence policy, one governed creation attempt, exact tag resolution, release identity/mode verification, and explicit latest-state verification.

`gh_run_workflow_exact` verifies exact workflow ID/path identity, active workflow state, exact ref/SHA, same-name branch/tag ambiguity, duplicate run state, one dispatch attempt, and authoritative readback bound to the returned run ID. It never automatically redispatches an ambiguous operation.

`gh_commit_files` conditionally advances exactly one existing branch using exact `beforeOid`/`afterOid` compare-and-swap semantics and never force-updates the ref.

## Ambiguity authority

Real `GhClient` executions produce structured `GitHubRequestError` metadata. Canonical write helpers preserve that structured ambiguity classification directly.

A bare `RuntimeError` is not reinterpreted as an ambiguous completed write based on text such as “timeout” or “connection reset.” This prevents message-string heuristics from upgrading uncertainty into an invented transport classification.

When a structured write result is ambiguous, `write_completed` remains `null`. Authoritative readback may establish whether requested state exists, but it does not change the historical transport outcome. Do not retry automatically; re-read authoritative state first.

## Host interception versus server defects

A host may reject or intercept a write before invoking `gh_mcp`. That is integration or host-policy evidence, not evidence that the server rejected the request.

Classify representative live replay separately as:

1. intercepted before `gh_mcp` invocation;
2. reached `gh_mcp` and was rejected by a server gate/precondition;
3. reached GitHub and failed or returned ambiguity; or
4. completed with authoritative readback.

Do not weaken annotations, schema constraints, descriptions, authorization gates, or exact-state checks solely to force host pass-through.

## Regression authority

The contract is enforced by complementary tests:

- `tests/test_release_gate_0_9_0.py` pins package/lock/runtime versions, exact 61/41/20 inventory, retired-tool absence, single canonical write registration, compatibility-path removal, default-off fine gates, and release documentation;
- `tests/test_tool_schema_snapshot.py` pins the complete current tool schema surface;
- `tests/test_write_surface_contract.py` pins all 20 public write facades and canonical module provenance;
- `tests/test_write_schema_policy.py` audits bounded schemas, annotations, generic-executor/bypass exclusions, the narrow `@me` selector, high-risk descriptions, and exact-state/payload constraints;
- `tests/test_write_target_policy.py` pins exact high-risk target gates and zero-call target mismatches;
- domain migration tests pin canonical direct tri-state outcomes and no-blind-retry behavior; and
- `tests/test_write_transport_metadata.py` pins structured ambiguity authority and rejection of bare-`RuntimeError` text inference.

A new public write or material schema/annotation change must update the independent policy set, release inventory, and relevant schema snapshots intentionally. Green tests are not authority to weaken this contract.
