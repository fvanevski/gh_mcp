# Release gate 0.8.0

Version 0.8.0 exposes 58 public MCP tools: 40 read-only and 18 write.

This document is the release-authority record for the 0.8.0 write-contract cleanup. Historical 0.7.0 and 0.7.1 release documents remain immutable records of their shipped surfaces; they do not constrain the current 0.8.0 runtime inventory.

## Scope

0.8.0 integrates issues #52 through #60 and completes issue #61. Issue #6 remains deferred and is not part of this release gate.

The release removes obsolete write compatibility infrastructure, makes the canonical host-facing write facade the sole MCP registration path for public writes, and versions the breaking public-surface changes introduced during the 0.8.0 remediation sequence.

## Public write inventory

The 18 public writes are:

- `gh_create_issue`
- `gh_edit_issue`
- `gh_set_issue_state`
- `gh_create_label`
- `gh_edit_label`
- `gh_create_milestone`
- `gh_create_comment`
- `gh_create_pr`
- `gh_edit_pr`
- `gh_set_pr_draft_state`
- `gh_submit_pr_review`
- `gh_merge_pr`
- `gh_create_repo`
- `gh_commit_files`
- `gh_create_release_exact`
- `gh_run_workflow_exact`
- `gh_create_branch`
- `gh_create_branch_from_sha`

The weaker historical public writes `gh_run_workflow`, `gh_create_release`, and `gh_upsert_label` are retired. They are not aliases, compatibility shims, or hidden public registrations in 0.8.0.

## Canonical registration invariant

`src/mcp_gh_server/server.py` imports the canonical wrappers from `write_tool_schema.py` and registers each public write exactly once with `mcp.add_tool`. It contains no `mcp.remove_tool` compatibility rebinding.

Domain implementation modules under `src/mcp_gh_server/tools/` do not independently decorate public write functions with `@mcp.tool`. Read-only tools retain their ordinary domain registration.

Obsolete `legacy_*write*` modules, `legacy_write_support.py`, `legacy_assignee_support.py`, and the `legacy_write_status` compatibility projection are absent from the release architecture.

## Write-contract invariant

Every remaining public write must retain all of the following:

- a bounded, host-legible input schema;
- truthful MCP annotations that distinguish additive and destructive mutations;
- the master write policy plus any operation-specific fine gate and exact target policy;
- exact state, ref, SHA, or identity preconditions where the operation admits one;
- at most one mutation attempt for a caller invocation;
- authoritative readback when GitHub exposes a stable identity;
- explicit tri-state outcome metadata for writes whose completion can be transport-ambiguous;
- no blind replay after an ambiguous result;
- no classifier-only confirmation parameter and no metadata claim that falsifies the operation's mutation semantics.

`gh_create_comment` reads back the immutable returned comment ID. `gh_run_workflow_exact` binds dispatch/readback to exact workflow, ref/SHA, and returned run identity. `gh_create_release_exact` binds the release and tag to the requested immutable target commit. Repository creation uses the exact prospective `OWNER/REPO` target policy. Content writes preserve exact branch-head compare-and-swap behavior.

## Fine-gate defaults

The following remain disabled by default:

- master write execution;
- repository creation;
- release creation;
- exact workflow dispatch;
- repository-content commits;
- pull-request merge.

Repository-creation and workflow-dispatch exact target lists also default to empty. Enabling a fine gate without its required exact target policy does not broaden authorization.

## Ambiguity and replay policy

A write whose transport outcome is unknown remains `write_completed=null` unless the operation can prove a known failure. Subsequent authoritative readback may establish whether the requested state exists, but it does not retroactively prove that the transport completed cleanly.

Do not retry an ambiguous mutation automatically. Re-read authoritative state first. A host, client, or caller must not convert transport ambiguity into a second write attempt merely because the original response was interrupted.

## Host replay classification

Live ChatGPT/OpenAI replay is integration evidence, not a substitute for source/server correctness. Record every attempted representative write in one of four categories:

1. **Pre-server host interception** — the host blocked the action before `gh_mcp` invocation.
2. **Server rejection** — the invocation reached `gh_mcp` and a fine gate, target policy, or exact precondition rejected it.
3. **GitHub failure or ambiguity** — the invocation reached GitHub but GitHub rejected it or the mutation result became indeterminate.
4. **Completed with authoritative readback** — the mutation completed and the server verified the stable resulting identity/state.

Residual host interception is not an `gh_mcp` implementation defect by itself. Host interception and server behavior must be reported separately. Truthful annotations, destructive/additive semantics, exact target constraints, and safety contracts must not be weakened merely to alter host classification.

Representative replay for final closure covers:

- issue or pull-request comment creation;
- issue/label/milestone metadata editing;
- a pull-request write at an exact target where applicable;
- exact workflow dispatch;
- exact release creation;
- allowlisted repository creation.

Use disposable targets for destructive or externally persistent replay evidence.

## Release validation

Validation is bound to one exact candidate SHA. Any source change invalidates affected results.

Required commands are:

```bash
uv run pytest tests/test_release_gate_0_8_0.py \
  tests/test_write_schema_policy.py \
  tests/test_tool_schema_snapshot.py \
  tests/test_tool_return_models.py \
  tests/test_mcp_protocol.py

uv run pytest tests/test_comment_create_exact.py \
  tests/test_issue_write_migration.py \
  tests/test_pr_write_migration.py \
  tests/test_git_content_write_migration.py \
  tests/test_git_content_write_schema.py \
  tests/test_repository_create_exact.py \
  tests/test_release_exact.py \
  tests/test_workflow_dispatch_exact.py \
  tests/test_workflow_dispatch_exact_cancellation.py \
  tests/test_write_target_policy.py \
  tests/test_write_transport_metadata.py \
  tests/test_write_contracts.py

uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

The release passes only when package version, server version, tool-schema version, `uv.lock`, runtime inventory, schema snapshots, documentation, static checks, focused negative/fail-closed regressions, and the full suite agree on the same exact candidate SHA.

## Non-goals

0.8.0 does not add arbitrary public `gh api`, arbitrary `gh <args...>`, a generic shell/subprocess MCP tool, administrator merge bypass, automatic mutation replay, artifact/log deletion, or branch-protection/ruleset mutation.
