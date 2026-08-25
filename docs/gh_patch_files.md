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
4. reads each exact blob and requires supported UTF-8 text content;
5. requires each `old_text` to occur exactly once in that original file;
6. rejects overlapping source spans;
7. deterministically materializes all replacements against the original snapshot;
8. enforces the existing path, file-count, per-file byte, aggregate commit-byte,
   commit-message, ordinary write, repository-policy, and content-commit fine-gate
   bounds;
9. rechecks the mutable branch head immediately before creating the first Git
   object.

A missing or non-unique context, overlapping span, missing target, symlink, binary
or non-UTF-8 target, bound violation, or stale/moved branch therefore fails before
content-object mutation.

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

## Non-goals

`gh_patch_files` does not create, delete, or rename files; change file modes; patch
symlinks or binary content; expose arbitrary Git/GitHub API/shell execution; provide
fuzzy matching; or alter the public `gh_commit_files` full-content replacement
contract.
