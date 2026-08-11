# gh_mcp — MCP 2.0 GitHub CLI Server

## Project overview

A Python MCP (Model Context Protocol) 2.0 server that wraps the `gh` CLI for GitHub operations. It runs `gh` commands as subprocesses with `--json` output for structured, JSON-safe results. Write commands (issue/PR/repo/release creation, workflow dispatch) are **disabled by default** and require explicit environment variable activation plus optional human approval via MCP elicitation.

**Key dependencies:** `mcp[cli]==2.0.0`, `pydantic-settings==2.14.2`, `python-dotenv==1.2.2`. Python 3.12+.

**Architecture:**

- `server.py` — small MCP composition/re-export root for the 44 public tools.
- `tooling.py` — shared MCP instance/lifespan, annotations, validation, write-policy, readback, and bounded-evidence helpers.
- `write_contracts.py` — shared typed exact-state preconditions, tri-state mutation/readback outcomes, semantic state verification, and governed JSON-write metadata helpers.
- `legacy_write_adapters.py` — 0.6.x compatibility projection for exact-head reviews/merges and atomic content commits.
- `legacy_git_write_adapter.py` — 0.6.x exact-branch compatibility projection with authoritative post-create ref readback.
- `tools/` — cohesive tool-domain modules for diagnostics, discovery, issues, pull requests, repositories/content, releases, Actions, and Git references.
- `gh_client.py` — `GhClient` dataclass: the sole `gh` subprocess boundary; parses JSON, normalizes output, classifies requests conservatively, and enters request governance before every execution.
- `request_governor.py` — shared serialization, mutative-request pacing, safe-read retry, rate-limit cooldown, and request metadata policy.
- `models.py` — Pydantic v2 result models (`IssueInfo`, `PullRequestInfo`, `SearchResults`, etc.).
- `serialization.py` — `to_json_value()` converts `Decimal`, `datetime`, `bytes`, infinities, etc. to JSON-safe types.
- `settings.py` — `Settings` Pydantic model backed by `MCP_GH_*` env vars + `.env` file.
- `__main__.py` — CLI entry point; selects stdio or Streamable HTTP transport.
- `asgi.py` — ASGI app factory for `uvicorn` deployment.

## Building and running

```bash
# Setup (one-time)
cp .env.example .env
$EDITOR .env
uv sync --dev

# Run as stdio MCP server
uv run mcp-gh

# Run as Streamable HTTP
MCP_GH_TRANSPORT=streamable-http uv run mcp-gh
# or: uv run uvicorn mcp_gh_server.asgi:app --host 127.0.0.1 --port 8766

# MCP Inspector (development)
uv run mcp dev src/mcp_gh_server/server.py
```

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Development conventions

