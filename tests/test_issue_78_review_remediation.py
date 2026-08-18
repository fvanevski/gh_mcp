"""Regression coverage for the PR #79 independent-review remediation."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from mcp_gh_server.merge_requirements_models import RequiredStatusCheck
from mcp_gh_server.merge_requirements_policy import read_effective_merge_policy
from mcp_gh_server.required_check_evidence import read_pinned_required_check_evidence
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.merge_requirements import (
    _reconcile_required_checks,
    _review_evidence_status,
)


@dataclass
class FakeGhClient:
    """Return queued GitHub payloads while recording exact calls."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _app(client: FakeGhClient) -> AppContext:
    return AppContext(client=client, settings=Settings())  # type: ignore[arg-type]


def _check_run(
    name: str,
    integration_id: int | None,
    state: str,
    *,
    workflow: str | None,
    event: str | None,
    started_at: str,
    include_event: bool = True,
) -> dict[str, Any]:
    workflow_run = (
        {
            **({"event": event} if include_event else {}),
            "workflow": {"name": workflow} if workflow is not None else None,
        }
        if workflow is not None or event is not None
        else None
    )
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED" if state not in {"PENDING", "IN_PROGRESS"} else state,
        "conclusion": state if state not in {"PENDING", "IN_PROGRESS"} else None,
        "startedAt": started_at,
        "completedAt": (
            "2026-08-18T20:00:30Z" if state not in {"PENDING", "IN_PROGRESS"} else None
        ),
        "detailsUrl": f"https://example.test/{workflow or 'external'}/{name}",
        "isRequired": True,
        "checkSuite": {
            "app": {"databaseId": integration_id} if integration_id is not None else None,
            "workflowRun": workflow_run,
        },
    }


def _status_context(context: str) -> dict[str, Any]:
    return {
        "__typename": "StatusContext",
        "context": context,
        "isRequired": True,
    }


def _required_graphql(
    *nodes: dict[str, Any],
    base_sha: str = "a" * 40,
    head_sha: str = "b" * 40,
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "baseRefOid": base_sha,
                    "headRefOid": head_sha,
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": head_sha,
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "nodes": list(nodes),
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                        }
                                    },
                                }
                            }
                        ]
                    },
                }
            }
        }
    }


def _workflow_run_feature_graphql(*fields: str) -> dict[str, Any]:
    return {
        "data": {
            "__type": {
                "fields": [{"name": field} for field in fields],
            }
        }
    }


async def test_pinned_required_check_keeps_distinct_workflow_event_rows() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _required_graphql(
                _check_run(
                    "lint",
                    42,
                    "SUCCESS",
                    workflow="CI",
                    event="pull_request",
                    started_at="2026-08-18T20:00:00Z",
                ),
                _check_run(
                    "lint",
                    42,
                    "FAILURE",
                    workflow="Release",
                    event="push",
                    started_at="2026-08-18T20:01:00Z",
                ),
                head_sha=head,
            )
        ]
    )

    read = await read_pinned_required_check_evidence(
        _app(client),
        "octo",
        "repo",
        21,
        base_sha="a" * 40,
        head_sha=head,
        required_identities={("lint", 42)},
        limit=100,
    )

    assert read.complete is True
    assert read.truncated is False
    assert read.warnings == []
    reconciled = _reconcile_required_checks(
        [RequiredStatusCheck(context="lint", integration_id=42)],
        [],
        read.checks,
        absence_authoritative=True,
    )
    assert {
        (check.name, check.integration_id, check.workflow, check.event, check.state)
        for check in reconciled
    } == {
        ("lint", 42, "CI", "pull_request", "SUCCESS"),
        ("lint", 42, "Release", "push", "FAILURE"),
    }


async def test_pinned_required_check_deduplicates_only_same_logical_rerun() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _required_graphql(
                _check_run(
                    "lint",
                    42,
                    "FAILURE",
                    workflow="CI",
                    event="pull_request",
                    started_at="2026-08-18T20:00:00Z",
                ),
                _check_run(
                    "lint",
                    42,
                    "SUCCESS",
                    workflow="CI",
                    event="pull_request",
                    started_at="2026-08-18T20:02:00Z",
                ),
                head_sha=head,
            )
        ]
    )

    read = await read_pinned_required_check_evidence(
        _app(client),
        "octo",
        "repo",
        21,
        base_sha="a" * 40,
        head_sha=head,
        required_identities={("lint", 42)},
        limit=100,
    )

    assert read.complete is True
    assert [(check.workflow, check.event, check.state) for check in read.checks] == [
        ("CI", "pull_request", "SUCCESS")
    ]


async def test_pinned_required_check_falls_back_when_workflow_event_is_unsupported() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            {"errors": [{"message": "Cannot query field 'event' on type 'WorkflowRun'."}]},
            _workflow_run_feature_graphql("workflow"),
            _required_graphql(
                _check_run(
                    "lint",
                    42,
                    "SUCCESS",
                    workflow="CI",
                    event=None,
                    started_at="2026-08-18T20:00:00Z",
                    include_event=False,
                ),
                head_sha=head,
            ),
        ]
    )

    read = await read_pinned_required_check_evidence(
        _app(client),
        "octo",
        "repo",
        21,
        base_sha="a" * 40,
        head_sha=head,
        required_identities={("lint", 42)},
        limit=100,
    )

    assert read.complete is True
    assert read.warnings == []
    assert [
        (check.name, check.integration_id, check.workflow, check.event, check.state)
        for check in read.checks
    ] == [("lint", 42, "CI", None, "SUCCESS")]
    assert len(client.calls) == 3
    first_query = next(arg for arg in client.calls[0][0] if arg.startswith("query="))
    feature_query = next(arg for arg in client.calls[1][0] if arg.startswith("query="))
    fallback_query = next(arg for arg in client.calls[2][0] if arg.startswith("query="))
    assert "event" in first_query
    assert "__type" in feature_query
    assert "event" not in fallback_query


