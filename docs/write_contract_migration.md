# Issue #9 write-contract migration

This document records the compatibility migration for the frozen 0.6.x write
surface. It is implementation evidence for issue #9, not a new public API.

## Shared invariant

Every public write is routed through a compatibility adapter that:

1. preserves existing write enablement and high-risk action gates;
2. executes the mutation once through the governed `GhClient` boundary;
3. preserves `GitHubRequestResult` metadata when the production client supplies it;
4. performs structured authoritative readback when the legacy resource identity permits it;
5. compares readback with the requested semantic state;
6. never automatically replays an ambiguous mutation; and
7. projects the shared tri-state outcome conservatively onto the frozen boolean
   `write_completed` / `readback_completed` fields.

An ambiguous transport outcome therefore projects to `write_completed=false` in
0.6.x while the warning retains the "unknown" state and request identity. New
0.7.x result models must expose the tri-state field directly.

## Compatibility matrix

| Tool | Legacy precondition | Authoritative/readback invariant |
| --- | --- | --- |
| `gh_create_issue` | append-only/server-assigned identity | created issue URL/number and requested fields available in readback |
| `gh_edit_issue` | no caller exact-state field in frozen schema | requested title/body/label/assignee/milestone changes |
| `gh_create_label` | GitHub create-only label semantics | label name/color/description |
| `gh_upsert_label` | no caller exact-state field in frozen schema | requested label name/color/description |
| `gh_edit_label` | no caller exact-state field in frozen schema | requested rename/color/description |
| `gh_create_milestone` | append-only/server-assigned identity | milestone number/title/state and supplied optional fields |
| `gh_create_comment` | append-only | no structured typed readback in frozen wrapper; reports `readback_completed=false` |
| `gh_create_pr` | append-only/server-assigned identity | PR identity plus requested PR fields exposed by the legacy read route |
| `gh_edit_pr` | no caller exact-state field in frozen schema | requested title/body/base/label/assignee changes |
| `gh_submit_pr_review` | exact `expected_head_sha` immediately before mutation | review id/state/commit/body |
| `gh_merge_pr` | exact `expected_head_sha` immediately before mutation plus `--match-head-commit` | merged/queued state, or matching auto-merge method |
| `gh_create_repo` | GitHub create-only repository identity | canonical repository identity |
| `gh_commit_files` | atomic GraphQL `beforeOid` compare-and-swap | branch ref equals newly created commit |
| `gh_create_release` | GitHub tag/release create semantics | release tag/identity plus exact requested draft/prerelease booleans and supplied name |
| `gh_run_workflow` | dispatch is append-only | exact run id when `gh` returns a stable run URL; otherwise unverified |
| `gh_create_branch` | GitHub issue-development create semantics | frozen wrapper has no structured exact ref readback; reports unverified |
| `gh_create_branch_from_sha` | exact commit resolution immediately before mutation | exact ref name and commit SHA |

## Merge-method invariant

`gh_merge_pr` must not treat an arbitrary `autoMergeRequest` as proof that the
requested merge state exists. When auto-merge is the evidence, its
`mergeMethod` is normalized and compared with the requested `merge`, `squash`,
or `rebase` method. Merge-queue state is separate because GitHub controls the
queue's final merge strategy.

## Regression requirements

The test suite must retain:

- the 44-tool schema/count/classification snapshot;
- core precondition, known-failure, readback-failure, semantic-mismatch, and
  ambiguous-transport executor tests;
- exact-branch and exact-head regressions;
- a 17-tool compatibility binding inventory;
- successful governor warning/request-id propagation through a real adapter, including request ID with no governor warning;
- wrong-method auto-merge rejection after ambiguous transport failure;
- false draft/prerelease release-state rejection after a failed write; and
- conservative `readback_completed=false` behavior where the frozen wrapper
  cannot perform structured authoritative readback.

No test, assertion, validation gate, or write authorization check may be relaxed
to make the migration pass.
