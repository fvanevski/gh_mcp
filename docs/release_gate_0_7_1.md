# Release gate 0.7.1

This document is the immutable historical acceptance record for the shipped 0.7.1 public MCP surface. It supplements, rather than rewrites, the historical 0.7.0 gate in `docs/release_gate_0_7_0.md`. Current runtime/release authority is defined separately by the active release gate.

## Version and inventory authority

Version 0.7.1 exposes 61 public MCP tools: 40 read-only and 21 write.

For the released 0.7.1 candidate, the authoritative sources were:

- package version: `pyproject.toml`;
- server/tool-schema version: `src/mcp_gh_server/__init__.py`, consumed by `gh_server_info`;
- locked editable package version: `uv.lock`;
- executable tool inventory: `src/mcp_gh_server/server.py` / `mcp.list_tools()`;
- exact public input/output schema snapshot: `tests/test_tool_schema_snapshot.py` and `tests/test_tool_return_models.py`; and
- release integration assertions in the then-current `tests/test_release_gate_0_7_1.py`.

Those sources all reported `0.7.1` for the released candidate. This document records that historical state; it does not require later releases to retain the same runtime version or public inventory.

## Historical 0.8.0 transition note

During development of the breaking 0.8.0 write-contract remediation, child issues intentionally retired weaker public writes before package/server/tool-schema authority advanced. That intermediate mismatch was treated as evidence that the breaking surface had not yet passed its version gate, not as authority to rewrite 0.7.1 history.

Issue #61 subsequently owns the separate 0.8.0 release authority in `docs/release_gate_0_8_0.md`. Historical 0.7.0/0.7.1 counts remain unchanged while current runtime assertions move to the 0.8.0 gate.

## 0.7.1 additions

The final 0.7.1 surface added five read-only tools to the established 0.7.0 release surface:

### `gh_get_merge_requirements`

Aggregates merge-readiness requirements for one pull request at an exact expected head SHA. The result preserves current head/base identity, required/current checks and reviews, conversation-resolution and up-to-date evidence, and allowed merge methods. Missing policy/rules visibility, unmodeled active requirements, truncated/incomplete evidence, or identity movement must fail closed as incomplete evidence rather than being interpreted as no requirement. The tool performs no merge and exposes no branch-protection/ruleset mutation or administrator bypass. See `docs/merge_requirements.md`.

### `gh_compare_commits`

Compares exact 40-character base/head commit SHAs and reports merge-base identity, identical/ahead/behind/diverged status, ahead/behind counts, and independently bounded commit and file collections. Returned evidence preserves truncation/completeness metadata and deterministic SHA-256 fingerprints. No branch/tag resolution occurs inside the tool. Missing-commit evidence remains distinct from permission/transport failure. See `docs/gh_compare_commits.md`.

### `gh_list_artifact_files` and `gh_read_artifact_file`

Inspect one exact unexpired Actions artifact without exposing arbitrary archive extraction or deletion. Archive paths are normalized; absolute/traversal paths, duplicate/conflicting entries, symlinks, special/encrypted entries, and configured archive/uncompressed/file-size violations are rejected. Reads return only bounded valid UTF-8 text/JSON and preserve artifact/archive/file identity and digest evidence. Temporary ZIP state is cleaned up on success and failure. See `docs/gh_artifact_content.md`.

### `gh_get_api_rate_status`

Reports GitHub-provided primary rate-limit observations separately from local `GitHubRequestGovernor` state. The diagnostic route itself remains governed and coalesces repeated/concurrent calls behind a local minimum refresh interval so it cannot become a high-frequency polling bypass. The local interval is deployment policy, not a hard-coded GitHub secondary-limit threshold. See `docs/gh_get_api_rate_status.md`.

## Historical public-surface invariants

The shipped 0.7.1 schema snapshot established all 61 registered names, required/optional input fields, read-only/destructive/idempotent/open-world annotations, and the focused schema details for that release. The then-current return-model regression accounted for every registered tool.

The 0.7.1 release preserved existing 0.7.0 behavior and write safety. In particular, it did not weaken exact-state/SHA preconditions, default-off write gating, write/readback semantics, request-governor mediation, bounded-evidence completeness handling, or stderr-only logging.

## Explicit non-goals

The 0.7.1 public MCP surface does **not** expose:

- arbitrary `gh <args...>` execution;
- arbitrary public `gh api` execution;
- generic shell or subprocess execution;
- administrator merge/protected-branch bypass;
- automatic rerun-on-failure or repeated workflow dispatch;
- artifact deletion or workflow-log deletion; or
- branch-protection or ruleset mutation.

The deferred workflow rerun/cancellation issue was not part of the 0.7.1 release surface.

## Historical validation record

The 0.7.1 release gate required its package/server/tool-schema/lock versions to agree at 0.7.1, the executable registry to be exactly 61/40/21, documentation to match that released registry, and the schema/protocol/static/full-suite gates to pass on the same exact candidate SHA.

Later releases must validate against their own release gate rather than mutating this record. For 0.8.0, use `docs/release_gate_0_8_0.md` and `tests/test_release_gate_0_8_0.py`.
