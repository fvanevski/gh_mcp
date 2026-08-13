# `gh_get_api_rate_status`

`gh_get_api_rate_status` is a zero-argument, read-only diagnostic for GitHub primary
rate-limit evidence and the server's local request-governor state.

## Authority and provenance

The result deliberately separates two kinds of evidence:

- `github` contains values observed from the most recent governed `GET /rate_limit`
  response. Header-derived fields are under `github.headers`; body-derived primary
  resource buckets are under `github.primary_resources`.
- `governor` contains local policy state: active read/write blocking, write pacing,
  the active cooldown reason/deadline, and retained metadata for the last relevant
  rate-limit or abuse event.

Local policy is never presented as a GitHub limit. In particular, the server does not
encode GitHub's mutable secondary-rate-limit thresholds as correctness constants.

## Primary rate resources

The diagnostic does not assume that `core` is the only primary resource.

`github.headers` exposes the source response's `X-RateLimit-Resource`,
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Used`, and
`X-RateLimit-Reset` values when GitHub supplies them.

`github.primary_resources` parses every valid resource object returned in the
`resources` map. Resource names are retained as strings so newly introduced GitHub
resource buckets remain visible without a server code change. `github.primary` remains
the `core` entry when present for compatibility with the original issue implementation.

## Diagnostic polling policy

A successful `GET /rate_limit` observation is cached locally for
`MCP_GH_API_RATE_STATUS_MIN_INTERVAL_SECONDS` seconds. The default is 5 seconds and
the configured value cannot be lower than 1 second.

Within that interval:

- no new GitHub request is created;
- `github.request_performed` is `false`;
- `github.cached` is `true`;
- `github.cache_age_seconds` reports the age of the source observation;
- `github.observed_at_epoch` and `github.request_id` continue to identify the original
  GitHub observation.

The cache is guarded by an async lock, so concurrent diagnostic calls coalesce rather
than racing into multiple `/rate_limit` requests. The source request still runs through
the shared `GitHubRequestGovernor`; the cache is not an alternate GitHub execution path.

This refresh interval is a local anti-polling safety policy. It is not inferred from,
and must not be interpreted as, a GitHub secondary-rate-limit threshold.

## Governor blocking

If GitHub supplies `Retry-After`, or a primary response reports zero remaining requests
with a future reset time, the shared governor blocks subsequent GitHub operations until
the corresponding deadline. A rate/abuse response without usable reset information uses
the governor's existing configurable fallback cooldown.

The diagnostic reports active local state through:

- `reads_blocked` / `writes_blocked`;
- `blocked_until_epoch`;
- `retry_after_seconds`;
- `block_reason` (`retry_after`, `primary_reset`, or `fallback`);
- `last_rate_event_at_epoch`;
- `last_rate_request_id`;
- `last_rate_warning`.

Expired active block state is cleared, while the last-event metadata remains available
for post-event diagnosis.

## Failure semantics

The tool is read-only and exposes no repository target, arbitrary `gh`, arbitrary public
`gh api`, shell, mutation, retry override, or polling/watch argument.

A governor-blocked diagnostic does not execute GitHub. If the request itself receives a
rate-limit response, that response's available request/rate metadata is returned while
the resulting governor cooldown is reported separately.

Non-rate-limit transport or command failures are not converted into a successful
diagnostic result. They propagate through the existing governed binary-read boundary;
the diagnostic request is not transparently retried after response-body observation.

## Regression coverage

Issue #24 coverage includes:

- normal primary capacity;
- exhausted primary capacity and reset wait;
- `Retry-After`;
- secondary/abuse fallback handling;
- stale governor-state expiry;
- local write-pacing state;
- repeated unblocked calls coalesced by the refresh cache;
- refresh-boundary behavior;
- source header resource/limit/remaining/used/reset capture;
- multiple and previously unknown primary resource buckets;
- schema/protocol/return-model registration.
