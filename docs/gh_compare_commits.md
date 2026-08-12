# `gh_compare_commits` evidence contract

`gh_compare_commits` is a read-only comparison primitive for issue #22. It accepts only exact
40-character base and head commit SHAs and does not resolve branch names or tags.

## Authoritative identity and status

A successful comparison returns the requested exact `base_sha` and `head_sha`, the exact
`merge_base_sha`, GitHub's comparison status (`identical`, `ahead`, `behind`, or `diverged`),
`ahead_by`, `behind_by`, and `total_commits`.

A compare-route `404` is not treated as proof that a commit is missing. The tool reuses the
exact-commit lookup contract and reports `base_found` / `head_found` only after the existing
access probe has distinguished an absent exact commit from permission or transport ambiguity.
Other permission, authentication, and transport failures remain errors.

## Bounded collection evidence

Commit and changed-file collections are bounded independently by caller limits, server hard
limits, and GitHub's compare-route limits. Each collection reports:

- `returned_count`
- `total_count` when authoritative
- `complete`
- `truncated`
- `sha256`
- `warning` when evidence is incomplete

The collection model rejects known under-returned evidence that claims completeness or fails
to report truncation. It also rejects `complete=true` when the authoritative total is unknown.

GitHub's compare API can saturate changed-file evidence at 300 files without exposing a
complete file count. The tool therefore returns at most 299 files and marks a 300-file
upstream response incomplete with `total_count=null`.

The jq projection separately records whether GitHub actually supplied array-valued `commits`
and `files` collections before projecting bounded metadata. If either upstream collection is
missing or null, the tool fails closed; absence is never normalized into authoritative empty
evidence.

## Commit-message evidence

Each returned commit message follows the shared bounded-text contract:

- `message`
- `message_bytes_returned`
- `message_total_bytes`
- `message_truncated`
- `message_sha256`
- `message_warning`

`message_sha256` fingerprints the complete source message supplied to the bounded-text helper,
not merely the returned prefix. The model validates returned UTF-8 byte length, total byte
count, truncation state, and warning presence. A truncated commit message also makes the
commit collection and overall comparison evidence incomplete.

## Validation expectations

Regression coverage must include exact-SHA rejection, all four comparison statuses, merge-base
identity, independent commit/file bounds, 300-file saturation, fail-closed missing upstream
collections, commit-message byte truncation and total-byte accounting, missing-commit
classification, permission/transport propagation, collection-model completeness invariants,
digest determinism, MCP schema/protocol registration, and the repository's full validation
gate:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Validation claims are meaningful only for the exact PR head on which the commands were run.
