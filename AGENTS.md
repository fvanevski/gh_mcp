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
uv run ruff check .
uv run ruff format --check .
uv run --with-requirements requirements-typecheck.txt pyrefly check
uv run pytest
```

`gh >= 2.97.0` is a runtime requirement. Actions log readers depend on the
`gh api --allow-escape-sequences` behavior introduced in 2.97.0. Do not validate against
older releases without an explicit compatibility design change.

Pyrefly is the sole authoritative Python static type checker for CI/review/release decisions.
`requirements-typecheck.txt` pins its executable independently from the runtime/dev lock,
and `[tool.pyrefly]` in `pyproject.toml` defines project scope. Changed Python tests must also
be supplied explicitly to Pyrefly because the historical test corpus is excluded from the
no-argument project scope.

Integration tests skip when `GITHUB_TOKEN` is absent; all other tests run offline.

## Project layout

| Path | Purpose |
|---|---|
| `src/mcp_gh_server/server.py` | Composition root — registers every public write exactly once and imports self-registered reads |
| `src/mcp_gh_server/tooling.py` | Shared helpers: annotations, write gates, repository validation, evidence |
| `src/mcp_gh_server/tools/` | Read-only tools plus canonical write implementations by domain |
| `src/mcp_gh_server/current_write_tool_schema.py` | Canonical current 0.9.0 public write inventory — 18 non-review writes plus 3 action-specific formal-review writes |
| `src/mcp_gh_server/patch_write_schema.py` | Focused host-facing facade for `gh_patch_files` |
| `src/mcp_gh_server/pr_review_tool_schema.py` | Dedicated action-specific formal-review facade for approve/request-changes/comment-review |
| `src/mcp_gh_server/write_tool_schema.py` | Internal facade source for the established non-review writes; not the complete current public-inventory authority |
| `src/mcp_gh_server/content_commit_service.py` | Shared materialized-content Git object, exact ref-CAS, and bounded reconciliation state machine |
| `src/mcp_gh_server/write_contracts.py` | Shared exact-state and tri-state mutation/readback contract |
| `src/mcp_gh_server/models.py` | Pydantic result schemas used by tests and tool annotations |
| `src/mcp_gh_server/patch_models.py` | Pydantic request/result models for exact-context patch writes |
| `src/mcp_gh_server/settings.py` | Runtime config, loaded once via cached `get_settings()` |
| `src/mcp_gh_server/serialization.py` | JSON-safe serialization helpers |
| `tests/test_release_gate_0_9_0.py` | Current package/schema/inventory/registration release gate |
| `tests/test_patch_files.py` | Exact-context patch semantics, failure ordering, one-commit/CAS, and ambiguity regressions |
| `tests/test_write_wrappers.py` | Broad regression coverage for write and adjacent read tools |
| `tests/test_mcp_protocol.py` | Protocol-level registration/schema/annotation contract tests |
| `tests/test_integration.py` | Live-GitHub integration tests requiring `GITHUB_TOKEN` |

Key invariant: `current_write_tool_schema.py` owns the current host-facing public write
inventory, composing 18 non-review writes with the 3 dedicated formal-review writes.
`gh_patch_files` is explicitly supplied by `patch_write_schema.py`; the other established
non-review facades remain sourced from `write_tool_schema.py`. `server.py` is the sole MCP
registration path for all 21 current public write names. `gh_submit_pr_review` is retired
from the current inventory and must not be restored merely because its internal historical
wrapper remains available to legacy code/tests.

The obsolete `legacy_*write*` compatibility adapters, `legacy_write_support.py`,
`legacy_assignee_support.py`, and the `legacy_write_status` projection are not part of the
current architecture and must not be restored merely to satisfy historical tests.

## Runtime architecture

- **Transport**: stdio (default) or Streamable HTTP on `127.0.0.1:8766`. Switch via
  `MCP_GH_TRANSPORT`.
- **Settings loading**: `settings.get_settings()` is cached (`@lru_cache`). Calling it
  again returns the same instance — do not mutate returned objects.
- **Logging always goes to stderr** so it never corrupts the MCP protocol channel.
- **Environment file**: `.env` in the project root, or an absolute path via
  `MCP_GH_ENV_FILE`. `GITHUB_TOKEN` can also come from the host process environment.

## Security model (do not bypass)

Every write tool is governed by the master write policy and repository/owner target policy.
Higher-risk classes add separate fine gates, all default-off:

1. `MCP_GH_ALLOW_WRITE_COMMANDS=true` — master write gate (default: `false`)
2. `MCP_GH_ALLOWED_REPOSITORIES=` or `MCP_GH_ALLOWED_OWNERS=` — ordinary target allowlist
3. Per-operation fine gates:
   - `MCP_GH_ALLOW_REPO_CREATION`
   - `MCP_GH_ALLOW_RELEASE_CREATION`
   - `MCP_GH_ALLOW_WORKFLOW_DISPATCH`
   - `MCP_GH_ALLOW_CONTENT_COMMITS`
   - `MCP_GH_ALLOW_PR_MERGE`

Repository creation and workflow dispatch additionally require their exact target policy.
Their target lists default empty, so an enabled fine gate alone is insufficient.

Ambiguous mutations are not blindly replayed. Where stable identity exists, canonical
writes perform authoritative readback and return explicit tri-state outcome metadata.
A partial or ambiguous result instructs callers to read authoritative state before any
subsequent write.

Branch tools distinguish contracts deliberately:

- `gh_create_branch` — issue-linked branch creation with exact repository/issue/base-OID
  evidence and authoritative linked-branch/ref readback.
- `gh_create_branch_from_sha` — accepts an exact 40-character commit SHA and never moves
  or overwrites an existing ref.

Repository content writes deliberately have two public request shapes but one mutation
state machine:

- `gh_commit_files` accepts complete UTF-8 replacement contents, validates path/request
  bounds, builds one commit, and conditionally advances one existing branch by exact CAS.
- `gh_patch_files` accepts bounded exact-context edits for existing UTF-8 regular/executable
  files only. Every `old_text` must occur exactly once in the immutable original blob; all
  edits resolve against that original and overlapping source spans are rejected. It preserves
  supported modes, validates all targets before object creation, rechecks the exact branch
  head immediately before object creation, and cannot create/delete/rename files.

Both delegate Git blob/tree/commit creation, the single `updateRefs` CAS, and bounded
exact-ref reconciliation to `content_commit_service.py`. Neither force-updates a ref, and an
ambiguous CAS is never blindly replayed. See `docs/gh_patch_files.md` for the focused patch
contract.

PR authors cannot `approve` their own pull request — the server checks login identity before
the POST and returns an explicit no-write error. `request_changes` has no equivalent
server-side identity restriction; GitHub remains authoritative.

## PR review workflow

For issue #75 / version 0.9.0 review workflows, require `server_version` and
`tool_schema_version` >= `0.9.0` before relying on the dedicated formal-review surface.

1. `gh_get_pr` → record exact `head_sha` and `base_sha`.
2. `gh_get_pr_diff` → read bounded diff/patch. Check `truncated`, `bytes_returned`,
   `total_bytes`, and `sha256`.
3. Page through `gh_list_pr_files` and `gh_list_pr_commits`. Re-fetch identity when the
   reviewing workflow requires current numbered-PR evidence.
4. Use `gh_get_file_contents` at exact SHAs for complete file content.
5. If the PR head changes, invalidate the old review and restart from the new exact head.
6. Use `gh_get_pr_review_eligibility` as the exact-head preflight for formal review.
7. Use `gh_approve_pr` for an independent APPROVED review, `gh_request_pr_changes` for
   CHANGES_REQUESTED, or `gh_comment_pr_review` for the ordinary-principal COMMENTED
   fallback when GitHub approval is not eligible.

`gh_submit_pr_review` is retired and must not be used or restored as the current formal-review
interface. Merge requires `gh_merge_pr` with the same exact reviewed head SHA and an explicit
strategy.

## Commands agents will need

```bash
# Confirm runtime compatibility floor
gh --version

