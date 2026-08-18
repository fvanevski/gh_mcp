"""Regression coverage for exact-head merge-requirement aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from mcp_gh_server.models import PullRequestCheck
from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.merge_requirements import gh_get_merge_requirements


@dataclass
class FakeGhClient:
    """Record exact GitHub reads and return queued values."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _context(client: FakeGhClient, settings: Settings | None = None) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=settings or Settings(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _pr(
    *,
    base_sha: str = "a" * 40,
    head_sha: str = "b" * 40,
    base_ref: str = "main",
    mergeable: bool | None = True,
    merge_state: str = "clean",
) -> dict[str, Any]:
    return {
        "base": {"ref": base_ref, "sha": base_sha},
        "head": {"ref": "feature", "sha": head_sha},
        "mergeable": mergeable,
        "mergeable_state": merge_state,
    }


def _review(
    review_id: int,
    state: str,
    commit_id: str,
    *,
    reviewer: str,
    submitted_at: str,
) -> dict[str, Any]:
    return {
        "id": review_id,
        "user": {"login": reviewer},
        "state": state,
        "commit_id": commit_id,
        "submitted_at": submitted_at,
        "body": f"review {review_id}",
        "author_association": "COLLABORATOR",
    }


def _requested() -> dict[str, Any]:
    return {"users": [], "teams": []}


def _graphql(
    *,
    decision: str | None,
    unresolved: bool = False,
) -> dict[str, Any]:
    nodes = (
        [
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "path": "src/example.py",
                "line": 12,
                "originalLine": 10,
                "comments": {"totalCount": 1},
            }
        ]
        if unresolved
        else []
    )
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewDecision": decision,
                    "reviewThreads": {
                        "totalCount": len(nodes),
                        "pageInfo": {"hasNextPage": False},
                        "nodes": nodes,
                    },
                }
            }
        }
    }


def _repo_methods(
    *,
    merge: bool = True,
    squash: bool = True,
    rebase: bool = True,
) -> dict[str, Any]:
    return {
        "allow_merge_commit": merge,
        "allow_squash_merge": squash,
        "allow_rebase_merge": rebase,
    }


def _classic_protection(
    *,
    dismiss_stale_reviews: bool = False,
    linear_history: bool = False,
) -> dict[str, Any]:
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": ["ci"],
            "checks": [{"context": "ci", "app_id": None}],
        },
        "required_pull_request_reviews": {
            "required_approving_review_count": 2,
            "dismiss_stale_reviews": dismiss_stale_reviews,
            "require_code_owner_reviews": True,
            "require_last_push_approval": False,
        },
        "required_conversation_resolution": {"enabled": False},
        "required_linear_history": {"enabled": linear_history},
    }


def _pinned_classic() -> dict[str, Any]:
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": ["ci"],
            "checks": [
                {"context": "lint", "app_id": 42},
                {"context": "ci", "app_id": None},
            ],
        },
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "require_last_push_approval": False,
        },
        "required_conversation_resolution": {"enabled": False},
        "required_linear_history": {"enabled": False},
    }


def _ruleset_rules() -> list[dict[str, Any]]:
    return [
        {
            "type": "pull_request",
            "parameters": {
                "allowed_merge_methods": ["merge", "squash"],
                "dismiss_stale_reviews_on_push": True,
                "required_approving_review_count": 3,
                "require_code_owner_review": False,
                "require_last_push_approval": True,
                "required_review_thread_resolution": True,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "lint", "integration_id": 42}],
            },
        },
        {"type": "required_linear_history"},
    ]


