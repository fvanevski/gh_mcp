# AGENTS.md — mcp-gh-server

## What this is

Python MCP 2.0 server (`mcp-gh`) wrapping the `gh` CLI as structured MCP tools. All `gh`
operations run as detached async subprocesses; there is **no generic command executor** and
**no local checkout capability**. Read-only tools return bounded structured JSON. Write
tools are off-by-default and gated behind explicit environment flags.

## Quick start

```bash
gh --version                 # requires gh >= 2.97.0
cp .env.example .env        # add GITHUB_TOKEN
uv sync --dev
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

`gh >= 2.97.0` is a runtime requirement. Actions log readers depend on the
`gh api --allow-escape-sequences` behavior introduced in 2.97.0. Do not validate against
older releases without an explicit compatibility design change.

Integration tests skip when `GITHUB_TOKEN` is absent; all other tests run offline.

## Project layout

| Path | Purpose |
|---|---|
| `src/mcp_gh_server/server.py` | Composition root — registers every tool on the MCP object |
| `src/mcp_gh_server/tooling.py` | Shared helpers: annotations, write gates, repository validation, evidence |
| `src/mcp_gh_server/tools/` | Read-only tool implementations by domain |
| `src/mcp_gh_server/write_tool_schema.py` | Public write-tool facade: schemas, titles, descriptions, annotations |
| `src/mcp_gh_server/legacy_*.py` | 14 legacy adapter files mapping MCP tools → `gh` subcommands |
| `src/mcp_gh_server/models.py` | Pydantic result schemas (used by tests and tool annotations) |
| `src/mcp_gh_server/settings.py` | All runtime config, loaded once via `get_settings()` (cached `@lru_cache`) |
| `src/mcp_gh_server/serialization.py` | JSON-safe serialization helpers (Decimal→str, bytes→base64:, inf→str) |
| `tests/test_write_wrappers.py` | Largest test file; regression coverage for every write tool |
| `tests/test_mcp_protocol.py` | Protocol-level contract tests (tool registration, schema, annotations) |
| `tests/test_integration.py` | Live-GitHub integration tests — require `GITHUB_TOKEN` |

Key invariant: write execution lives in `legacy_*.py`; the public facade in
`write_tool_schema.py` owns host-facing schema only and delegates unchanged to those
implementations. Changing a write tool's mutation semantics requires touching both.

## Runtime architecture

- **Transport**: stdio (default) or Streamable HTTP on `127.0.0.1:8766`. Switch via
  `MCP_GH_TRANSPORT`.
- **Settings loading**: `settings.get_settings()` is cached (`@lru_cache`). Calling it
  again returns the same instance — do not mutate returned objects.
- **Logging always goes to stderr** so it never corrupts the MCP protocol channel.
- **Environment file**: `.env` in the project root, or an absolute path via
  `MCP_GH_ENV_FILE`. `GITHUB_TOKEN` can also come from the host process environment.

## Security model (do not bypass)

Every write tool checks at least one of these before invoking `gh`:

1. `MCP_GH_ALLOW_WRITE_COMMANDS=true` — master write gate (default: `false`)
2. `MCP_GH_ALLOWED_REPOSITORIES=` or `MCP_GH_ALLOWED_OWNERS=` — target allowlist
3. Per-operation fine gates (all default `false`):
   - `MCP_GH_ALLOW_REPO_CREATION`
   - `MCP_GH_ALLOW_RELEASE_CREATION`
   - `MCP_GH_ALLOW_WORKFLOW_DISPATCH`
   - `MCP_GH_ALLOW_CONTENT_COMMITS`
   - `MCP_GH_ALLOW_PR_MERGE`

Write tools return structured errors rather than raising unhandled exceptions. A partial-write
warning (success + unreadable readback) instructs callers **not to retry automatically**.

Branch tools distinguish contracts deliberately:
- `gh_create_branch` — base must be a branch name; rejects 40-char SHAs and redirects to
  `gh_create_branch_from_sha`.
- `gh_create_branch_from_sha` — accepts only an exact 40-character commit SHA; never moves
  or overwrites an existing ref.

`gh_commit_files` validates paths are repository-relative, bounds total request size, and
conditionally advances the branch only when `expected_head_sha` still matches. It does not
support file deletion.

PR author cannot `approve` their own pull request — the server checks login identity before
the POST and returns an explicit `no review was attempted` error. `request_changes` has no
equivalent server-side restriction (GitHub is authoritative).

## PR review workflow (read-only, no checkout)

1. `gh_get_pr` → record `head_sha` and `base_sha`.
2. `gh_get_pr_diff` → read bounded diff/patch. Check `truncated`, `bytes_returned`,
   `total_bytes`, and `sha256`.
3. Page through `gh_list_pr_files` and `gh_list_pr_commits`. Re-fetch the SHA pair after
   each page; abort if the snapshot changed.
4. For full file content, use `gh_get_file_contents` at the recorded SHAs.
5. If the PR changes during review, restart from the new SHA pair.

Formal review requires `gh_submit_pr_review` (not `gh_create_comment`). Merge requires
`gh_merge_pr` with the same exact head SHA; `--match-head-commit` prevents silent revision
swaps. Both are destructive writes requiring explicit opt-in.

## Commands that agents will need

```bash
# Confirm the runtime CLI compatibility floor
gh --version

