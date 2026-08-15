# Search and issue-list read contract

This document defines the bounded read semantics for `gh_search_repos`,
`gh_search_issues`, `gh_search_code`, and the label-filter path of
`gh_list_issues`.

## GitHub Search item and count evidence

Each `gh_search_*` tool performs two read-only requests:

1. `gh search ... --json ... --limit <N>` supplies the bounded item payload.
2. One matching `gh api search/... -X GET -f q=<query> -f per_page=1`
   supplies GitHub's reported `total_count` and `incomplete_results` fields.

These requests are intentionally separate and therefore are not an atomic snapshot.
The server preserves already-returned item evidence instead of turning an upstream
partial-count condition into a complete tool failure.

When the Search REST response is structurally valid:

- if `incomplete_results=false` and `total_count >= len(items)`, the reported
  count is used directly and `truncated` is true only when fewer items were
  returned than that count;
- if `incomplete_results=true`, the item payload is still returned and
  `truncated=true`;
- if the later REST count is smaller than the number of already-returned items,
  the server treats the two-read evidence as incomplete, reconciles
  `total_count` upward to `len(items)`, and sets `truncated=true`.

Malformed count evidence remains fail-closed. A non-object response, a non-boolean
`incomplete_results`, or a negative/non-integer `total_count` raises an error.

`truncated=true` must therefore be interpreted conservatively: either the caller's
bounded item limit omitted matches, GitHub marked the Search count incomplete, or
the non-atomic item/count reads observed count drift. It must not be presented as
complete search evidence.

## Code-search boundary

`gh search code` is backed by GitHub CLI's code-search command. The server does not
convert an upstream `incomplete_results=true` count response into a namespace/tool
failure. It returns the bounded matches already obtained and marks the result
truncated.

## Issue label filtering

`gh_list_issues(labels=...)` maps its comma-separated label value to the GitHub CLI
contract:

```text
gh issue list ... --label <labels>
```

The implementation must use the singular `--label` flag. `--labels` is not a
supported `gh issue list` option.

## Regression coverage

- `tests/test_search_total_count.py` covers complete counts, upstream
  `incomplete_results=true`, non-atomic count drift below returned item count,
  malformed evidence, and query serialization across repository/issue/code search.
- `tests/test_issue_list_labels.py` pins singular `--label` argv construction and
  verifies that no label option is added when the filter is omitted.
- `tests/test_integration.py` contains an optional authenticated live CLI smoke test
  for the supported issue-list label flag.

These changes preserve the public `SearchResults` model, the 58/40/18 tool inventory,
all read/write annotations, and every write gate.