def _checks() -> list[dict[str, Any]]:
    return [
        {"name": "ci", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"},
        {"name": "lint", "state": "PENDING", "bucket": "pending", "workflow": "CI"},
    ]


async def test_merge_requirements_layers_policy_and_uses_current_valid_reviews() -> None:
    head = "b" * 40
    stale = "c" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            _ruleset_rules(),
            {"protected": True},
            _classic_protection(),
            _repo_methods(),
            _pr(head_sha=head),
            _checks(),
            _pr(head_sha=head),
            {"behind_by": 2},
            _pr(head_sha=head),
            [
                _review(
                    1,
                    "APPROVED",
                    head,
                    reviewer="alice",
                    submitted_at="2026-08-12T10:00:00Z",
                ),
                _review(
                    2,
                    "APPROVED",
                    stale,
                    reviewer="bob",
                    submitted_at="2026-08-12T10:01:00Z",
                ),
                _review(
                    3,
                    "APPROVED",
                    head,
                    reviewer="carol",
                    submitted_at="2026-08-12T10:02:00Z",
                ),
                _review(
                    4,
                    "CHANGES_REQUESTED",
                    head,
                    reviewer="carol",
                    submitted_at="2026-08-12T10:03:00Z",
                ),
            ],
            _requested(),
            _graphql(decision="CHANGES_REQUESTED", unresolved=True),
            _pr(head_sha=head),
            _pr(head_sha=head, merge_state="blocked"),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client),
    )

    assert result.head_matches_expected is True
    assert result.exact_head_evidence is True
    assert result.base_sha == "a" * 40
    assert result.current_head_sha == head
    assert result.mergeable is True
    assert result.merge_state == "blocked"

    assert result.policy_evidence_complete is True
    assert {(check.context, check.integration_id) for check in result.required_status_checks} == {
        ("ci", None),
        ("lint", 42),
    }
    assert [check.name for check in result.current_required_checks] == [
        "ci",
        "lint",
        "lint",
    ]
    integrated_placeholder = result.current_required_checks[2]
    assert integrated_placeholder.state == "UNKNOWN"
    assert integrated_placeholder.bucket == "pending"
    assert result.checks_evidence_complete is True

    assert result.required_approvals == 3
    assert [review.reviewer for review in result.current_valid_approvals] == ["alice"]
    assert result.current_valid_approval_count == 1
    assert result.review_decision == "CHANGES_REQUESTED"
    assert result.review_requirements_satisfied is False
    assert result.review_evidence_complete is True

    assert result.code_owner_review_required is True
    assert result.last_push_approval_required is True
    assert result.conversation_resolution_required is True
    assert [thread.id for thread in result.unresolved_review_threads] == ["T1"]

    assert result.up_to_date_required is True
    assert result.up_to_date is False
    assert result.up_to_date_evidence_complete is True
    assert result.allowed_merge_methods == ["squash"]
    assert result.allowed_merge_methods_complete is True
    assert result.warning is None

    for args, _kwargs in client.calls:
        if args[:1] == ("api",) and len(args) > 1 and args[1] != "graphql":
            assert args[args.index("-X") + 1] == "GET" if "-X" in args else None
        assert "--admin" not in args
        assert "--delete-branch" not in args
    graphql_calls = [args for args, _ in client.calls if args[:2] == ("api", "graphql")]
    assert len(graphql_calls) == 1
    assert any(arg.startswith("query=query ") for arg in graphql_calls[0])


async def test_merge_requirements_counts_preserved_stale_approval_when_policy_allows_it() -> None:
    head = "b" * 40
    stale = "c" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [],
            {"protected": True},
            _classic_protection(dismiss_stale_reviews=False),
            _repo_methods(),
            _pr(head_sha=head),
            _checks(),
            _pr(head_sha=head),
            {"behind_by": 0},
            _pr(head_sha=head),
            [
                _review(
                    1,
                    "APPROVED",
                    head,
                    reviewer="alice",
                    submitted_at="2026-08-12T10:00:00Z",
                ),
                _review(
                    2,
                    "APPROVED",
                    stale,
                    reviewer="bob",
                    submitted_at="2026-08-12T10:01:00Z",
                ),
            ],
            _requested(),
            _graphql(decision="APPROVED"),
            _pr(head_sha=head),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client),
    )

    assert result.required_approvals == 2
    assert [review.reviewer for review in result.current_valid_approvals] == ["alice", "bob"]
    assert result.current_valid_approval_count == 2
    assert result.review_requirements_satisfied is True