# Run all unit tests (offline, fast)
uv run pytest

# Run integration tests (requires GITHUB_TOKEN)
GITHUB_TOKEN=<token> uv run pytest tests/test_integration.py

# Full validation suite
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest

# Start MCP Inspector for manual tool testing
uv run mcp dev src/mcp_gh_server/server.py

# Launch as stdio MCP server
uv run mcp-gh

# Launch as Streamable HTTP server
MCP_GH_TRANSPORT=streamable-http uv run mcp-gh
```

## Testing conventions

- Use `FakeGhClient` (defined in `tests/test_write_wrappers.py`) when writing unit tests
  for write tools. It records calls, accepts queued return values, and handles `--input`
  payload parsing.
- Async fixtures are automatic (`asyncio_mode = "auto"` in `pyproject.toml`). Do not add
  explicit `@pytest.mark.asyncio` decorators.
- Integration tests live in `tests/test_integration.py`; they use `cli/cli` as the stable
  reference repo and skip gracefully without `GITHUB_TOKEN`.
- Never commit `.env` files. The `.env.example` is the canonical template.
- The released version (0.7.1) intentionally retains immutable inventory-gate failures
  caused by the 59/19 development registry. Issue #61 owns the 0.8.0 cleanup — do not
  hide those failures by lowering counts on intermediate child issues.

## Agent tooling (local stack)

This repo is Python; prefer Serena over glob/grep for symbol work. Shell commands go
through RTK to preserve tokens.

| Task | Tool | Example |
|---|---|---|
| Semantic read (symbols, declarations, references, body) | `serena_*` | `serena_get_symbols_overview`, `serena_search_for_pattern` |
| Edit via symbols | `serena_replace_symbol_body`, `serena_insert_before_symbol` | Rename a function across callers without regex guessing |
| Diagnostics | `serena_get_diagnostics_for_file` | Catch type errors before running mypy |
| Shell commands | `rtk <cmd>` | `rtk uv run pytest tests/test_write_wrappers.py` |
| Cross-session memory | `openviking_recall` / `openviking_remember` | Preserve decisions about write-tool gate semantics across sessions |

Do **not** invoke the raw `gh` CLI directly when an MCP tool exists — the server enforces
write gates, SHA pinning, and bounded output that a bare subprocess skips. Do **not**
commit `.env`; use `rtk` for env lookups if needed.

## Known constraints

- The `gh` CLI must be installed on `$PATH` at version **2.97.0 or newer**. The server
  never installs or manages it. `gh_get_job_logs` and `gh_get_run_logs` rely on the
  `gh api --allow-escape-sequences` behavior available from 2.97.0 onward.
- Rate limits, authentication scope, and permission scoping are delegated entirely to
  `gh` and the supplied token — the server performs no additional auth checks.
- Tools like `gh release create` that require file uploads or multi-step interactive flows
  are intentionally out of scope.
- `gh_search_repos/issues/code` use `--json` output; field names follow GitHub's API
  naming convention (camelCase), not snake_case.
- Result serialization converts `Decimal` → string, `bytes` → `base64:` prefix,
  infinities → string, and all datetimes → ISO 8601. Do not assume native Python types
  in responses.
- Detailed contract documents live under `docs/` (e.g. `docs/release_gate_0_7_1.md`,
  `docs/pr-review-evidence-contract.md`, `docs/gh_action_logs.md`). Refer to them when
  working on tools whose behavior is specified there rather than inferring from code.
