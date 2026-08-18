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

`src/mcp_gh_server/current_write_tool_schema.py` composes this inventory from an explicit
17-tool non-review allowlist plus the three action-specific formal-review tools. It does not
derive the current authority by importing an older public inventory and subtracting a retired
tool name. This keeps future changes to the historical/non-review facade from accidentally
reintroducing a retired formal-review authority.

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

## Reviewer-principal hardening

The preferred reviewer path is a narrowly scoped GitHub App. Release acceptance requires
deterministic coverage of the App path rather than only configuration parsing:

- read-only reviewer resolution verifies App ID, exact repository installation,
  `pull_requests=write`, and non-suspended installation state without minting an
  installation token;
- write-client construction rechecks the exact installation, mints one repository-scoped
  token with only `pull_requests=write`, constructs a reviewer-specific `GhClient`, and
  verifies the authenticated reviewer through GraphQL `viewer.login`;
- mismatched installation, permission, suspension, and authenticated reviewer identity fail
  closed;
- App JWT signing invokes `openssl` directly without a shell and never places private-key
  contents in argv or stdin;
- public reviewer-auth failures do not reflect configured private-key filesystem paths or
  raw OpenSSL stderr, either of which can expose deployment-local details; and
- the temporary static-token path remains reviewer-only and never overwrites the ordinary
  GitHub credential.

`tests/test_reviewer_client_isolation.py` independently proves that an unrelated ordinary PR
write continues to execute solely through the ordinary application client even when reviewer
credentials are configured.

## Test/documentary additions

- `tests/test_pr_review_identity.py` — dedicated regression coverage for the reviewer
  identity, exact-head review writes, eligibility paths, self-approval rejection, and
  no-blind-replay behavior.
- `tests/test_reviewer_auth.py` — direct regression coverage for static-token and GitHub-App
  reviewer principal resolution, App JWT signing, exact installation/permission checks,
  repository-scoped token minting, authenticated reviewer verification, fixed GitHub API
  transport, ambiguity classification, credential isolation, and sanitized deployment
  errors.
- `tests/test_reviewer_client_isolation.py` — proves unrelated ordinary PR writes do not
  resolve or reuse reviewer credentials.
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
  tests/test_reviewer_client_isolation.py \
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

The repository currently has no committed Pyrefly pin/configuration/baseline. A workflow that
requires Pyrefly must report that gate as unavailable rather than installing, regenerating, or
weakening type-check policy inside issue #75. The repository's existing mypy command remains
supplementary evidence and is not represented as Pyrefly.

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

A deployed `gh_server_info` result is not sufficient to prove that a ChatGPT connector has
refreshed its cached tool namespace. Before closing the gate, rediscover the connector and
verify that `gh_get_pr_review_eligibility`, `gh_approve_pr`, `gh_request_pr_changes`, and
`gh_comment_pr_review` are callable and that the retired `gh_submit_pr_review` is absent.
A stale host namespace must be classified separately from the server's executable inventory.

## Disposable live exercise

The deterministic suite is necessary but not sufficient for issue #75. The live
exercise required the following evidence to be bound to one exact disposable PR head:

| Scenario | Required result |
| --- | --- |
| ordinary author identity attempts own approval | rejected before formal-review POST |
| distinct reviewer identity approves author's PR | verified `APPROVED` review |
| wrong expected reviewer login | rejected before formal-review POST |
| changed PR head | rejected before formal-review POST |
| same-author formal review fallback | verified `COMMENTED`, never represented as `APPROVED` |
| ambiguous response simulation where supported | authoritative read before any retry; no blind replay |
| ChatGPT invocation of dedicated approval tool | server reachability + verified review, or explicit pre-server host rejection |

If an App approval is intended to satisfy a repository required-review policy, verify that
policy separately. The existence of an `APPROVED` review object is not evidence that a
ruleset or branch-protection rule counts that App's approval.

All required live scenarios in the table above have been executed; the completed record
is "Final live evidence" below. The only remaining gate action after that record is a
fresh exact-head review of the final head before merge.

## Final live evidence

The completed issue #75 live acceptance record:

1. **Deterministic validation** — the focused fail-closed, no-blind-replay, and
   reviewer-isolation regressions in `tests/test_reviewer_auth.py`,
   `tests/test_pr_review_identity.py`, and `tests/test_reviewer_client_isolation.py`
   pass at the validated implementation head. The deterministic tri-state and
   no-blind-replay tests remain the authoritative coverage for transport ambiguity.

2. **Reviewer/App configuration** — the dedicated reviewer principal is
   `gh-mcp-reviewer[bot]` (reviewer kind `github_app`), distinct from the ordinary
   principal `fvanevski`. The App is installed on `fvanevski/gh_mcp` with
   `pull_requests=write`, mints repository-scoped installation credentials, and
   authenticates as `gh-mcp-reviewer[bot]`. App authentication and configuration:
   COMPLETE. No App secrets, installation-token values, JWTs, or private-key
   contents are recorded in this document.

3. **Connector/runtime surface** — backend version 0.9.0, tool-schema version 0.9.0,
   61 total tools (41 read-only, 20 write). The current action-specific review
   surface is `gh_get_pr_review_eligibility`, `gh_approve_pr`,
   `gh_request_pr_changes`, and `gh_comment_pr_review`; the retired
   `gh_submit_pr_review` is absent from the authoritative current executable MCP
   inventory.

4. **Completed live scenario matrix**
   - **Wrong expected reviewer**: COMPLETE — rejected before the formal-review POST;
     no review created.
   - **Stale/moved expected head**: COMPLETE — rejected before the formal-review POST;
     no review created.
   - **Reviewer is PR author**: COMPLETE — disposable PR #77 (author
     `gh-mcp-reviewer[bot]`) reported `approval_eligible=false` with
     `reason=reviewer_is_pr_author`. Its exactly one `gh_approve_pr` attempt was
     rejected with the explicit error that the configured reviewer
     `gh-mcp-reviewer[bot]` is the pull request author and cannot approve its own
     pull request, with no review attempted. Pre- and post-review counts were 0.
     The disposable PR was closed without merging and its branch was deleted.
   - **Same-author COMMENTED**: COMPLETE — PR #76 review 4955099421 by `fvanevski`
     is `COMMENTED` at commit
     206d3f2b81517b774d8939829af8754845c7c083. COMMENTED is not APPROVED, and no
     silent approve-to-comment fallback occurred.
   - **Distinct reviewer APPROVED**: COMPLETE — PR #76 review 4956014379 by
     `gh-mcp-reviewer[bot]` is `APPROVED` at the same commit with
     `precondition_checked`, `write_completed`, `readback_completed`, and
     `state_matches_requested` all true. This is the successful live-acceptance
     artifact for the implementation head
     206d3f2b81517b774d8939829af8754845c7c083; it is not the final
     current-head approval after later commits.

5. **Immutable review IDs** — 4955099421 (`fvanevski`, COMMENTED) and
   4956014379 (`gh-mcp-reviewer[bot]`, APPROVED), both recorded at commit
   206d3f2b81517b774d8939829af8754845c7c083.

6. **Merge/policy readback** — at the accepted implementation head:
   `required_approvals=0`, `required_status_checks=[]`,
   `conversation_resolution_required=false`, `up_to_date_required=false`,
   `up_to_date=true`, `mergeable=true`, `merge_state=clean`, and the allowed merge
   methods were `merge`, `squash`, and `rebase`. No required-review policy was in
   force, so policy satisfaction was "not required"; this is not evidence that the
   App was proven to satisfy a one-review branch-protection requirement.

7. **Ambiguity disposition** — live ambiguity injection was NOT SAFELY INDUCED: no
   deterministic fault-injection mechanism exists that can safely induce an
   ambiguous non-idempotent formal-review POST without risking an unknown duplicate
   mutation. The deterministic tri-state and no-blind-retry tests remain
   authoritative for that case. This is not an outstanding blocker; the live
   exercise contract required the ambiguity scenario only when safely and
   supportably inducible.

8. **Exact-head consequence** — any later documentation-only commit moves the PR
   head and supersedes review 4956014379 for final exact-head approval. A fresh
   exact-head review must be obtained before merging the new head.

## Non-goals

0.9.0 does not add arbitrary public `gh api`, arbitrary `gh <args...>`, a generic
shell/subprocess MCP tool, administrator merge bypass, automatic mutation replay,
artifact/log deletion, or branch-protection/ruleset mutation.
