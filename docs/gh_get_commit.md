# `gh_get_commit` exact-commit contract

`gh_get_commit` is the 0.7.x exact-commit evidence primitive introduced by issue #12. It is a read-only MCP tool for one immutable Git commit object; it does not accept branch names, tag names, abbreviated SHAs, arbitrary `gh`, arbitrary public `gh api`, or shell execution.

## Exact input and identity

`commit_sha` must be exactly 40 hexadecimal characters. The implementation normalizes hexadecimal case, calls GitHub's exact Git Database commit-object route, and verifies that the returned full SHA is identical to the requested commit before accepting any evidence.

A found commit returns its exact commit SHA, tree SHA, every parent SHA in GitHub's returned order, raw Git author and committer identity/date fields, complete commit message, and GitHub's commit-signature verification object. Merge commits therefore retain all parent identities rather than being reduced to one lineage.

The verification result preserves GitHub's `verified`, `reason`, `signature`, `payload`, and `verified_at` fields. The server does not reinterpret `reason`, cryptographically re-verify the signature, or upgrade GitHub's verification state into a stronger provenance claim.

## Missing-commit and failure classification

A `404` from the exact commit read is not treated as sufficient proof of absence by itself. Before returning `found=false`, the tool performs a separate bounded branch-list read to establish repository Contents-read access. If that probe fails, its authentication, permission, transport, or other operational error is preserved instead of being converted into a missing commit.

A `409` is also not mapped directly to absence. The tool reads GitHub's authoritative repository `isEmpty` state and returns `found=false` only when that value is exactly `true`. A non-empty repository, malformed empty-state evidence, or failed probe remains an error.

Malformed successful responses also fail closed: the returned commit SHA must match the request exactly, the tree SHA must be a full object SHA, every parent must be a full object SHA, author/committer fields must be complete strings, the message must be a string, and verification metadata must have the expected types.

All GitHub reads continue to execute through `GhClient.run` and the shared request governor with read-only MCP annotations.

## Version-gate discipline

Issue #25 owns the 0.7.0 package/server/tool-schema release bump after the planned 0.7.0 surface is complete. Issue #12 therefore does not independently change `pyproject.toml`, `mcp_gh_server.__version__`, or `tool_schema_version` to 0.7.0.

## Regression requirements

Focused regression coverage includes a normal commit, a merge commit with multiple parents, exact-SHA input rejection, missing-commit classification, empty/non-empty 409 handling, authentication/permission/transport failures, verification-field preservation, mismatched returned identity, and malformed tree/parent evidence.

The repository acceptance commands remain:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Any source or test change after validation invalidates the affected exact-head evidence and requires validation to be rerun on the resulting full branch HEAD SHA.
