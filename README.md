# MCP 2.0 GitHub CLI Server

A Python MCP server for the ``gh`` CLI. It uses the official MCP Python SDK 2.x,
runs `gh` asynchronously without a terminal, and returns structured results from
direct JSON output or a post-write readback.

## Tools

### Read-only (24)

- `gh_server_info`: report the deployed MCP server and tool-schema version without
  contacting GitHub or starting a subprocess.
- `gh_info`: gh CLI version, authentication status, and active account.
- `gh_search_repos`: search GitHub repositories with qualifiers.
- `gh_search_issues`: search issues and pull requests with qualifiers.
- `gh_search_code`: search source code with qualifiers.
- `gh_list_issues`: list issues in a repository with filters.
- `gh_get_issue`: get details of a specific issue or pull request.
- `gh_list_prs`: list pull requests in a repository.
- `gh_get_pr`: get details and exact base/head commit SHAs for a pull request.
- `gh_get_pr_diff`: read a bounded diff or patch pinned to the PR's exact base and
  head SHAs, with truncation metadata and a SHA-256 fingerprint.
- `gh_list_pr_files`: list one bounded page of changed files and patch fragments.
- `gh_list_pr_commits`: list one bounded page of commits in a pull request.
- `gh_get_repo`: get details of a specific repository.
- `gh_list_repos`: list repositories for a user or organization.
- `gh_list_releases`: list releases in a repository.
- `gh_get_release`: get details of a specific release.
- `gh_list_workflows`: list GitHub Actions workflows in a repository.
- `gh_get_workflow`: get details of a specific workflow.
- `gh_list_runs`: list recent GitHub Actions workflow runs.
- `gh_get_run`: get details of a specific workflow run.
- `gh_watch_run`: poll a workflow run until completion or a caller-supplied timeout.
- `gh_list_labels`: list labels in a repository.
- `gh_list_milestones`: list milestones in a repository.
- `gh_get_file_contents`: read a complete file at an exact branch, tag, or commit ref.

### Write (16)

- `gh_create_issue`: create a new issue (write, disabled by default).
- `gh_create_pr`: create a new pull request (write, disabled by default).
- `gh_create_repo`: create a new repository (write, disabled by default).
- `gh_create_release`: create a new release (write, disabled by default).
- `gh_run_workflow`: trigger a workflow dispatch event (write, disabled by default).
- `gh_edit_issue`: edit an existing issue (write, disabled by default).
- `gh_create_label`: create a new label (write, disabled by default).
- `gh_upsert_label`: create or overwrite a label (destructive write, disabled by default).
- `gh_edit_label`: edit an existing label (write, disabled by default).
- `gh_create_milestone`: create a new milestone (write, disabled by default).
- `gh_create_comment`: create a comment on an issue or PR (write, disabled by default).
- `gh_create_branch`: create a branch from an issue's PR (write, disabled by default).
- `gh_edit_pr`: edit an existing pull request (write, disabled by default).
- `gh_submit_pr_review`: submit a formal review pinned to an exact PR head SHA
  (additive write, disabled by default).
- `gh_merge_pr`: merge an exact reviewed PR head with an explicit strategy
  (destructive write, separately disabled by default).
- `gh_commit_files`: atomically create or replace files in one branch commit
  (destructive write, separately disabled by default).

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

### ChatGPT plan and gateway limitations

The action surface is version `0.5.0`, but availability in
ChatGPT depends on the account plan and integration surface:

- OpenAI currently limits full custom MCP apps, including write/modify actions,
  to Business and Enterprise/Edu workspaces.
- A ChatGPT Plus user may be able to install or discover a custom plugin, but the
  plugin's MCP gateway is a separate, more limited integration. This project does
  not assume that gateway supports arbitrary custom MCP tools or write actions.
- Seeing `gh_get_file_contents` or `gh_commit_files` in a discovery response proves
  only that the server advertised the tools. It does not prove that the Plus plugin
  gateway will route either invocation to the server.

The Business/Enterprise **Action control**, action-refresh, and workspace-publish
instructions do not apply to a Plus account. If the Plus gateway reports that the
plugin or `gh_CLI` namespace has been disabled, there may be no user-accessible
action setting that can re-enable the tool in that conversation.

#### Tentative Plus schema-refresh procedure

Limited testing indicates that the Plus custom-plugin gateway may retain a cached
tool schema after the backend changes. Deleting and reinstalling the custom plugin
appears to force rediscovery of the revised tools and may be necessary when tool
names, parameters, annotations, or result schemas change:

1. Increment the project, package, and MCP server version whenever a deployed
   revision changes tool names, schemas, annotations, or routing behavior.
