# QWEN.md

Repository-specific implementation guidance. `AGENTS.md` remains the general development-workflow authority.

## Release and development surface authority

Released version 0.7.1 exposes 61 public MCP tools: 40 read-only and 21 write. That inventory is immutable released-version authority enforced by `tests/test_release_gate_0_7_1.py`; `tests/test_release_gate_0_7_0.py` likewise preserves the historical 0.7.0 floor.

The current unreleased 0.8.0 development surface exposes 59 public MCP tools: 40 read-only and 19 write after issue #55 retired generic workflow dispatch and issue #56 retired generic release creation. Until issue #61 performs the 0.8.0 version bump and integration cleanup, the executable registry and active schema snapshots describe that development surface while the 0.7.0/0.7.1 release gates intentionally continue to enforce their released inventories. Do not lower historical/current release-gate counts in child canonicalization issues merely to make the intermediate full suite green.

The 0.7.1 read-only additions are:

- `gh_get_merge_requirements`
- `gh_compare_commits`
- `gh_list_artifact_files`
- `gh_read_artifact_file`
- `gh_get_api_rate_status`

## Current unreleased write-surface transition

- `gh_run_workflow_exact` is the sole current public workflow-dispatch primitive; the weaker generic `gh_run_workflow` contract is retired from the active registry.
- `gh_create_release_exact` is the sole current public release-creation primitive; the weaker generic `gh_create_release` contract is retired from the active registry.
- Legacy implementations may remain internal only until #61 removes obsolete compatibility infrastructure and versions the breaking public-surface change as 0.8.0.
- README/current-development documentation may describe the 59/19 active registry, but released 0.7.0/0.7.1 gate documents must remain truthful historical records.

## Architecture

`src/mcp_gh_server/server.py` is the composition root. Public tools are implemented in cohesive domain modules under `src/mcp_gh_server/tools/`. GitHub request execution is centralized through the shared `GitHubRequestGovernor`; tool code must not invent direct arbitrary `gh`, `gh api`, or shell escape hatches.

Raw-byte evidence reads must preserve the same governor metadata contract as structured reads. If `gh api` response headers are requested internally to recover request IDs, `Retry-After`, or primary rate-limit state, transport code must strip that framing before any binary body bytes reach the evidence sink. Once the sink has observed body bytes, the read must not be transparently retried.

Writes remain default-off. Exact writes preserve expected state/SHA preconditions when applicable, perform one mutation attempt, and require authoritative readback before success. Ambiguous or partial write outcomes are evidence to re-read authoritative state, not authorization for blind retry.

Bounded evidence must remain explicitly bounded. Callers must preserve truncation/completeness metadata, byte counts, digests, and warnings rather than presenting partial log, comparison, or artifact evidence as complete.

## 0.7.1 inspection invariants

- `gh_get_merge_requirements` is strictly read-only and bound to an exact expected PR head. Missing branch/ruleset visibility, unmodeled active requirements, incomplete check/review evidence, or base/head movement must remain incomplete evidence; they must never be converted into an assumption that no requirement exists.
- `gh_compare_commits` accepts exact 40-character commit SHAs, reports merge-base and ahead/behind status, independently bounds commit/file evidence, and preserves truncation/completeness and digest metadata. Branch-name resolution does not belong inside this tool.
- `gh_list_artifact_files` and `gh_read_artifact_file` operate on one exact unexpired artifact ID. Archive paths must remain normalized and traversal-safe; archive/file sizes are bounded; symlinks, special/encrypted entries, duplicate/conflicting paths, and binary/non-UTF-8 content are rejected. Artifact ZIP state is temporary and is never exposed as a deletion or arbitrary download surface.
- `gh_get_api_rate_status` reports GitHub-provided primary rate-limit evidence separately from local governor policy/state. The diagnostic refresh interval is a local anti-polling policy, not a GitHub secondary-limit threshold, and repeated/concurrent diagnostic calls must not create a polling bypass.

## Explicit non-goals

0.7.1 does not expose arbitrary `gh`, arbitrary public `gh api`, a generic shell/subprocess MCP tool, administrator bypasses, automatic repeated workflow rerun/dispatch, artifact/log deletion, or branch-protection/ruleset mutation.

## Validation

For issue-scoped 0.8.x child work, validate the changed invariant and active schema surface first:

- `uv run pytest tests/test_release_canonicalization.py tests/test_release_exact.py tests/test_tool_schema_snapshot.py tests/test_write_schema_policy.py`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy`

The full `uv run pytest` remains authoritative evidence, but before #61 it is expected to retain release-inventory gate failures caused by the intentional mismatch between the still-versioned 0.7.x release authority and the 59/19 unreleased development registry. Treat those failures as #61 integration work; do not repair them by lowering 0.7.0/0.7.1 release counts. Other failures remain ordinary defects and must be investigated.

After #61 establishes 0.8.0 authority, the full suite, inventory exercise, schema snapshots, documentation, and release gates must all agree on the final versioned surface.

See `docs/release_gate_0_7_1.md` for the immutable 0.7.1 release acceptance mapping and the focused contract documents for implementation details.
