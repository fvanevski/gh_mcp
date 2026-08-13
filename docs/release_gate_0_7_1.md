# Release gate 0.7.1

This document records the repository-authoritative acceptance contract for the final 0.7.1 public MCP surface. It supplements, rather than rewrites, the historical 0.7.0 gate in `docs/release_gate_0_7_0.md`.

## Version and inventory authority

The 0.7.1 release surface is exactly **61 public MCP tools: 40 read-only and 21 write**.

The authoritative sources are:

- package version: `pyproject.toml`;
- server/tool-schema version: `src/mcp_gh_server/__init__.py`, consumed by `gh_server_info`;
- locked editable package version: `uv.lock`;
- executable tool inventory: `src/mcp_gh_server/server.py` / `mcp.list_tools()`;
- exact public input/output schema snapshot: `tests/test_tool_schema_snapshot.py` and `tests/test_tool_return_models.py`;
- final release integration assertions: `tests/test_release_gate_0_7_1.py`.

All version sources must report `0.7.1`. Documentation counts are descriptive and must agree with the executable registry; they are not an independent source of truth.

## 0.7.1 additions

The final surface adds five read-only tools to the established 0.7.0 release surface:

### `gh_get_merge_requirements`

Aggregates merge-readiness requirements for one pull request at an exact expected head SHA. The result preserves current head/base identity, required/current checks and reviews, conversation-resolution and up-to-date evidence, and allowed merge methods. Missing policy/rules visibility, unmodeled active requirements, truncated/incomplete evidence, or identity movement must fail closed as incomplete evidence rather than being interpreted as no requirement. The tool performs no merge and exposes no branch-protection/ruleset mutation or administrator bypass. See `docs/merge_requirements.md`.

### `gh_compare_commits`

Compares exact 40-character base/head commit SHAs and reports merge-base identity, identical/ahead/behind/diverged status, ahead/behind counts, and independently bounded commit and file collections. Returned evidence preserves truncation/completeness metadata and deterministic SHA-256 fingerprints. No branch/tag resolution occurs inside the tool. Missing-commit evidence remains distinct from permission/transport failure. See `docs/gh_compare_commits.md`.

### `gh_list_artifact_files` and `gh_read_artifact_file`

Inspect one exact unexpired Actions artifact without exposing arbitrary archive extraction or deletion. Archive paths are normalized; absolute/traversal paths, duplicate/conflicting entries, symlinks, special/encrypted entries, and configured archive/uncompressed/file-size violations are rejected. Reads return only bounded valid UTF-8 text/JSON and preserve artifact/archive/file identity and digest evidence. Temporary ZIP state is cleaned up on success and failure. See `docs/gh_artifact_content.md`.

### `gh_get_api_rate_status`

Reports GitHub-provided primary rate-limit observations separately from local `GitHubRequestGovernor` state. The diagnostic route itself remains governed and coalesces repeated/concurrent calls behind a local minimum refresh interval so it cannot become a high-frequency polling bypass. The local interval is deployment policy, not a hard-coded GitHub secondary-limit threshold. See `docs/gh_get_api_rate_status.md`.

## Final public-surface invariants

The exact schema snapshot must continue to prove all 61 registered names, required/optional input fields, read-only/destructive/idempotent/open-world annotations, and the focused schema details encoded by `tests/test_tool_schema_snapshot.py`. The return-model regression must continue to account for every registered tool.

`tests/exercise_tools.py --inventory-only` must resolve the server and assert the exact 61-tool inventory, including all five 0.7.1 additions, without performing GitHub I/O. The optional live exercise remains non-destructive and is not a substitute for the protocol/schema tests.

Existing 0.7.0 behavior and write safety remain intact. In particular, the final release does not weaken exact-state/SHA preconditions, default-off write gating, write/readback semantics, request-governor mediation, bounded-evidence completeness handling, or stderr-only logging.

## Explicit non-goals

The 0.7.1 public MCP surface does **not** expose:

- arbitrary `gh <args...>` execution;
- arbitrary public `gh api` execution;
- generic shell or subprocess execution;
- administrator merge/protected-branch bypass;
- automatic rerun-on-failure or repeated workflow dispatch;
- artifact deletion or workflow-log deletion;
- branch-protection or ruleset mutation.

The deferred workflow rerun/cancellation issue is not part of the 0.7.1 release surface.

## Required validation

Run against the exact final candidate SHA, with a clean working tree before and after:

```bash
uv run pytest \
  tests/test_release_gate_0_7_1.py \
  tests/test_tool_schema_snapshot.py \
  tests/test_mcp_protocol.py \
  tests/test_tool_return_models.py \
  tests/test_mcp_schema.py \
  tests/test_write_wrappers.py
python tests/exercise_tools.py --inventory-only
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
git diff --check
```

The release gate passes only when package/server/tool-schema/lock versions agree at 0.7.1, the executable registry remains exactly 61/40/21, documentation matches that registry, the final schema/protocol regressions pass, and the full repository gate passes on the same exact SHA.
