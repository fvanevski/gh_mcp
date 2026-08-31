# Public write-schema and host-legibility contract

Version 0.9.0 exposes 21 public GitHub write tools (63 total public MCP tools: 42 read-only and 21 write) through one canonical current host-facing registry. This document defines their schema, metadata, registration, authorization, and ambiguity invariants. It does not weaken execution, exact-state, mutation-attempt, or readback requirements.

Historical 0.7.0, 0.7.1, 0.8.0, and 0.8.1 inventories remain recorded by their release documents. The current 0.9.0 runtime authority is defined by `docs/release_gate_0_9_0.md` and `tests/test_release_gate_0_9_0.py`.

## Composition boundary

`src/mcp_gh_server/current_write_tool_schema.py` is the canonical current registry for every public write. It owns the exact set of functions and metadata registered by `server.py`, while focused facades own their public signatures and action-specific metadata:

- the established non-review writes are sourced from `src/mcp_gh_server/write_tool_schema.py`;
- `gh_patch_files` is sourced explicitly from `src/mcp_gh_server/patch_write_schema.py`; and
- the three action-specific formal-review writes are sourced from `src/mcp_gh_server/pr_review_tool_schema.py`.

The focused facades own:

- the public function signature used to generate the MCP input schema;
- the tool title and action-specific description;
- truthful MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`); and
- delegation to the authoritative canonical domain implementation.

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
- complete-content commits in `tools/content_writes.py`;
- exact-context existing-file patches in `tools/patch_writes.py`;
- the shared materialized-content object/CAS/readback state machine in `content_commit_service.py`;
- exact release creation in `tools/release_exact.py`; and
- exact workflow dispatch in `tools/workflow_dispatch.py`.

`gh_commit_files` and `gh_patch_files` deliberately have different request shapes but converge on `content_commit_service.py` after validation/materialization. There is no second public or internal content-CAS state machine for patch writes.

Canonical metadata-aware mutation helpers trust structured `GitHubRequestError` ambiguity metadata emitted by `GhClient`. They do not infer mutation ambiguity from bare exception-message text.

## Host-facing schema requirements

A host must be able to classify the target and material effect from the advertised tool without inferring hidden generic capabilities. Public write schemas therefore use bounded, typed parameters appropriate to the operation, including:

- canonical owner and repository shapes;
- positive GitHub object identifiers;
- exact 40-character SHA patterns where immutable identity is required;
- bounded branch, tag, ref, title, body, description, and commit-message strings;
- finite enums for state/review/merge choices;
- bounded assignee, reviewer, label, workflow-input, complete-file, file-patch, and exact-context edit collections;
- assignee elements limited to a canonical GitHub login or the exact supported `@me` selector, while reviewer elements remain canonical GitHub logins only;
- bounded nested complete-file path/content/mode fields for `gh_commit_files`;
- bounded nested patch path plus `old_text`/`new_text` fields for `gh_patch_files`;
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

A tool governed by a separate operation fine gate names that fine gate in its host-facing description. Both `gh_commit_files` and `gh_patch_files` therefore advertise the content-commit fine gate.

Annotations remain semantic rather than host-policy workarounds:

- all 21 writes have `readOnlyHint=false`;
- additive writes have `destructiveHint=false`;
- state-changing/destructive writes, including both repository-content writes, have `destructiveHint=true`;
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

### Repository content writes

`gh_commit_files` accepts complete UTF-8 file contents and conditionally advances exactly one existing branch using exact `beforeOid`/`afterOid` compare-and-swap semantics. It never force-updates the ref. Its existing complete-replacement public input/output contract remains unchanged by issue #80.

`gh_patch_files` is the focused existing-file partial-edit primitive. Before any content Git object is created it must:

- verify the branch is at the exact caller-supplied `expected_head_sha`;
- resolve every target from that immutable commit/tree snapshot;
- accept only existing `100644` or `100755` UTF-8 text blobs;
- reject symlinks, unsupported modes, NUL/binary content, and non-UTF-8 targets;
- require every `old_text` to occur exactly once in the original file;
- resolve every edit for a file against that original immutable content rather than incrementally modified content;
- reject overlapping original source spans;
- preserve the original supported file mode; and
- materialize and size-check every requested file.

After materialization, `gh_patch_files` re-reads the mutable branch immediately before the first Git-object write. Head movement during validation therefore fails before creating patch blobs/tree/commit objects. Validated materialized files then enter the same `content_commit_service.py` path used by `gh_commit_files`: one set of blobs, one tree, one commit, exactly one `updateRefs` compare-and-swap with exact `beforeOid`, requested `afterOid`, and `force=false`, followed by the issue #73 bounded exact-ref reconciliation/readback contract.

A multi-file patch request never creates separate commits or separate CAS attempts per file. An ambiguous CAS is never replayed. `gh_patch_files` does not create, delete, rename, or fuzzily match files and does not expose a mode-change operation. See `docs/gh_patch_files.md` for the complete exact-context contract and result evidence.

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

## Static-validation authority

Pyrefly is the sole Python static type-check authority for current CI/review/release decisions. `requirements-typecheck.txt` pins the checker and `[tool.pyrefly]` in `pyproject.toml` defines the no-argument project scope. Because the historical test corpus is excluded from that project scope, every changed Python test is also passed explicitly to Pyrefly during review.

Dynamic MCP schemas expose optional output schemas at the SDK type level. Tests that inspect an output schema must narrow/assert non-`None` schema state explicitly rather than adding Pyrefly ignores, broad suppressions, or a baseline. Mypy is not a release-validation substitute.

## Regression authority

The contract is enforced by complementary tests:

- `tests/test_release_gate_0_9_0.py` pins package/lock/runtime versions, exact 63/42/21 inventory, retired-tool absence, single canonical write registration, compatibility-path removal, default-off fine gates, current static authority, and current documentation;
- `tests/test_tool_schema_snapshot.py` pins the complete current 63-tool schema surface and uses explicit output-schema narrowing for changed-test Pyrefly validation;
- `tests/test_write_surface_contract.py` pins all 21 public write facades and canonical module provenance;
- `tests/test_write_schema_policy.py` independently pins all 21 public write names, audits bounded schemas and annotations, requires the content-commit fine-gate marker for both content writes, excludes generic executor/bypass fields, and pins exact-state/payload constraints;
- `tests/test_git_content_write_schema.py` pins the shared exact outcome/reconciliation evidence, the exact `gh_patch_files` request/evidence shape, and the unchanged `gh_commit_files` public request shape;
- `tests/test_patch_files.py` pins unique and multiline replacement, complete-line deletion, original-snapshot resolution, missing/non-unique/overlap rejection, stale and mid-validation head rejection, mode preservation, unsupported target rejection, multi-file one-commit/one-CAS semantics, and ambiguous-CAS reconciliation without replay;
- `tests/test_write_target_policy.py` pins exact high-risk target gates and zero-call target mismatches;
- domain migration tests pin canonical direct tri-state outcomes and no-blind-retry behavior; and
- `tests/test_write_transport_metadata.py` pins structured ambiguity authority, rejection of bare-`RuntimeError` text inference, and the current 63/42/21 documentation inventory.

A new public write or material schema/annotation change must update the independent policy set, release inventory, protocol inventory, documentation authority, and relevant schema snapshots intentionally. Green tests are not authority to weaken this contract.
