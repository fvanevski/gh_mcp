# `gh_get_ref` exact-reference contract

`gh_get_ref` is the 0.7.x exact-reference primitive introduced by issue #11. It is a
read-only MCP tool for immutable branch and tag identity; it does not perform matching-ref
search, local checkout, arbitrary `gh`, arbitrary public `gh api`, or shell execution.

## Exact input and identity

The `ref` input is relative to `refs/` and must be either `heads/<branch>` or `tags/<tag>`.
Malformed Git ref names and matching-style inputs are rejected before GitHub is contacted.
The implementation uses GitHub's exact Git-reference endpoint and verifies that the returned
full `refs/...` name is identical to the requested ref before accepting object evidence.

A found ref returns its direct Git object type, SHA, and URL. A lightweight tag therefore
retains its direct commit identity. An annotated tag retains the tag-object identity and is
peeled by exact tag-object SHA reads. Peeling is bounded to 16 tag objects, rejects cycles,
and fails closed if the exact object chain cannot be established. `peeled_commit_sha` is set
only when the chain actually terminates at a commit.

## Missing-ref and failure classification

GitHub documents two distinct absence-related boundaries for Git-database reads:

- `404` means the exact ref was not found. Before returning `found=false`, `gh_get_ref`
  performs a separate bounded branch-list read so a repository/auth/permission failure is
  not silently converted into absence.
- `409` may mean that the Git repository is empty or unavailable. `gh_get_ref` therefore
  does **not** map 409 to absence directly. It performs the read-only
  `gh repo view OWNER/REPO --json isEmpty` probe and returns `found=false` only when GitHub's
  authoritative `Repository.isEmpty` field is the boolean `true`. `false`, malformed probe
  data, or a failed probe remains an error.

Authentication, permission, transport, rate-limit, malformed-response, ambiguous 409, and
annotated-tag evidence failures are not reported as missing refs.

All GitHub reads continue to execute through `GhClient.run` and the shared request governor.

## Version-gate discipline

Issue #25 owns the 0.7.0 package/server/tool-schema release bump and explicitly requires that
bump to occur only after the full planned 0.7.0 surface is complete. Issue #11 therefore does
not independently change `pyproject.toml`, `mcp_gh_server.__version__`, or
`tool_schema_version` to 0.7.0. Intermediate 0.7.0 development commits are not release-gate
commits and must not be treated or deployed as a completed 0.7.0 release.

This preserves the repository's declared release ordering instead of allowing each child tool
PR to publish a partial surface under the final 0.7.0 version.

## Regression requirements

Focused tests cover branch refs, lightweight and annotated tags, nested peeling, cycles, the
exact 16-object peel boundary, over-bound chains, 404 absence, 409 empty-repository absence,
ambiguous/non-empty 409 failure, failed probes, malformed refs, mismatched returned refs, and
auth/permission/transport failures.

The repository release/acceptance validation remains:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```
