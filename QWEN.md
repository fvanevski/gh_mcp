# QWEN.md

Repository-specific implementation guidance. `AGENTS.md` remains the general development-workflow authority.

## 0.7.1 release authority

Release 0.7.1 exposes 61 public MCP tools: 40 read-only and 21 write. The executable registry in `src/mcp_gh_server/server.py` and the exact schema snapshot in `tests/test_tool_schema_snapshot.py` are the source of truth for the public tool surface. `tests/test_release_gate_0_7_0.py` preserves the historical 0.7.0 floor; `tests/test_release_gate_0_7_1.py` is the final 0.7.1 release-integration gate.

The 0.7.1 read-only additions are:

- `gh_get_merge_requirements`
- `gh_compare_commits`
- `gh_list_artifact_files`
- `gh_read_artifact_file`
- `gh_get_api_rate_status`

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

Run the repository gate against the exact final branch head:

- `uv run pytest tests/test_release_gate_0_7_1.py tests/test_tool_schema_snapshot.py tests/test_mcp_protocol.py tests/test_tool_return_models.py tests/test_mcp_schema.py tests/test_write_wrappers.py`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy`
- `uv run pytest`

Run `python tests/exercise_tools.py --inventory-only` to verify that the exercise harness resolves the exact 61-tool final surface without issuing GitHub requests. Live underlying-CLI exercises remain optional and non-destructive.

See `docs/release_gate_0_7_1.md` for the final release acceptance mapping and the focused Phase 3 contract documents for implementation details.