# Current release gate and focused contracts
uv run pytest tests/test_release_gate_0_9_0.py

# Full validation suite
uv run ruff check .
uv run ruff format --check .
uv run --with-requirements requirements-typecheck.txt pyrefly check
uv run pytest
git diff --check

# Changed-test type validation: pass every changed Python test explicitly, for example:
uv run --with-requirements requirements-typecheck.txt pyrefly check tests/test_patch_files.py

# Optional live integration tests
GITHUB_TOKEN=<token> uv run pytest tests/test_integration.py

# Start MCP Inspector
uv run mcp dev src/mcp_gh_server/server.py

# Launch stdio server
uv run mcp-gh

# Launch Streamable HTTP
MCP_GH_TRANSPORT=streamable-http uv run mcp-gh
```

## Testing conventions

- Reuse the repository's existing fake-client patterns rather than inventing parallel test
  harnesses.
- Async execution uses `asyncio_mode = "auto"` in `pyproject.toml`; new tests do not need
  explicit `@pytest.mark.asyncio` decorators.
- Integration tests live in `tests/test_integration.py` and skip without `GITHUB_TOKEN`.
- Never commit `.env` files. `.env.example` is the canonical template.
- Historical `tests/test_release_gate_0_7_0.py`, `tests/test_release_gate_0_7_1.py`, and
  `tests/test_release_gate_0_8_1.py` preserve documentary release history; current executable
  authority belongs to `tests/test_release_gate_0_9_0.py`.
- Never relax an exact-state assertion, negative/fail-closed regression, schema bound,
  fine gate, or no-blind-retry invariant merely to make the suite green.
- Do not add static-analysis suppressions or baselines merely to avoid typing a changed test;
  narrow optional/dynamic schemas explicitly in test helpers instead.

## Local agent tooling contract

Use the local stack narrowly:

- **Probe**: first-line broad repository discovery for version/schema/registration/legacy
  patterns and compact source extraction. Do not use Probe LSP as a competing semantic
  authority.
- **Serena (`no-memories`)**: exact symbols/references/dependencies/diagnostics and scoped
  structural edits after discovery is narrowed. Do not use Serena as a generic shell.
- **RTK**: efficiency layer for routine pytest/Ruff/Pyrefly/search output when filtering cannot
  change the decision. Use native output for exact SHAs, complete diffs, decisive failures,
  release/security evidence, or anything truncation-sensitive.
- **OpenViking**: bounded historical rationale only. Never authority for current source,
  Git/GitHub state, version/inventory, CI, runtime, or release readiness.
- **Native git/gh/runtime tools**: authoritative local Git state, branch/commit operations,
  authenticated GitHub reads/writes not delegated to Central ChatGPT, and host/service
  evidence. Do not route local `git` or `gh` through the MCP server under test.

OpenCode roles remain scoped to their configured profiles: `plan`/`chat-audit` for gate
mapping, `review`/`chat-review` for evidence audit, `build`/`chat` only for bounded authorized
remediation, and `chat-fast` only for mechanical assistance. Do not widen global permissions
to bypass a failed gate.

## Release authority

Version 0.9.0 release authority is `docs/release_gate_0_9_0.md` plus
`tests/test_release_gate_0_9_0.py`. Required closure evidence must belong to one exact
candidate SHA and includes package/server/tool-schema/lock agreement, exact 62/41/21 tool
inventory, schema snapshots, canonical single-registration proof, compatibility-path absence,
focused write/readback and fail-closed tests, Ruff, format, Pyrefly, full pytest, and the
representative live replay required by the current 0.9.0 gate.

Host interception during live replay is classified separately from server fine-gate
rejection, GitHub failure/ambiguity, and completed authoritative readback. Do not weaken
truthful metadata or safety contracts solely to move an action past host interception.

## Known constraints

- The `gh` CLI must be installed on `$PATH` at version **2.97.0 or newer**. The server
  never installs or manages it.
- Rate limits, authentication scope, and permission scoping are delegated to `gh` and the
  supplied token, while the server independently enforces its write/target policies.
- Generic `gh release create` file-upload/multi-step flows are outside the public tool
  surface; release creation uses the exact canonical contract.
- `gh_patch_files` is text-only and existing-file-only; binary/symlink edits,
  create/delete/rename operations, fuzzy matching, and mode changes are deliberately absent.
- Search field names follow GitHub/API output conventions rather than an invented local
  naming layer.
- Result serialization converts `Decimal` → string, `bytes` → `base64:` prefix,
  infinities → string, and datetimes → ISO 8601.
- Detailed current contracts live under `docs/`, especially `docs/release_gate_0_9_0.md`,
  `docs/search-read-contract.md`, `docs/write-schema-contract.md`, `docs/gh_patch_files.md`,
  `docs/pr-review-evidence-contract.md`, and the focused Git/ref/workflow/release documents.
