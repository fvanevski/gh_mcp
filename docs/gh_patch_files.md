# Exact-context repository patch contract

Issue #80 adds `gh_patch_files` as the bounded partial-edit counterpart to
`gh_commit_files`. The new tool changes only how complete replacement contents are
materialized; both tools share the same content-commit authorization, exact-head
commit construction, single branch compare-and-swap, and bounded exact-ref
reconciliation path.

## Public request

`gh_patch_files` accepts:

- `owner`, `repo`, and an existing `branch`;
- `expected_head_sha`, an exact 40-character commit SHA;
- one or more `patches`, each targeting one existing repository-relative path and
  containing one or more exact-context edits;
- `commit_message`.

Each edit contains non-empty `old_text` and `new_text`. `new_text` may be empty.

The tool is intentionally not a generic patch engine. It does not accept unified
diffs, offsets, line numbers, fuzzy context, regular expressions, arbitrary Git
arguments, or shell input.

## Exact-context materialization

All edit matching is performed against the original file content at
`expected_head_sha`, never against content produced by an earlier edit in the same
request.

Before creating any Git blob, tree, or commit object, the server:

1. verifies the target branch is exactly at `expected_head_sha`;
2. resolves every target through the immutable base tree;
3. requires every target to be an existing regular (`100644`) or executable
   (`100755`) blob;
4. checks the blob size advertised by the Git tree and rejects a target above
   `MCP_GH_MAX_FILE_BYTES` before requesting its blob content;
5. reads each permitted exact blob, caps the base64 payload before decoding, rechecks
   the decoded-byte bound, and requires supported UTF-8 text content;
6. requires each `old_text` to occur exactly once in that original file;
7. rejects overlapping source spans;
8. deterministically materializes all replacements against the original snapshot;
9. enforces the existing path, file-count, per-file byte, aggregate commit-byte,
   commit-message, ordinary write, repository-policy, and content-commit fine-gate
   bounds;
10. rechecks the mutable branch head immediately before creating the first Git
    object.

A missing or non-unique context, overlapping span, missing target, symlink, binary
or non-UTF-8 target, bound violation, or stale/moved branch therefore fails before
content-object mutation.

### Resource-bound behavior

The file-size setting is a resource bound, not merely a post-decode validation rule.
For normal GitHub blob tree entries, the advertised `size` is checked before the
blob-content GET so an oversized existing target does not require transferring or
decoding its complete base64 body first. The encoded and decoded payload are checked
again as fail-closed backstops before text decoding.

After all exact source spans are resolved and overlap-checked, output construction
walks the ordered spans once and joins untouched original slices with replacements in
one assembly. It does not rebuild the complete file once per edit. This keeps the
materialization allocation proportional to the input/output size rather than the
number of edits times the file size.

## Shared commit/CAS path

Once every patch has been materialized into complete UTF-8 file contents,
`gh_patch_files` and `gh_commit_files` use the same internal materialized-content
commit service.

That service:

- creates replacement blobs and one tree;
- preserves the patch target's original `100644` or `100755` mode exactly;
- creates one commit whose sole parent is the exact pre-write head;
- performs exactly one GraphQL `updateRefs` CAS with that head as `beforeOid`,
  the created commit as `afterOid`, and `force=False`;
- never retries or replays an ambiguous mutation;
- applies the issue #73 bounded exact-ref reconciliation contract after the
  single CAS attempt.

The patch tool does not recursively call the public `gh_commit_files` facade and
does not maintain a second CAS/readback state machine.

A known CAS failure after blob/tree/commit object creation is still one mutation
attempt: exact-ref readback may establish that the branch remained at the old head,
but the mutation is not replayed. Likewise, successful/ambiguous CAS outcomes that
continue to expose only the old head through the bounded readback window remain
unresolved rather than being converted into a false negative; a distinct third-party
head is a conclusive semantic mismatch.

## Result evidence

In addition to the shared exact-write outcome fields, `gh_patch_files` returns:

- `previous_head_sha`;
- created `commit_sha` and `tree_sha`;
- tri-state `ref_updated`;
- final valid `observed_head_sha` and `readback_attempts`;
- `changed_file_count`;
- `applied_edit_count`;
- ordered `changed_paths`;
- per-file path, preserved mode, `before_blob_sha`, and `after_blob_sha`;
- request identity and warnings from the shared write/readback contract.

An ambiguous CAS remains `write_completed=None` even if readback later proves that
the created commit is installed. Exhausted old-head observations remain unresolved,
not disproven. A distinct third-party head is a conclusive semantic mismatch. The
mutation is never replayed.

## Regression authority

`tests/test_patch_files.py` remains the primary issue #80 functional regression
suite. `tests/test_patch_files_review_regressions.py` adds the independent-review
closure cases that must remain covered:

- invalid repository paths reject before repository calls;
- an oversized existing target is rejected from tree-size evidence before blob fetch;
- an oversized materialized result rejects before Git-object creation;
- the maximum supported edit count preserves deterministic original-snapshot
  materialization under the single-pass output assembly;
- a known CAS failure after object creation attempts the CAS exactly once;
- exhausted old-head reconciliation remains unresolved without replay; and
- a distinct third-party post-CAS head remains a conclusive mismatch without replay.

These focused regressions supplement, rather than replace, the full repository Ruff,
format, Pyrefly, pytest, schema, and release-gate validation sequence.

## Non-goals

`gh_patch_files` does not create, delete, or rename files; change file modes; patch
symlinks or binary content; expose arbitrary Git/GitHub API/shell execution; provide
fuzzy matching; or alter the public `gh_commit_files` full-content replacement
contract.
