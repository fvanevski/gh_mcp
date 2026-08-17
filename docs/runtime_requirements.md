# Runtime requirements

## GitHub CLI

`mcp-gh-server` requires **GitHub CLI (`gh`) 2.97.0 or newer**.

The server does not install or upgrade `gh`; deployment is responsible for satisfying this
runtime dependency. Confirm the active binary before starting the server:

```bash
gh --version
```

### Why 2.97.0 is the minimum

The exact Actions log readers (`gh_get_job_logs` and `gh_get_run_logs`) stream the GitHub
Actions job-log REST response through `gh api`. GitHub CLI 2.97.0 introduced
`--allow-escape-sequences`, which permits that raw response to reach the MCP server instead of
being rejected by the CLI's terminal-output guard when escape sequences are present.

The MCP server enables that option only inside the governed read-only `GhClient.stream_text`
path used for exact Actions job-log retrieval. Direct model-supplied
`--allow-escape-sequences` arguments remain rejected, non-API opt-in remains rejected, and the
option is default-disabled for every other streaming read.

After receipt, `ActionLogEvidenceAccumulator` converts terminal controls to inert visible
`\\xNN` text before hashing, UTF-8 byte accounting, max/tail selection, and literal-marker
selection. LF, CR, and TAB remain unchanged. This keeps returned evidence printable while the
reported SHA-256 and byte counts describe the complete normalized evidence stream before
selection.

### Unsupported older releases

`gh <= 2.96.x` is outside the supported runtime contract. Those releases do not accept
`gh api --allow-escape-sequences`, so deploying this server against them can make exact Actions
log reads fail at the CLI argument-parsing boundary. Do not add an implicit compatibility
fallback for older `gh` versions without treating that as a deliberate runtime-compatibility
design change and adding corresponding regression coverage.

## OpenSSL for reviewer GitHub Apps

The preferred issue-#75 reviewer-principal path requires the deployment `openssl` executable
to sign short-lived GitHub App JWTs with the configured RSA private key. The server invokes
`openssl` directly without a shell, passes only the private-key file path in argv, and passes
the JWT signing input on stdin. Private-key contents are never placed in argv.

This requirement applies only when `MCP_GH_REVIEWER_APP_ID` /
`MCP_GH_REVIEWER_INSTALLATION_ID` / `MCP_GH_REVIEWER_PRIVATE_KEY_FILE` are configured.
Static `MCP_GH_REVIEWER_TOKEN` compatibility deployments do not use `openssl`.

Confirm availability before enabling the App reviewer path:

```bash
openssl version
```

## Validation expectations

For changes affecting GitHub CLI invocation, validate both the focused subprocess/protocol
contract and the repository-wide gates:

```bash
uv run pytest tests/test_gh_client.py tests/test_action_log_evidence.py tests/test_action_log_protocol.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

The commands above reproduce the repository's current 0.9.0 validation documentation.
Issue work governed by the newer project workflow must additionally run the repository-pinned
Pyrefly gate when that tooling exists. Issue #75 does not silently add, regenerate, or weaken
type-check tooling or a Pyrefly baseline.

Subprocess tests for the Actions log path should assert the complete expected `gh` argv rather
than checking only for the presence of an endpoint substring or a single flag. This prevents a
malformed command shape from satisfying protocol coverage accidentally.
