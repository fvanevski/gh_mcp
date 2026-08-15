# Release gate 0.8.1

This document is the immutable historical acceptance record for the shipped 0.8.1 public
MCP surface. It supplements, rather than rewrites, the current runtime authority defined
separately by any later release gate. Current release authority is defined by
`docs/release_gate_0_8_1.md` plus `tests/test_release_gate_0_8_1.py`.

## Version and inventory authority

Version 0.8.1 exposes 58 public MCP tools: 40 read-only and 18 write.

For the released 0.8.1 candidate, the authoritative sources were:

- package version: `pyproject.toml`;
- server/tool-schema version: `src/mcp_gh_server/__init__.py`, consumed by `gh_server_info`;
- locked editable package version: `uv.lock`;
- executable tool inventory: `src/mcp_gh_server/server.py` / `mcp.list_tools()`;
- exact public input/output schema snapshot: `tests/test_tool_schema_snapshot.py` and
  `tests/test_tool_return_models.py`; and
- release integration assertions in `tests/test_release_gate_0_8_1.py`.

Those sources all reported `0.8.1` for the released candidate. This document records that
historical state; it does not require later releases to retain the same runtime version or
public inventory.

## Scope

0.8.1 is a narrow patch release addressing two post-release live-read regressions surfaced
after the 0.8.0 tag was cut. It integrates issue #72 and does not change the write
surface, write-tool inventory, or write authorization contracts.

The two remediated defects:

1. **`gh_search_code` (and all `gh_search_*` tools) failed on usable partial Search evidence.**
   The shared search helper treated `incomplete_results=true` from the matching Search REST
   count request as a fatal tool error, discarding bounded items already returned by `gh
   search`. The revised contract preserves structurally valid but incomplete count evidence,
   returns the item set, and sets `truncated=true`. Malformed count evidence still fails
   closed.

2. **`gh_list_issues(labels=...)` emitted an unsupported CLI flag.**
   The implementation used `--labels`; the GitHub CLI option is singular `--label`. The PR
   changes only that argv spelling and preserves the public comma-separated `labels` input.

Additional follow-up hardening: during diagnosis, `gh_search_issues` also exposed a case
where a later count could be lower than the number of items already returned. The shared
helper now reconciles `total_count = max(reported_total_count, len(items))` conservatively
and sets `truncated=true` rather than discarding items. Complete, consistent count
responses retain their existing behavior.

## Public write inventory

Unchanged from 0.8.0. The 18 public writes are:

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

The weaker historical public writes `gh_run_workflow`, `gh_create_release`, and
`gh_upsert_label` remain retired. They are not aliases, compatibility shims, or hidden
public registrations in 0.8.1.

## Read-plane contract changes

The following read-plane behaviors changed in 0.8.1:

- All three `gh_search_*` tools preserve items when the upstream count indicates
  `incomplete_results=true` and set `truncated=true` instead of raising.
- When a later non-atomic count report falls below already-returned items, the helper
  reconciles upward (`total_count = max(reported, len(items))`) and marks truncated.
- Malformed count evidence (non-object response, non-boolean `incomplete_results`,
  negative/non-integer `total_count`) continues to fail closed.
- `gh_list_issues(labels=...)` constructs argv with singular `--label` per the GitHub
  CLI contract.

These changes preserve:

- public tool names;
- the 58/40/18 inventory;
- the `SearchResults` schema;
- read/write annotations;
- any write implementation;
- write authorization or fine gates;
- historical 0.7.x/0.8.0 release-gate records.

## Test/documentary additions

- `tests/test_search_total_count.py` — covers complete counts, upstream
  `incomplete_results=true`, non-atomic count drift below returned item count, malformed
  evidence, and query serialization across repository/issue/code search.
- `tests/test_issue_list_labels.py` — pins singular `--label` argv construction and
  rejects any regression to `--labels`; covers the no-filter argv path.
- `tests/test_integration.py` — optional authenticated GitHub CLI smoke test for `gh
  issue list --label`.
- `docs/search-read-contract.md` — defines the two-read search evidence contract,
  conservative `truncated` semantics, malformed-evidence behavior, and issue-label CLI
  mapping.
- `README.md` — documents the operational search-count and issue-label behavior.

## Validation

Validation is bound to one exact candidate SHA. Any source change invalidates affected
results.

Required commands are:

```bash
uv run pytest tests/test_release_gate_0_8_1.py \
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

The release passes only when package version, server version, tool-schema version,
`uv.lock`, runtime inventory, schema snapshots, documentation, static checks, focused
negative/fail-closed regressions, and the full suite agree on the same exact candidate SHA.

## Ambiguity and replay policy

A write whose transport outcome is unknown remains `write_completed=null` unless the
operation can prove a known failure. Subsequent authoritative readback may establish
whether the requested state exists, but it does not retroactively prove that the transport
completed cleanly.

Do not retry an ambiguous mutation automatically. Re-read authoritative state first. A
host, client, or caller must not convert transport ambiguity into a second write attempt
merely because the original response was interrupted.

## Host replay classification

Live ChatGPT/OpenAI replay is integration evidence, not a substitute for source/server
correctness. Record every attempted representative write in one of four categories:

1. **Pre-server host interception** — the host blocked the action before `gh_mcp` invocation.
2. **Server rejection** — the invocation reached `gh_mcp` and a fine gate, target policy,
   or exact precondition rejected it.
3. **GitHub failure or ambiguity** — the invocation reached GitHub but GitHub rejected it
   or the mutation result became indeterminate.
4. **Completed with authoritative readback** — the mutation completed and the server
   verified the stable resulting identity/state.

Residual host interception is not an `gh_mcp` implementation defect by itself. Host
interception and server behavior must be reported separately. Truthful annotations,
destructive/additive semantics, exact target constraints, and safety contracts must not be
weakened merely to alter host classification.

## Non-goals

0.8.1 does not add arbitrary public `gh api`, arbitrary `gh <args...>`, a generic
shell/subprocess MCP tool, administrator merge bypass, automatic mutation replay,
artifact/log deletion, or branch-protection/ruleset mutation.
