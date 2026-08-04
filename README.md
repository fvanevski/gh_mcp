# MCP 2.0 GitHub CLI Server

A Python MCP server for the ``gh`` CLI. It uses the official MCP Python SDK 2.x
and runs ``gh`` commands via subprocess with ``--json`` output for structured
results.

## Tools

### Read-only (20)

- `gh_info`: gh CLI version, authentication status, and active account.
- `gh_search_repos`: search GitHub repositories with qualifiers.
- `gh_search_issues`: search issues and pull requests with qualifiers.
- `gh_search_code`: search source code with qualifiers.
- `gh_list_issues`: list issues in a repository with filters.
- `gh_get_issue`: get details of a specific issue or pull request.
- `gh_list_prs`: list pull requests in a repository.
- `gh_get_pr`: get details of a specific pull request.
- `gh_get_repo`: get details of a specific repository.
- `gh_list_repos`: list repositories for a user or organization.
- `gh_list_releases`: list releases in a repository.
- `gh_get_release`: get details of a specific release.
- `gh_list_workflows`: list GitHub Actions workflows in a repository.
- `gh_get_workflow`: get details of a specific workflow.
- `gh_list_runs`: list recent GitHub Actions workflow runs.
- `gh_get_run`: get details of a specific workflow run.
- `gh_watch_run`: watch a workflow run until completion (blocking).
- `gh_list_labels`: list labels in a repository.
- `gh_list_milestones`: list milestones in a repository.
- `gh_create_comment`: create a comment on an issue or PR.

### Write (11)

- `gh_create_issue`: create a new issue (write, disabled by default).
- `gh_create_pr`: create a new pull request (write, disabled by default).
- `gh_create_repo`: create a new repository (write, disabled by default).
- `gh_create_release`: create a new release (write, disabled by default).
- `gh_run_workflow`: trigger a workflow dispatch event (write, disabled by default).
- `gh_edit_issue`: edit an existing issue (write, disabled by default).
- `gh_create_label`: create a new label (write, disabled by default).
- `gh_edit_label`: edit an existing label (write, disabled by default).
- `gh_create_milestone`: create a new milestone (write, disabled by default).
- `gh_create_branch`: create a branch from an issue's PR (write, disabled by default).
- `gh_edit_pr`: edit an existing pull request (write, disabled by default).

## Install

```bash
cp .env.example .env
$EDITOR .env
uv sync --dev
```

`GITHUB_TOKEN` is required and can be supplied either in the process
environment or the located `.env` file. To use an env file outside the
launch directory, set:

```bash
export MCP_GH_ENV_FILE=/absolute/path/to/.env
```

Start the MCP Inspector:

```bash
uv run mcp dev src/mcp_gh_server/server.py
```

Run as a local stdio MCP server:

```bash
uv run mcp-gh
```

The default transport is stdio. For local Streamable HTTP:

```bash
MCP_GH_TRANSPORT=streamable-http uv run mcp-gh
# endpoint: http://127.0.0.1:8766/mcp
```

Alternative ASGI launch:

```bash
uv run uvicorn mcp_gh_server.asgi:app --host 127.0.0.1 --port 8766
```

## Client configuration

### VS Code / compatible local stdio host

Use an absolute project path. The host should launch the locked project
environment. Run `uv sync --dev` first so the project has a generated
`uv.lock` before using `--frozen`:

```json
{
  "servers": {
    "gh-local": {
      "type": "stdio",
      "command": "/absolute/path/to/uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/gh_mcp",
        "--frozen",
        "mcp-gh"
      ],
      "env": {
        "MCP_GH_ENV_FILE": "/absolute/path/to/gh_mcp/.env"
      }
    }
  }
}
```

For Qwen Code or another host using the common `mcpServers` shape, keep
the same command/args and place the entry under `mcpServers`.

## Write-command policy

Write execution is off by default:

```dotenv
MCP_GH_ALLOW_WRITE_COMMANDS=false
```

To enable it while retaining a mandatory human approval prompt:

```dotenv
MCP_GH_ALLOW_WRITE_COMMANDS=true
MCP_GH_CONFIRM_WRITE_COMMANDS=true
```

The approval is an MCP 2.0 resolver dependency, not a model-visible Boolean
parameter. The prompt asks a human to confirm before the tool executes.
Each accepted write command runs via `gh` CLI and commits only after
successful completion.

Setting `MCP_GH_CONFIRM_WRITE_COMMANDS=false` removes the human approval
gate and is not recommended for a general-purpose agent.

## Operational limits

- Search tools use GitHub's `gh search` subcommands with `--json` output.
- Results are bounded by `MCP_GH_DEFAULT_MAX_RESULTS` (default: 30) and
  capped at `MCP_GH_HARD_MAX_RESULTS` (default: 100).
- All output is JSON-safe: `Decimal` → string, `bytes` → `base64:`
  prefix, datetimes → ISO 8601, infinities → string.
- Logs are sent to stderr so stdio protocol output is not corrupted.
- Streamable HTTP binds to `127.0.0.1` by default and uses MCP's
  localhost DNS-rebinding protection.
- `MCP_GH_LOG_LEVEL` (default: `INFO`) controls logging verbosity;
  set to `DEBUG` for detailed command logs.

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Known boundaries

- The server runs `gh` as a subprocess; it does not use the GitHub REST
  or GraphQL APIs directly. Rate limits, authentication, and permission
  scoping are governed by the `gh` CLI and the token in `GITHUB_TOKEN`.
- Commands like `gh release create` that require file uploads or complex
  multi-step flows are intentionally out of scope — they would need a
  dedicated maintenance tool.
- The `gh` CLI must be installed and available on PATH.
