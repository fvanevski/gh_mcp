# Bounded GitHub Actions log evidence

`gh_get_job_logs` and `gh_get_run_logs` are read-only evidence tools for inspecting
successful or failed GitHub Actions logs without exposing arbitrary shell, regular
expression, rerun, cancel, delete, workflow-dispatch behavior, or an unbounded run-log
archive download.

## Acquisition and authority contract

All GitHub reads still enter through `GhClient` and the shared `GitHubRequestGovernor`.
The general log tools do **not** use `gh run view --log` and do **not** call GitHub's
workflow-run or run-attempt `/logs` archive endpoints.

Instead:

- `gh_get_job_logs` resolves the exact job, resolves the caller-supplied exact run
  attempt, proves that the job is a member of that attempt, and streams the job's
  plaintext `actions/jobs/{job_id}/logs` resource.
- `gh_get_run_logs` resolves the exact run attempt, enumerates the complete job set for
  that attempt under `MCP_GH_MAX_ACTION_LOG_JOBS`, and streams each non-skipped job's
  plaintext job-log resource in ascending numeric job-ID order.
- The run aggregate inserts exactly one `LF` (`\n`) separator between streamed job-log
  sources. That separator is part of the normalized aggregate evidence for byte-count
  and digest purposes.
- Both tools require completed identities before reading logs and re-read the exact
  identity after retrieval. Run aggregation additionally re-enumerates the complete job
  set and fails closed if job membership changed.

The streaming `GhClient` path drains stdout incrementally. It does not use
`process.communicate()` for the log body and does not materialize the complete source in
memory. Streaming reads are deliberately not retried after partial output has reached the
evidence accumulator, because replaying a partial source into the same sink would corrupt
byte counts and digests. They still use the shared governor for serialization and
rate-limit state.

## Job-count bound

`gh_get_run_logs` must know the complete job set in order for `total_bytes`, `sha256`, and
identity verification to describe the complete normalized run aggregate. The deployment
therefore bounds job enumeration with:

```dotenv
MCP_GH_MAX_ACTION_LOG_JOBS=100
```

The default is 100 jobs. Values must be a multiple of 100 and may be configured from 100
to 1000. If an attempt contains more jobs than the deployment cap, `gh_get_run_logs`
fails closed before any job log is streamed. `gh_get_job_logs` also uses this cap while
paging the exact attempt's job collection to prove membership; a target beyond the
configured scan bound fails closed and requires an explicit deployment-cap increase.
Neither tool silently omits membership evidence while claiming an exact result.

## Selection and memory bound

The complete normalized plaintext stream is scanned incrementally for byte accounting and
SHA-256, while only bounded selector state is retained:

- no selector: retain a UTF-8-safe prefix;
- `tail_bytes`: retain a bounded rolling UTF-8 byte suffix;
- `start_marker` / `end_marker`: retain only the bounded returned prefix plus at most the
  marker-overlap state needed to recognize literal markers across stream chunks.

Markers are literal strings, not regular expressions. `tail_bytes` cannot be combined
with marker selection. A supplied marker that is absent fails closed after the source
stream has been scanned rather than returning evidence from a different location.

The retained/returned evidence limit is:

```dotenv
MCP_GH_MAX_ACTION_LOG_BYTES=500000
```

The default is 500,000 UTF-8 bytes and the maximum is 1,000,000. A caller may request a
smaller `max_bytes` but cannot raise the deployment cap. This limit bounds retained and
returned evidence; it is not a promise that GitHub will transfer only that many source
bytes. Full-source scanning is required when computing complete-source byte counts and
digests, resolving a tail selector, or proving that an end marker is absent.

`total_bytes` is the UTF-8 byte length of the complete normalized source evidence stream.
`bytes_returned` is the UTF-8 byte length actually returned in `text`. `truncated=true`
whenever returned text is incomplete relative to that complete normalized source,
including marker selection, tail selection, or a byte cap. Truncated evidence always has
an explicit warning.

A UTF-8 tail boundary may land inside a multibyte code point. The returned suffix drops
that incomplete leading code point and the warning reports the **actual** returned suffix
byte count rather than claiming that the requested byte count was necessarily returned.

## Digest semantics

`sha256` is the lowercase SHA-256 digest of the **complete normalized evidence stream
before any marker, tail, or max-byte selection**.

For `gh_get_job_logs`, that stream is the incrementally decoded plaintext emitted by the
exact job-log read. For `gh_get_run_logs`, it is the stable job-ID-ordered concatenation
of non-skipped job-log streams with one synthetic LF separator between job sources. The
streaming decoder uses UTF-8 with replacement for invalid byte sequences, so the digest
and byte counts describe the normalized UTF-8 text exposed by this server rather than the
redirect transport container or any workflow-run ZIP archive.

## Failure semantics

The tools fail closed when, among other cases:

- the requested run attempt does not exist or GitHub returns a different identity;
- the job cannot be proven to belong to the caller-supplied attempt;
- the run/job is not completed and its logs are therefore not yet immutable;
- a run contains more jobs than `MCP_GH_MAX_ACTION_LOG_JOBS`;
- pagination is inconsistent, incomplete, or returns duplicate jobs;
- pre/post run, job, head-SHA, or run-job membership evidence changes;
- a requested literal marker is absent;
- authentication, permissions, retention expiry, transport, or rate limits prevent the
  exact read.

No deletion, rerun, cancellation, dispatch, generic API command, arbitrary shell, regex,
or retry-after-partial-stream behavior is exposed by either MCP tool.
