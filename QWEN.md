# QWEN.md

Repository-specific implementation guidance. `AGENTS.md` remains the general development-workflow authority.

## 0.7.0 release authority

Release 0.7.0 exposes 56 public MCP tools: 35 read-only and 21 write. The executable registry in `src/mcp_gh_server/server.py` and the exact schema snapshot in `tests/test_tool_schema_snapshot.py` are the source of truth for the public tool surface.

## Architecture

`src/mcp_gh_server/server.py` is the composition root. Public tools are implemented in cohesive domain modules under `src/mcp_gh_server/tools/`. GitHub request execution is centralized through the shared `GitHubRequestGovernor`; tool code must not invent direct arbitrary `gh`, `gh api`, or shell escape hatches.

Raw-byte evidence reads must preserve the same governor metadata contract as structured reads. If `gh api` response headers are requested internally to recover request IDs, `Retry-After`, or primary rate-limit state, transport code must strip that framing before any binary body bytes reach the evidence sink. Once the sink has observed body bytes, the read must not be transparently retried.

Writes remain default-off. Exact writes preserve expected state/SHA preconditions when applicable, perform one mutation attempt, and require authoritative readback before success. Ambiguous or partial write outcomes are evidence to re-read authoritative state, not authorization for blind retry.

Bounded evidence must remain explicitly bounded. Callers must preserve truncation/completeness metadata, byte counts, digests, and warnings rather than presenting partial log or artifact evidence as complete.

## Explicit non-goals

0.7.0 does not expose arbitrary `gh`, arbitrary public `gh api`, a generic shell/subprocess MCP tool, administrator bypasses, automatic repeated workflow rerun/dispatch, artifact/log deletion, or branch-protection/ruleset mutation.

## Validation

Run the repository gate against the exact final branch head:

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy`
- `uv run pytest`

See `docs/release_gate_0_7_0.md` for the release acceptance mapping.