async def test_merge_requirements_head_mismatch_returns_identity_only() -> None:
    expected = "b" * 40
    current = "c" * 40
    client = FakeGhClient([_pr(head_sha=current, mergeable=None, merge_state="unknown")])

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        expected,
        ctx=_context(client),
    )

    assert result.current_head_sha == current
    assert result.head_matches_expected is False
    assert result.exact_head_evidence is False
    assert result.policy_evidence_complete is False
    assert result.current_required_checks == []
    assert result.required_approvals is None
    assert result.warning is not None
    assert len(client.calls) == 1


async def test_merge_requirements_unprotected_branch_reports_zero_known_requirements() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [],
            {"protected": False},
            _repo_methods(merge=True, squash=True, rebase=False),
            {"behind_by": 0},
            _pr(head_sha=head),
            [],
            _requested(),
            _graphql(decision=None),
            _pr(head_sha=head),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client),
    )

    assert result.policy_evidence_complete is True
    assert result.required_status_checks == []
    assert result.current_required_checks == []
    assert result.checks_evidence_complete is True
    assert result.required_approvals == 0
    assert result.code_owner_review_required is False
    assert result.last_push_approval_required is False
    assert result.conversation_resolution_required is False
    assert result.up_to_date_required is False
    assert result.up_to_date is True
    assert result.allowed_merge_methods == ["merge", "squash"]
    assert result.allowed_merge_methods_complete is True
    assert not any(args[:2] == ("pr", "checks") for args, _ in client.calls)


async def test_merge_requirements_missing_rule_visibility_is_not_no_requirement() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            GitHubRequestError("forbidden", status_code=403),
            {"protected": True},
            _classic_protection(),
            _repo_methods(),
            _pr(head_sha=head),
            [],
            _pr(head_sha=head),
            {"behind_by": 0},
            _pr(head_sha=head),
            [],
            _requested(),
            _graphql(decision="APPROVED"),
            _pr(head_sha=head),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client),
    )

    assert result.policy_evidence_complete is False
    assert result.required_status_checks == []
    assert result.required_approvals is None
    assert result.current_valid_approval_count is None
    assert result.code_owner_review_required is None
    assert result.last_push_approval_required is None
    assert result.conversation_resolution_required is None
    assert result.up_to_date_required is None
    assert result.allowed_merge_methods == []
    assert result.allowed_merge_methods_complete is False
    assert result.checks_evidence_complete is False
    assert result.warning is not None
    assert "HTTP 403" in result.warning


async def test_merge_requirements_head_movement_discards_collected_evidence() -> None:
    expected = "b" * 40
    moved = "d" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=expected),
            [],
            {"protected": False},
            _repo_methods(),
            {"behind_by": 0},
            _pr(head_sha=expected),
            [],
            _requested(),
            _graphql(decision=None),
            _pr(head_sha=expected),
            _pr(head_sha=moved, merge_state="dirty"),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        expected,
        ctx=_context(client),
    )

    assert result.current_head_sha == moved
    assert result.head_matches_expected is False
    assert result.exact_head_evidence is False
    assert result.policy_evidence_complete is False
    assert result.required_status_checks == []
    assert result.current_required_checks == []
    assert result.current_valid_approvals == []
    assert result.allowed_merge_methods == []
    assert result.warning is not None
    assert "discarded" in result.warning


async def test_merge_requirements_check_snapshot_movement_invalidates_aggregate() -> None:
    expected = "b" * 40
    moved = "d" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=expected),
            [],
            {"protected": True},
            _classic_protection(),
            _repo_methods(),
            _pr(head_sha=moved),
            _checks(),
            _pr(head_sha=moved),
            {"behind_by": 0},
            _pr(head_sha=expected),
            [],
            _requested(),
            _graphql(decision=None),
            _pr(head_sha=expected),
            _pr(head_sha=expected),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        expected,
        ctx=_context(client),
    )

    assert result.current_head_sha == expected
    assert result.head_matches_expected is True
    assert result.exact_head_evidence is False
    assert result.checks_evidence_complete is False
    assert result.current_required_checks == []
    assert result.warning is not None
    assert "discarded" in result.warning


