# Exact release creation contract

`gh_create_release_exact` is the release-grade creation primitive for callers that need an
immutable target SHA, fail-closed tag/release absence checks, a single mutation attempt, and
authoritative readback. It is intentionally separate from the compatibility
`gh_create_release` surface.

## Preconditions

The tool requires an exact 40-character `expected_target_sha` and re-resolves that commit
immediately before mutation. `expected_tag_absent` and `expected_release_absent` default to
`true`.

Release absence is evaluated across **all release states**, including drafts. GitHub's
published-release-by-tag endpoint is not sufficient for that invariant because draft releases
are unpublished. The implementation therefore scans the paginated repository release listing
for an exact `tag_name` match and refuses to interpret the listing as draft-complete unless the
repository response proves authenticated `permissions.push == true`. GitHub documents that
only users with push access receive draft releases in release listings and that release
creation itself requires push access.

The scan uses 100 releases per page. It continues until an exact match is found or GitHub
returns a short page. A 10,000-item safety bound prevents an unbounded precondition sequence;
reaching the bound is an **unverified absence** error, never permission to write.

## Mutation and readback

The creation request sends the exact normalized SHA as `target_commitish` and sends
`make_latest` explicitly. Drafts and prereleases are rejected when `make_latest=true` before
any GitHub request.

There is exactly one mutation attempt. Mandatory release readback always uses the same
draft-complete exact-tag scan as the precondition; it does not rely on the published-only
tag endpoint or on the release-ID endpoint's public-release contract. When the successful
creation response includes a release identifier, the all-state readback must return that same
identifier. For transport-ambiguous writes where no identifier is available, the exact-tag
all-state result remains semantic evidence while `write_completed` stays unknown. The mutation
is never replayed automatically.

Semantic success additionally requires:

- release tag name equals the requested tag;
- the exact tag ref resolves to `expected_target_sha` (including annotated-tag peeling);
- draft/prerelease state equals the request;
- explicit latest/non-latest state equals `make_latest`;
- supplied release name/body match authoritative readback.

A completed mutation with failed or mismatched readback is returned through the shared exact
write contract and must not be treated as verified success.

## Regression coverage

`tests/test_release_exact.py` covers target mismatch/missing target, existing tag, existing
published and draft releases, push-access and permission failures, pagination and bounded
absence, successful prerelease and draft readback, release-ID mismatch, tag-target mismatch,
ambiguous published and draft mutations, write gating, and invalid latest+draft/prerelease
combinations.

## References

- GitHub REST API, Releases: <https://docs.github.com/en/rest/releases/releases>
- GitHub CLI, `gh release create`: <https://cli.github.com/manual/gh_release_create>