- **No direct GitHub API calls.** Everything goes through `GhClient`, then the shared `GitHubRequestGovernor`, and finally the noninteractive `gh` CLI subprocess boundary. Do not spawn `gh` elsewhere or bypass governor policy.
- **Conservative request classification.** Known read-only `gh` routes may use bounded retry for transient transport/server failures. For `gh api`, explicit `GET`/`HEAD` remains read-only, while GraphQL defaults and body-bearing implicit-POST forms (`-F/--field`, `-f/--raw-field`, and `--input`, including attached long forms) are classified as writes. Mutations, malformed/unknown API forms, and unknown commands fail closed as non-retryable writes.
- **Rate-limit cooldown is fail-closed.** `Retry-After` is authoritative when present; an exhausted primary limit uses `X-RateLimit-Reset`; otherwise a rate-limit response establishes the governor's policy fallback cooldown (60 seconds by default) before another queued request may execute. The fallback duration is constructor policy, not a hard-coded secondary-rate-limit trigger threshold.
- **Exact-state write contract.** Every new nontrivial 0.7.x write must use the shared `write_contracts.py` contract and return an `ExactWriteResult`-derived schema with `precondition_checked`, tri-state `write_completed`, `readback_completed`, `state_matches_requested`, `warning`, and `request_id`. Verify expected state immediately before mutation, or encode it as an atomic server-side compare-and-swap/create-only precondition. Never replay an ambiguous mutation automatically. Authoritative readback must verify the requested semantic invariant, not merely command exit status or a mutation response body. The 0.6.x public schemas remain frozen; the `legacy_*_write_adapter.py` compatibility modules are transitional bridges and must not be used as a model for new 0.7.x tools.
- **Order-safe API header capture.** Non-paginated `gh api` calls may receive an internal `--include` flag so request/rate-limit headers can be parsed. Injection is positioned after the detected endpoint and must preserve both `gh api <endpoint> ...` and supported flag-before-endpoint forms such as `gh api -X GET <endpoint>`.
- **JSON-safe output.** All `gh` output passes through `to_json_value()` — `Decimal` → string, `bytes` → `base64:` prefix, `datetime` → ISO 8601, infinities → string.
- **Logs to stderr.** Never pollute stdout (stdio protocol channel).
- **Write commands gated.** `MCP_GH_ALLOW_WRITE_COMMANDS=false` (default) disables all write tools. With `true` + `MCP_GH_CONFIRM_WRITE_COMMANDS=true`, MCP elicitation prompts a human before execution.
- **Result limits.** `MCP_GH_DEFAULT_MAX_RESULTS=30`, capped at `MCP_GH_HARD_MAX_RESULTS=100`.
- **Strict typing.** `mypy` runs in strict mode. All new public functions should have type annotations.
- **Pydantic v2 models.** Tool-specific return models remain in `models.py`; the shared exact-write metadata/base model lives in `write_contracts.py` so future write result models can derive from one contract.
- **Tool annotations.** Read-only tools use `_READ_ONLY_TOOL`; write tools use `_WRITE_TOOL` with `ToolAnnotations`.

## Test structure

- `test_serialization.py` — `to_json_value()` edge cases (decimals, datetimes, bytes, infinities).
- `test_settings.py` — Settings parsing, env var precedence, limit validation.
- `test_request_governor.py` — serialization, pacing, retry, rate-limit/fallback cooldown, implicit API mutation classification, API argument-order preservation, ambiguous-write, and subprocess-boundary invariants.
- `test_write_contracts.py` — exact-state precondition ordering, tri-state write outcomes, semantic readback matching, write/readback failure, and no-replay ambiguity regressions.
- `test_branch_write_contract.py` — exact-commit branch creation readback, mismatch, and ambiguous-transport regressions.
- `test_mcp_schema.py` — MCP tool schema validation.
- `test_integration.py` — End-to-end integration against a real `gh` CLI.
- `exercise_tools.py` — Tool exercise harness.

## Configuration

All config via environment variables (prefixed with `MCP_GH_`) or `.env` file:

| Variable | Default | Description |
| --- | --- | --- |
| `GITHUB_TOKEN` | *(required)* | GitHub PAT (unprefixed, conventional name) |
| `MCP_GH_ALLOW_WRITE_COMMANDS` | `false` | Enable write tools |
| `MCP_GH_CONFIRM_WRITE_COMMANDS` | `true` | Require human approval prompt |
| `MCP_GH_DEFAULT_MAX_RESULTS` | `30` | Default results per query |
| `MCP_GH_HARD_MAX_RESULTS` | `100` | Absolute cap |
| `MCP_GH_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_GH_HTTP_HOST` | `127.0.0.1` | HTTP bind address |
| `MCP_GH_HTTP_PORT` | `8766` | HTTP port |
| `MCP_GH_LOG_LEVEL` | `INFO` | Logging verbosity |
| `MCP_GH_ENV_FILE` | *(auto-discover)* | Absolute path to `.env` file |

## Known boundaries

- `gh release create` with file uploads is out of scope (requires multi-step flow).
- `gh` CLI must be installed and on PATH.
- No support for GitHub REST/GraphQL API directly — purely `gh` subprocess wrapper.
