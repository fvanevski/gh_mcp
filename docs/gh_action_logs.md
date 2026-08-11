# Bounded GitHub Actions log evidence

`gh_get_job_logs` and `gh_get_run_logs` are read-only evidence tools for inspecting
successful or failed GitHub Actions logs without exposing arbitrary shell, regular
expression, rerun, cancel, delete, or workflow-dispatch behavior.

## Identity contract

`gh_get_run_logs` requires an explicit `run_id` and `attempt`. The server resolves that
exact attempt before and after reading the log and rejects an attempt mismatch or an
identity change; it never silently substitutes the latest attempt.

`gh_get_job_logs` requires an exact `job_id` and explicit `attempt`. GitHub's individual
job metadata identifies the containing run and head SHA but does not identify the run
attempt, so the server additionally verifies that the job ID occurs in the caller-supplied
attempt before and after the log read. This avoids falsely associating an older job with
a newer attempt of the same run.

## Selection and byte accounting

The complete text returned by the governed `gh` log read is the source evidence. The
server then applies at most one selection mode followed by the normal output byte cap:

- no selector: return a bounded UTF-8 prefix;
- `tail_bytes`: return a UTF-8-safe suffix, then apply `max_bytes` if it is smaller;
- `start_marker` / `end_marker`: select an inclusive literal substring, then apply
  `max_bytes`.

Markers are literal strings, not regular expressions. `tail_bytes` cannot be combined
with marker selection. A supplied marker that is absent fails closed rather than
returning evidence from a different location.

`total_bytes` is the UTF-8 byte length of the complete retrieved source log.
`bytes_returned` is the UTF-8 byte length actually returned in `text`. `truncated` is
true whenever the returned text does not cover the complete source evidence, including
marker selection, tail selection, or a byte cap. Truncated evidence always includes an
explicit warning.

The deployment-level hard cap for these two general log tools is
`MCP_GH_MAX_ACTION_LOG_BYTES` (default `500000`, maximum `1000000`). A caller may request
a smaller `max_bytes` but cannot raise the deployment cap.

## Digest semantics

`sha256` is the lowercase SHA-256 digest of the **complete retrieved source log before
any marker, tail, or max-byte selection**. It therefore fingerprints the source evidence
used for the read and is intentionally not the digest of the returned slice when
`truncated=true`.

The digest covers the text as returned by this server's ordinary `GhClient` stdout
normalization. It is not a digest of GitHub's underlying ZIP archive or other transport
container.
