# Release gate: 0.7.0

This document is the immutable historical integration record for the shipped 0.7.0 public MCP surface. It describes the release invariant that applied to that candidate; current runtime/release authority is defined separately by the active release gate.

## Version and public surface

Version 0.7.0 exposes 56 public MCP tools: 35 read-only and 21 write.

The Phase 2 surface added these 12 tools over the 0.6.x baseline:

- `gh_get_ref`
- `gh_get_commit`
- `gh_list_run_artifacts`
- `gh_get_artifact`
- `gh_get_job_logs`
- `gh_get_run_logs`
- `gh_run_workflow_exact`
- `gh_create_release_exact`
- `gh_set_issue_state`
- `gh_list_pr_reviews`
- `gh_get_pr_review_state`
- `gh_set_pr_draft_state`

`gh_list_runs` was enhanced within the same 0.7.0 gate. The then-current exact public names, input schemas, annotations, and output contracts were captured by the release's schema regressions.

## Historical architecture

`src/mcp_gh_server/server.py` was the composition/re-export root and tool implementations lived in cohesive domain modules under `src/mcp_gh_server/tools/` rather than a monolithic server module.

GitHub request execution was governed by the shared `GitHubRequestGovernor`. The release required tool implementations to preserve request classification, cooldown/rate-limit handling, ambiguity handling, and bounded-evidence behavior rather than introducing independent request policy.

## Exact-state writes and readback

Writes remained disabled by default and required explicit write gates.

Where a mutation depended on current state or revision identity, the public contract required the applicable expected state/SHA precondition. A write performed one mutation attempt and then re-read authoritative GitHub state before reporting verified success. Ambiguous or partial outcomes were not blindly retried; callers re-read state first.

## Bounded evidence

Artifact and Actions-log reads were bounded evidence operations. Truncation/completeness indicators, byte counts, digests, and warnings were part of the evidence contract and were not to be discarded or reinterpreted as complete results.

Log tailing or marker-based selection remained intentionally partial evidence when the response said so. Artifact tooling exposed bounded metadata/evidence reads, not a generic artifact-download or deletion escape hatch.

## Explicit non-goals

0.7.0 does not expose:

- arbitrary public `gh <args...>` execution;
- arbitrary public `gh api` execution;
- a generic shell or subprocess MCP tool;
- administrator bypasses;
- automatic repeated workflow rerun or dispatch;
- artifact or log deletion; or
- branch-protection or ruleset mutation.

The release did expose narrowly defined one-shot write tools such as exact workflow dispatch where documented preconditions and readback semantics were satisfied; that did not create a general rerun/dispatch primitive.

## Historical acceptance mapping

For the released 0.7.0 candidate:

- package, lockfile, server, and tool-schema versions agreed at `0.7.0`;
- the executable registry was exactly 56/35/21;
- the public schema snapshot detected removal/rename drift;
- forbidden generic/admin/destructive surfaces remained absent; and
- Ruff, format, mypy, and the full pytest suite were required on the same exact candidate head.

Those statements record 0.7.0 release evidence; they do not require later `pyproject.toml`, `uv.lock`, or the live registry to remain at 0.7.0.

Later releases validate against their own release gate rather than mutating this record. For 0.8.0, use `docs/release_gate_0_8_0.md` and `tests/test_release_gate_0_8_0.py`.
