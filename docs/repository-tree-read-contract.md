# Exact repository-tree read contract

`gh_list_repository_tree` is the bounded structural-discovery companion to
`gh_get_file_contents`.

## Intended workflow

1. Establish immutable repository identity. A mutable branch/tag is resolved outside this
   tool with `gh_get_ref` and, when needed, `gh_get_commit`.
2. Call `gh_list_repository_tree` with one exact 40-character commit SHA.
3. Discover a repository-relative blob path from the returned tree evidence.
4. Pass that path and the same commit SHA to `gh_get_file_contents` for complete blob content.

`gh_list_repository_tree` does not read file contents and `gh_get_file_contents` remains
file-only.

## Inputs and bounds

The public inputs are `owner`, `repo`, exact `commit_sha`, optional repository-relative
`path` (empty means repository root), `recursive=false` by default, and optional
`max_entries`.

`max_entries` is clamped by the existing `MCP_GH_DEFAULT_MAX_RESULTS` /
`MCP_GH_HARD_MAX_RESULTS` policy. The tool may therefore return fewer entries than GitHub
supplies. It never exposes arbitrary GitHub API endpoints, command arguments, shell input,
local paths, clone/checkout behavior, or mutation controls.

A non-empty directory path uses the same repository-relative safety invariants as file paths:
no leading slash, backslash, control characters, empty/`.`/`..` components, or `.git`
component. Validation completes before GitHub access.

## Exact-state traversal

The implementation reads the supplied SHA through GitHub's exact Git commit route and
requires the returned commit identity to match. It records the commit's exact root tree SHA.
For a nested directory, each path component is then resolved through exact non-recursive Git
tree-object reads. Every component must exist exactly and have Git object type `tree`;
missing components and existing blob/submodule components are distinct failures.

The target directory is read by its exact tree SHA. `recursive=false` returns immediate
children only. `recursive=true` requests GitHub's recursive tree representation. Returned
paths are normalized back to repository-relative paths rooted at the repository, including
when the requested directory is nested.

Entries preserve GitHub's exact object `type`, `mode`, SHA, and blob size when supplied.
Accepted object/mode pairs are the Git Trees API forms used for blobs, trees, symlinks,
executables, and submodules; malformed or mismatched identity evidence fails closed.

All GitHub requests execute through `GhClient`, and therefore through the shared
`GitHubRequestGovernor`. The tool never enters a write-policy path.

## Completeness semantics

The result exposes `entries_returned`, `truncated`, `evidence_complete`, and `warning`.

- If the requested/server `max_entries` bound omits otherwise returned entries,
  `truncated=true` and `evidence_complete=false`.
- If GitHub reports its tree response as truncated, the same incomplete state is surfaced
  even when the application cap was not reached.
- If both conditions apply, the warning records both causes.
- Truncated intermediate evidence is never used to claim successful nested-directory
  traversal; traversal fails closed instead.

No partial structural response is represented as complete.
