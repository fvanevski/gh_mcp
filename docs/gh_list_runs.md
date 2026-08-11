# `gh_list_runs` exact-filter and completeness contract

`gh_list_runs` returns one bounded page of GitHub Actions workflow runs. Issue #13 extends the existing `branch`, `status`, and `per_page` interface without removing or renaming those parameters.

## Authoritative server-side filters

The tool uses GitHub's workflow-runs REST API rather than locally filtering a broad `gh run list` result. Without `workflow_id` it calls the repository workflow-runs route. With an exact positive `workflow_id` it calls the workflow-scoped runs route. Both routes receive the applicable filters directly:

- `branch`
- `status`
- `head_sha` — exactly 40 hexadecimal characters
- `event`
- `actor`
- `created` — derived from `created_from` / `created_to`
- `check_suite_id`

The tool performs exactly one workflow-run discovery request per invocation; it does not poll, watch, rerun, dispatch, or locally post-filter workflow runs. After that bounded discovery response, it resolves only the distinct `workflow_id` values present on the returned page through exact workflow-metadata reads. Because a page can contain at most 100 runs, these compatibility reads are bounded to at most 100 distinct workflow IDs and are deduplicated within the page.

## Creation ranges and timezone handling

`created_from` and `created_to` are inclusive bounds. Each supplied bound must be a whole-second ISO 8601 timestamp containing an explicit UTC offset or `Z`. Naive timestamps, date-only values, any fractional-second syntax (including zero-valued forms such as `.000Z`), and reversed ranges are rejected before GitHub is contacted.

Bounds are normalized to UTC before being sent to GitHub. For example, `2026-08-01T03:00:00-07:00` becomes `2026-08-01T10:00:00Z`. Two bounds are encoded as GitHub's inclusive `START..END` range. A single lower bound uses `>=TIMESTAMP`; a single upper bound uses `<=TIMESTAMP`.

## Pagination and completeness

`page` is one-based. `per_page` remains optional for backward compatibility. The effective page size is bounded by both `MCP_GH_HARD_MAX_RESULTS` and GitHub's maximum workflow-runs page size of 100. The response always includes:

- `total_count` — GitHub's count for the query;
- `page` and effective `per_page`;
- `has_more` — whether another page exists inside the authoritative accessible result window;
- `truncated` — whether the requested/result evidence cannot be treated as complete;
- `warning` — an explicit reason when a bound or completeness limitation applies.

Requesting more than the effective hard page size is never silently clipped. If additional results exist beyond the capped page, `truncated=true`; even when the entire result fits, `warning` records that the requested page size was capped.

GitHub documents an additional maximum of 1,000 results for workflow-run searches using `actor`, `branch`, `check_suite_id`, `created`, `event`, `head_sha`, or `status`. When GitHub's returned `total_count` reaches or exceeds that boundary, `gh_list_runs` conservatively sets `truncated=true` and warns that global completeness beyond the accessible search window is not established. At the end of that accessible window, `has_more=false` rather than directing callers to an inaccessible page.

## Backward-compatible item semantics

Run items retain the historical `gh run list --json` keys used by existing callers: `databaseId`, `name`, `displayTitle`, `headBranch`, `headSha`, `conclusion`, `status`, `event`, `url`, `createdAt`, `updatedAt`, `startedAt`, and `workflowName`. The REST response is normalized to those keys; pagination metadata is added at the enclosing result level.

`name` and `workflowName` are intentionally distinct. `name` comes from the workflow-run payload. `workflowName` is resolved from the run's exact `workflow_id`, matching the historical GitHub CLI behavior rather than aliasing the run `name`. If exact workflow metadata returns HTTP 404—for example for an organization/enterprise ruleset workflow that is visible as a run but whose workflow metadata cannot be read—`workflowName` is the empty string while the run remains in the result. Non-404 workflow-metadata failures are surfaced rather than silently fabricating a name.