async def test_pinned_required_check_does_not_mask_noncompatibility_graphql_error() -> None:
    client = FakeGhClient(
        [
            {"errors": [{"message": "unrelated GraphQL failure"}]},
            _workflow_run_feature_graphql("event", "workflow"),
        ]
    )

    try:
        await read_pinned_required_check_evidence(
            _app(client),
            "octo",
            "repo",
            21,
            base_sha="a" * 40,
            head_sha="b" * 40,
            required_identities={("lint", 42)},
            limit=100,
        )
    except RuntimeError as exc:
        assert "GraphQL returned errors" in str(exc)
    else:
        raise AssertionError("non-compatibility GraphQL errors must remain fail-closed")


async def test_mixed_context_only_and_pinned_observations_are_compatible() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _required_graphql(
                _status_context("lint"),
                _check_run(
                    "lint",
                    42,
                    "SUCCESS",
                    workflow="CI",
                    event="pull_request",
                    started_at="2026-08-18T20:00:00Z",
                ),
                _check_run(
                    "lint",
                    99,
                    "SUCCESS",
                    workflow="External",
                    event="push",
                    started_at="2026-08-18T20:01:00Z",
                ),
                head_sha=head,
            )
        ]
    )

    read = await read_pinned_required_check_evidence(
        _app(client),
        "octo",
        "repo",
        21,
        base_sha="a" * 40,
        head_sha=head,
        required_identities={("lint", None), ("lint", 42)},
        limit=100,
    )

    assert read.complete is True
    assert read.warnings == []
    assert [
        (check.name, check.integration_id, check.workflow, check.state) for check in read.checks
    ] == [("lint", 42, "CI", "SUCCESS")]


async def test_pinned_only_context_rejects_required_row_without_compatible_app() -> None:
    head = "b" * 40
    client = FakeGhClient([_required_graphql(_status_context("lint"), head_sha=head)])

    read = await read_pinned_required_check_evidence(
        _app(client),
        "octo",
        "repo",
        21,
        base_sha="a" * 40,
        head_sha=head,
        required_identities={("lint", 42)},
        limit=100,
    )

    assert read.complete is False
    assert read.checks == []
    assert len(read.warnings) == 1
    assert "no GitHub App identity" in read.warnings[0]


async def test_classic_context_only_requirement_survives_same_context_ruleset_pin() -> None:
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [
                    {"context": "lint", "integration_id": 42},
                ],
            },
        }
    ]
    classic = {
        "required_status_checks": {
            "strict": False,
            "contexts": ["lint"],
            "checks": [],
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
    client = FakeGhClient([rules, {"protected": True}, classic])

    policy, complete, warnings = await read_effective_merge_policy(
        _app(client),
        "octo",
        "repo",
        "main",
    )

    assert complete is True
    assert policy is not None
    assert set(policy.required_status_checks) == {
        ("lint", None),
        ("lint", 42),
    }
    assert warnings == []


async def test_classic_context_projection_does_not_duplicate_same_source_pinned_check() -> None:
    classic = {
        "required_status_checks": {
            "strict": False,
            "contexts": ["lint"],
            "checks": [{"context": "lint", "app_id": 42}],
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
    client = FakeGhClient([[], {"protected": True}, classic])

    policy, complete, warnings = await read_effective_merge_policy(
        _app(client),
        "octo",
        "repo",
        "main",
    )

    assert complete is True
    assert policy is not None
    assert set(policy.required_status_checks) == {("lint", 42)}
    assert warnings == []


def test_review_evidence_status_reports_stable_head_truncation() -> None:
    state = SimpleNamespace(
        head_matches_expected=True,
        exact_head_evidence=False,
        reviews_evidence=SimpleNamespace(truncated=True),
        review_threads_evidence=SimpleNamespace(truncated=False),
    )

    status, reason = _review_evidence_status(state)  # type: ignore[arg-type]

    assert status == "truncated"
    assert "result bound" in reason
    assert "head" not in reason.casefold()


def test_review_evidence_status_reserves_head_mismatch_for_identity_movement() -> None:
    state = SimpleNamespace(
        head_matches_expected=False,
        exact_head_evidence=False,
        reviews_evidence=None,
        review_threads_evidence=None,
    )

    status, reason = _review_evidence_status(state)  # type: ignore[arg-type]

    assert status == "head_mismatch"
    assert "expected pull-request head" in reason


def test_review_evidence_status_reports_complete_exact_head() -> None:
    state = SimpleNamespace(
        head_matches_expected=True,
        exact_head_evidence=True,
        reviews_evidence=None,
        review_threads_evidence=None,
    )

    status, reason = _review_evidence_status(state)  # type: ignore[arg-type]

    assert status == "complete"
    assert reason == "Exact-head review/thread evidence is complete."
