# Release gate: 0.7.0

This document records the integration contract for the 0.7.0 public MCP surface. It describes the release invariant; it does not replace exact-head test evidence.

## Version and public surface

Version 0.7.0 exposes 56 public MCP tools: 35 read-only and 21 write.

The Phase 2 surface adds these 12 tools over the 0.6.x baseline:

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

`gh_list_runs` is enhanced within the same 0.7.0 gate. The exact public names, input schemas, annotations, and output contracts are captured by `tests/test_tool_schema_snapshot.py`.

## Architecture

`src/mcp_gh_server/server.py` is the composition/re-export root. Tool implementations live in cohesive domain modules under `src/mcp_gh_server/tools/` rather than a monolithic server module.

GitHub request execution is governed by the shared `GitHubRequestGovernor`. Tool implementations must preserve its request classification, cooldown/rate-limit handling, ambiguity handling, and bounded-evidence behavior rather than introducing independent request policy.

## Exact-state writes and readback

Writes remain disabled by default and continue to require the repository's explicit write gates.

Where a mutation depends on current state or revision identity, the public contract requires the applicable expected state/SHA precondition. A write performs one mutation attempt and then re-reads authoritative GitHub state before reporting success. Ambiguous or partial write outcomes must not be blindly retried; callers re-read state first and decide from authoritative evidence.

## Bounded evidence

Artifact and Actions-log reads remain bounded evidence operations. Truncation/completeness indicators, byte counts, digests, and warnings are part of the evidence contract and must not be discarded or reinterpreted as complete results.

Log tailing or marker-based selection is intentionally partial evidence when the response says so. Artifact tooling in 0.7.0 exposes bounded metadata/evidence reads, not a generic artifact-download or deletion escape hatch.

## Explicit non-goals

0.7.0 does not expose:

- arbitrary public `gh <args...>` execution;
- arbitrary public `gh api` execution;
- a generic shell or subprocess MCP tool;
- administrator bypasses;
- automatic repeated workflow rerun or dispatch;
- artifact or log deletion;
- branch-protection or ruleset mutation.

The release does expose narrowly defined one-shot write tools such as exact workflow dispatch where their documented preconditions and readback semantics are satisfied; that does not create a general rerun/dispatch primitive.

## Configuration

No additional release-gate configuration is required beyond settings already introduced by the completed 0.7.0 tool work. `.env.example` and settings documentation should therefore remain unchanged unless current source introduces a real configuration surface that is missing from them.

## Acceptance mapping

| Criterion | Source of truth | Regression evidence |
| --- | --- | --- |
| Package/server/tool-schema version agree at 0.7.0 | `pyproject.toml`, `uv.lock`, `mcp_gh_server.__version__` | `tests/test_release_gate_0_7_0.py`, protocol/model tests |
| README/QWEN inventory and architecture match source | live registry, `server.py`, `tools/` modules | release-gate docs test plus exact tool snapshot |
| Public schema removal/rename is detected | live MCP registry | `tests/test_tool_schema_snapshot.py` |
| Forbidden generic/admin/destructive surfaces remain absent | live MCP registry and repository contracts | exact snapshot plus release non-goal assertions |
| Full project quality gate passes | repository configuration | exact-final-head Ruff, format, mypy, and pytest execution |

## Required final validation

Validation must be executed after the final source/doc/lockfile change and bound to that exact Git HEAD:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Any subsequent source change invalidates affected validation and requires the relevant commands to be rerun.