async def test_merge_requirements_bounded_ruleset_visibility_is_incomplete() -> None:
    head = "b" * 40
    settings = Settings(default_max_results=1, hard_max_results=1)
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [_ruleset_rules()[0]],
            [_ruleset_rules()[1]],
            {"protected": False},
            _repo_methods(),
            _pr(head_sha=head),
            [],
            _pr(head_sha=head),
            {"behind_by": 0},
            _pr(head_sha=head),
            [],
            _requested(),
            _graphql(decision=None),
            _pr(head_sha=head),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client, settings),
    )

    assert result.policy_evidence_complete is False
    assert result.required_approvals is None
    assert result.allowed_merge_methods_complete is False
    assert result.warning is not None
    assert "exceeds the configured evidence bound" in result.warning


async def test_merge_requirements_missing_required_check_is_explicit_not_silently_dropped() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [],
            {"protected": True},
            _classic_protection(),
            _repo_methods(),
            _pr(head_sha=head),
            [{"name": "lint", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}],
            _pr(head_sha=head),
            {"behind_by": 0},
            _pr(head_sha=head),
            [],
            _requested(),
            _graphql(decision="APPROVED"),
            _pr(head_sha=head),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client),
    )

    assert result.head_matches_expected is True
    assert result.exact_head_evidence is True
    assert result.policy_evidence_complete is True
    assert {(check.context, check.integration_id) for check in result.required_status_checks} == {
        ("ci", None),
    }
    by_name = {check.name: check for check in result.current_required_checks}
    assert [check.name for check in result.current_required_checks] == ["lint", "ci"]
    assert by_name["lint"].state == "SUCCESS"
    assert by_name["lint"].bucket == "pass"
    assert by_name["ci"].state == "UNKNOWN"
    assert by_name["ci"].bucket == "pending"
    assert result.checks_evidence_complete is True
    assert result.warning is None


async def test_merge_requirements_pinned_check_not_satisfied_by_context_row() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [],
            {"protected": True},
            _pinned_classic(),
            _repo_methods(),
            _pr(head_sha=head),
            [
                {"name": "lint", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"},
                {"name": "ci", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"},
            ],
            _pr(head_sha=head),
            {"behind_by": 0},
            _pr(head_sha=head),
            [],
            _requested(),
            _graphql(decision=None),
            _pr(head_sha=head),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client),
    )

    assert result.head_matches_expected is True
    assert result.exact_head_evidence is True
    assert result.policy_evidence_complete is True
    assert {(check.context, check.integration_id) for check in result.required_status_checks} == {
        ("ci", None),
        ("lint", 42),
    }
    by_name: dict[str, list[PullRequestCheck]] = {}
    for check in result.current_required_checks:
        by_name.setdefault(check.name, []).append(check)
    assert len(by_name["ci"]) == 1
    assert by_name["ci"][0].state == "SUCCESS"
    assert by_name["ci"][0].bucket == "pass"
    assert len(by_name["lint"]) == 2
    placeholder = next(check for check in by_name["lint"] if check.state == "UNKNOWN")
    assert placeholder.bucket == "pending"
    assert placeholder.description is not None
    assert "integration" in placeholder.description
    assert result.checks_evidence_complete is True
    assert result.warning is None


async def test_merge_requirements_classic_linear_history_removes_merge_method() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [],
            {"protected": True},
            _classic_protection(linear_history=True),
            _repo_methods(),
            _pr(head_sha=head),
            _checks(),
            _pr(head_sha=head),
            {"behind_by": 0},
            _pr(head_sha=head),
            [],
            _requested(),
            _graphql(decision="APPROVED"),
            _pr(head_sha=head),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_merge_requirements(
        "octo",
        "repo",
        21,
        head,
        ctx=_context(client),
    )

    assert result.allowed_merge_methods == ["squash", "rebase"]
    assert result.allowed_merge_methods_complete is True
    classic_sources = [
        source for source in result.evidence_sources if source.source == "classic_branch_protection"
    ]
    assert len(classic_sources) == 1
    assert classic_sources[0].status == "present"