2. Deploy the revised backend and restart the MCP server.
3. Delete the existing custom plugin from ChatGPT Plus.
4. Reinstall the plugin so the gateway scans the backend's current tool definitions.
5. In a new conversation, call `gh_server_info` and confirm both
   `server_version` and `tool_schema_version` match the expected deployment.
6. Only then test the revised GitHub tools.

This procedure is based on observed behavior rather than a documented compatibility
guarantee. A backend restart alone may leave the Plus gateway using stale tool
definitions, while deletion and reinstallation may still fail if the gateway does
not support a particular tool or capability.

`gh_server_info` is intentionally the smallest and safest possible verification
call. It takes no model-controlled arguments, performs no external I/O, starts no
subprocess, triggers no elicitation or approval flow, and returns only bounded local
metadata. `gh_info` is not a substitute: it reports the installed GitHub CLI version,
not the deployed MCP server version.

See OpenAI's current
[MCP app availability](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
and
[plugin availability](https://help.openai.com/en/articles/20001256).

At `INFO`, both repository-content tools log a content-free reachability marker:

```text
MCP tool invocation reached server: tool=gh_get_file_contents
```

The three focused PR-review tools emit the same marker using their own tool name.

The version probe emits the equivalent marker with `tool=gh_server_info`.

If ChatGPT reports that the app or namespace is disabled and this marker is absent,
the call was rejected by ChatGPT's plugin gateway before reaching the MCP server.
Restarting `gh`, changing the GitHub token, or changing this server's command
implementation cannot repair that host-side state. On Plus, full validation should
therefore use a standard MCP client such as the local stdio or Streamable HTTP
configurations above; passing those checks does not establish compatibility with
ChatGPT's limited custom-plugin gateway.

## Read-only pull-request review without checkout

The server deliberately does not expose a generic command executor or a standalone
checkout operation. A checkout performed by this backend would exist on the MCP
server's filesystem, not in ChatGPT's local environment, and a path alone would not
provide a safe review workspace. The focused review tools instead operate through
noninteractive GitHub reads and return bounded structured results:

1. Call `gh_get_pr` and record its exact `base_sha` and `head_sha`.
2. Call `gh_get_pr_diff` for a unified `diff` or email-style `patch`. The server
   resolves the PR's object IDs and reads the comparison by those immutable SHAs.
3. Check `truncated`, `bytes_returned`, `total_bytes`, and `sha256`. A truncated
   result is not a complete diff and must not be described as one.
4. Page through `gh_list_pr_files` and `gh_list_pr_commits` as needed. The server
   rechecks the SHA pair after each numbered-PR page and rejects the result if the
   snapshot changed during the read. GitHub may omit or truncate an individual
   file's `patch`, and the server also bounds patch fragments and commit messages;
   inspect their truncation fields and use the unified diff plus
   `gh_get_file_contents` at the returned SHAs for complete file inspection.
5. If the PR changes during review, restart from the new exact SHA pair rather than
   combining observations from different snapshots.

`gh_get_pr_diff` returns at most `MCP_GH_MAX_PR_DIFF_BYTES` UTF-8 bytes. A caller may
request a smaller limit, but cannot raise the deployment cap above 1,000,000 bytes:

```dotenv
MCP_GH_MAX_PR_DIFF_BYTES=500000
MCP_GH_MAX_PR_FILE_PATCH_BYTES=8000
MCP_GH_MAX_PR_COMMIT_MESSAGE_BYTES=4000
```

This workflow is valid for source-level, read-only review. It does not check out a
worktree, inspect generated or untracked files, install dependencies, build code, or
run tests. A validation record should state that boundary explicitly, for example:

> Reviewed the pull request using GitHub metadata, diff data, and repository file
> contents pinned to the recorded base and head SHAs. No local checkout, build, or
> test execution was performed.

If acceptance requires execution, use a separate isolated repository runner with a
managed workspace, bounded commands, cancellation, cleanup, and exact-head-SHA
validation. The read-only MCP tools are not a substitute for that environment.

## Formal pull-request review and merge

`gh_create_comment` creates an issue-style conversation comment; it does not submit
a GitHub pull-request review and cannot produce the formal `APPROVED`,
`CHANGES_REQUESTED`, or `COMMENTED` review states. Use `gh_submit_pr_review` when a
formal disposition is required.

The safe completion sequence is:

1. Read and review the PR using `gh_get_pr`, `gh_get_pr_diff`, file pages, commit
   pages, and exact-ref file reads. Record the returned `head_sha`.
2. Call `gh_submit_pr_review` with that SHA and one of `approve`,
   `request_changes`, or `comment`. A body is mandatory for the latter two actions.
3. Confirm the structured result's `state`, `commit_sha`, and `review_id`. The tool
   submits the review with GitHub's `commit_id` field and rejects a stale head before
   writing.
4. If merge is separately authorized, call `gh_merge_pr` with the same exact head
   SHA and an explicit `merge`, `squash`, or `rebase` strategy.
5. Treat the PR as merged only when the result reports `merged: true`. A successful
   command may instead report a merge queue or unmet requirements; formal review
   submission by itself never merges or closes the PR.

Both operations are focused tools with bounded text fields. They start no nested MCP
elicitation, inherit no stdin, and return structured readback. Review request bodies
are transferred through a temporary JSON input file; merge bodies are supplied on
controlled stdin. If a write succeeds but readback fails, the response is marked as
partial success and instructs the caller not to retry automatically.

`gh_merge_pr` deliberately exposes no administrator bypass, branch deletion, or
automatic-merge switch. It passes GitHub CLI's `--match-head-commit` guard so a
force-push or new commit cannot silently change the authorized merge target. GitHub
permissions and branch protection still apply, and an author generally cannot
approve their own pull request.

## Write-command policy

Write execution is off by default:

```dotenv
MCP_GH_ALLOW_WRITE_COMMANDS=false
```

To enable writes:

```dotenv
MCP_GH_ALLOW_WRITE_COMMANDS=true
```

Write tools do not initiate nested MCP elicitation. A compatible MCP host is
responsible for presenting any user-facing action approval. The ChatGPT Plus
custom-plugin gateway may reject write tools instead of offering approval. The
server independently enforces the write-enable flag, optional repository policy,
and high-risk operation switches before starting `gh`.

Limit enabled writes to explicit repositories or owners:

```dotenv
MCP_GH_ALLOWED_REPOSITORIES=fvanevski/project-a,fvanevski/project-b
MCP_GH_ALLOWED_OWNERS=fvanevski
```

When either allowlist is non-empty, a target is accepted if its exact
`owner/repo` or its owner is listed. Fine-grained GitHub token permissions
remain the primary GitHub-side authorization boundary.

Repository creation, release creation, workflow dispatch, repository-content
commits, and PR merging require separate opt-in because they can have broader effects:

```dotenv
MCP_GH_ALLOW_REPO_CREATION=true
MCP_GH_ALLOW_RELEASE_CREATION=true
MCP_GH_ALLOW_WORKFLOW_DISPATCH=true
MCP_GH_ALLOW_CONTENT_COMMITS=true
MCP_GH_ALLOW_PR_MERGE=true
```

Enable only the operations the deployment actually needs; all five default to
`false`.

`gh_commit_files` accepts complete UTF-8 file contents, validates repository-relative
paths, and creates all supplied files in one Git tree and one commit. It conditionally
advances the named branch only if it still points to `expected_head_sha`; the update
uses GitHub's atomic `updateRefs` mutation with `beforeOid` and never forces a ref.
The operation does not support file deletion. Bound its request size with:

```dotenv
MCP_GH_MAX_COMMIT_FILES=100
MCP_GH_MAX_FILE_BYTES=1000000
MCP_GH_MAX_COMMIT_BYTES=5000000
```

If the final ref-update response is interrupted, the tool reads the branch before
reporting the outcome. An indeterminate result explicitly requires a fresh read and
must not be retried automatically.

## Operational limits

- Search tools use GitHub's `gh search` subcommands with `--json` output.
- Results are bounded by `MCP_GH_DEFAULT_MAX_RESULTS` (default: 30) and
  capped at `MCP_GH_HARD_MAX_RESULTS` (default: 100).
- All output is JSON-safe: `Decimal` → string, `bytes` → `base64:`
  prefix, datetimes → ISO 8601, infinities → string.
- Logs are sent to stderr so stdio protocol output is not corrupted.
- Every `gh` process is noninteractive, receives either closed or explicitly
  supplied stdin, and runs with prompting, pagers, Git credential prompts,
  spinners, color, and update notices disabled.
- User-authored bodies and notes are supplied through stdin rather than command
  arguments. Debug command logs redact titles and other free-form values.
- Commands run asynchronously, are terminated with their process group on
  timeout or cancellation, and default to `MCP_GH_COMMAND_TIMEOUT_SECONDS=30`.
- Write results distinguish write completion from structured readback. A
  partial-success warning explicitly instructs callers to verify before retrying.
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

- The server runs `gh` as a subprocess, including allowlisted REST and GraphQL
  calls inside focused tools; it does not expose a generic command or API executor.
  Rate limits, authentication, and permission
  scoping are governed by the `gh` CLI and the token in `GITHUB_TOKEN`.
- Commands like `gh release create` that require file uploads or complex
  multi-step flows are intentionally out of scope — they would need a
  dedicated maintenance tool.
- The `gh` CLI must be installed and available on PATH.
