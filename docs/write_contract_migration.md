# Historical write-contract migration record

This document records the staged compatibility migration that preceded the 0.8.0 canonical write architecture. It is historical implementation evidence, not current public API or current routing authority.

Current write-contract authority is defined by:

- `docs/write-schema-contract.md` for the canonical public write facade and execution invariants; and
- `docs/release_gate_0_8_0.md` for the released 0.8.0 inventory and integration gate.

## Historical purpose

The earlier migration preserved the frozen 0.6.x public surface while exact write/readback contracts were introduced incrementally. Compatibility adapters temporarily:

1. preserved existing write enablement and high-risk action gates;
2. executed mutations once through the governed `GhClient` boundary;
3. preserved `GitHubRequestResult` metadata when available;
4. performed structured authoritative readback where the frozen resource identity permitted it;
5. compared readback with the requested semantic state;
6. avoided automatic replay after ambiguous mutations; and
7. projected newer tri-state outcomes onto older compatibility result shapes where required.

Those adapters were transitional infrastructure. They are removed in 0.8.0 and are no longer a valid execution, schema, or testing dependency.

## Historical compatibility surface

During the transition, compatibility coverage included issue/label/milestone writes, pull-request writes, repository/content writes, branch writes, generic release creation, generic workflow dispatch, and `gh_upsert_label`.

The following weaker public names were subsequently retired rather than preserved as aliases:

- `gh_create_release` → canonical public release creation is `gh_create_release_exact`;
- `gh_run_workflow` → canonical public dispatch is `gh_run_workflow_exact`; and
- `gh_upsert_label` → callers choose explicit `gh_create_label` or `gh_edit_label` semantics.

`gh_create_comment` was migrated from the historical unverified compatibility behavior to one governed REST creation attempt plus authoritative readback of the immutable returned comment ID.

## Invariants carried forward into 0.8.0

The compatibility layer is gone, but the safety invariants it helped preserve remain requirements of the canonical implementations:

- master write authorization and operation-specific fine gates remain fail-closed;
- exact target/state/SHA preconditions are preserved where prescribed;
- one caller invocation never blindly replays an ambiguous mutation;
- authoritative readback is used when GitHub exposes stable identity;
- structured request/ambiguity metadata remains distinct from semantic readback state;
- `@me` assignee selection is normalized to the authenticated concrete login for authoritative comparison without broadening reviewer syntax;
- merge evidence does not treat an arbitrary auto-merge request as proof of the requested merge method;
- content commits retain exact branch-head compare-and-swap semantics; and
- host interception is reported separately from server rejection or GitHub ambiguity.

## 0.8.0 supersession

Issue #61 completes the migration by removing the obsolete `legacy_*write*` modules, `legacy_write_support.py`, `legacy_assignee_support.py`, and the `legacy_write_status` projection. `server.py` registers the 18 public write facades exactly once; domain implementation modules do not independently register the same MCP write names.

Tests must therefore validate the canonical architecture directly. They must not restore compatibility modules, generic retired writes, boolean legacy projections, or importability assertions merely to preserve old migration scaffolding.

Historical release documents for 0.7.0 and 0.7.1 remain unchanged records of their shipped inventories. Current executable inventory, schema, version, and release validation belong to the 0.8.0 release gate.
