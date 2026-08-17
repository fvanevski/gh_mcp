# Release gate 0.9.0

This document is the current acceptance record for the 0.9.0 public MCP surface. It defines
the release authority for the current runtime together with `tests/test_release_gate_0_9_0.py`.
Historical 0.7.0, 0.7.1, 0.8.0, and 0.8.1 release records remain under `docs/` but do not
define the current runtime inventory.

## Version and inventory authority

Version 0.9.0 exposes 61 public MCP tools: 41 read-only and 20 write.

For the 0.9.0 candidate, the authoritative sources are:

- package version: `pyproject.toml`;
- server/tool-schema version: `src/mcp_gh_server/__init__.py`, consumed by `gh_server_info`;
- locked editable package version: `uv.lock`;
- executable tool inventory: `src/mcp_gh_server/server.py` / `mcp.list_tools()`;
- exact public input/output schema snapshot: `tests/test_tool_schema_snapshot.py` and
  `tests/test_tool_return_models.py`; and
- release integration assertions in `tests/test_release_gate_0_9_0.py`.

Those sources must all report `0.9.0` and the 61/41/20 inventory for the release candidate.

## Scope

0.9.0 is a surface-focused release that splits the former generic `gh_submit_pr_review`
write into three action-specific formal pull-request review writes and adds one read-only
eligibility preflight. It does not change the ordinary write gates, repository/owner target
policies, or the exact-state and ambiguity contracts of the unchanged writes.

The 0.9.0 changes:

1. **`gh_approve_pr`** — additive write: submit exactly one formal GitHub APPROVED review for
   the supplied exact PR head through the server-configured independent reviewer principal.
   Before the review POST the server verifies repository write policy, current head, expected
   reviewer login, authenticated reviewer login, and reviewer != PR author. The caller cannot
   select credentials. The write is attempted once and immutable review-ID readback verifies
   APPROVED state, actor, head, and body.

2. **`gh_request_pr_changes`** — additive write: submit exactly one formal
   CHANGES_REQUESTED review for the supplied exact PR head through the configured reviewer
   principal. The exact expected reviewer login is a compare-only precondition and cannot
   select credentials. The review POST is attempted once and readback verifies state, actor,
   head, and body.

3. **`gh_comment_pr_review`** — additive write: submit exactly one formal COMMENTED review
   through the ordinary authenticated GitHub principal for the supplied exact PR head. This
   is the explicit same-author fallback for recording an external or Central disposition;
   COMMENTED is never reported as GitHub APPROVED. It cannot select reviewer credentials.

4. **`gh_get_pr_review_eligibility`** — read-only exact-head preflight: report the PR author,
   ordinary GitHub identity, configured reviewer identity, and whether an independent
   APPROVED review or ordinary COMMENTED review is currently eligible. This advisory call
   performs no review write and never mints a reviewer installation token.

The retired `gh_submit_pr_review` generic review action is not a public registration, alias,
or compatibility shim in 0.9.0.

## Public write inventory

The 20 public writes are:

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
- `gh_approve_pr`
- `gh_request_pr_changes`
- `gh_comment_pr_review`
- `gh_merge_pr`
- `gh_create_repo`
- `gh_commit_files`
- `gh_create_release_exact`
- `gh_run_workflow_exact`
- `gh_create_branch`
- `gh_create_branch_from_sha`

The weaker historical public writes `gh_run_workflow`, `gh_create_release`, and
`gh_upsert_label` remain retired, and the generic `gh_submit_pr_review` action is superseded
by the three action-specific review writes above. None are aliases, compatibility shims, or
hidden public registrations in 0.9.0.

## Read-plane contract additions

The 0.9.0 read-plane addition is:

- `gh_get_pr_review_eligibility` — a read-only, fail-closed preflight that binds the
  requested exact head SHA, reports author/ordinary/reviewer identity, and reports approval
  and COMMENTED-review eligibility. It performs no review write and never mints a reviewer
  installation token during the read-only preflight.

These changes preserve:

- public tool names for all unchanged tools;
- read/write annotations;
- the 41/20 split;
- write authorization or fine gates;
- historical 0.7.x/0.8.x release-gate records.

## Test/documentary additions

- `tests/test_pr_review_identity.py` — dedicated regression coverage for the reviewer
  identity and eligibility paths.
- `tests/test_reviewer_auth.py` — regression coverage for static-token and GitHub-App
  reviewer principal resolution and fail-closed identity behavior.
- `tests/test_tool_schema_snapshot.py` — pins the complete current 61-tool schema surface,
  including the four new review tool schemas and the absence of `gh_submit_pr_review`.
- `tests/test_write_surface_contract.py` — pins all 20 public write facades and canonical
  module provenance, including the three review writes bound to `pr_review_tool_schema`.
- `docs/pr-review-evidence-contract.md` and `docs/write-schema-contract.md` — describe the
  0.9.0 review surface and host-facing write contracts.

## Validation

Validation is bound to one exact candidate SHA. Any source change invalidates affected
results.

Required commands are:

```bash
uv run pytest tests/test_release_gate_0_9_0.py \
  tests/test_write_schema_policy.py \
  tests/test_tool_schema_snapshot.py \
  tests/test_write_surface_contract.py \
  tests/test_pr_review_identity.py \
  tests/test_reviewer_auth.py \
  tests/test_mcp_protocol.py

uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
git diff --check
```

The release passes only when package version, server version, tool-schema version, `uv.lock`,
runtime inventory, schema snapshots, documentation, static checks, focused negative/fail-closed
regressions, and the full suite agree on the same exact candidate SHA.

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

0.9.0 does not add arbitrary public `gh api`, arbitrary `gh <args...>`, a generic
shell/subprocess MCP tool, administrator merge bypass, automatic mutation replay,
artifact/log deletion, or branch-protection/ruleset mutation.
