"""Regression coverage for exact-head pull-request review-state reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.pr_reviews import gh_get_pr_review_state, gh_list_pr_reviews


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


def _pr(base_sha: str = "a" * 40, head_sha: str = "b" * 40) -> dict[str, Any]:
    return {"base": {"sha": base_sha}, "head": {"sha": head_sha}}


def _review(
    review_id: int,
    state: str,
    commit_id: str,
    *,
    reviewer: str = "reviewer",
) -> dict[str, Any]:
    return {
        "id": review_id,
        "user": {"login": reviewer},
        "state": state,
        "commit_id": commit_id,
        "submitted_at": "2026-08-11T20:00:00Z",
        "body": f"review {review_id}",
        "author_association": "COLLABORATOR",
    }


def _requested(
    users: list[str] | None = None,
    teams: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "users": [{"login": login} for login in users or []],
        "teams": [{"slug": slug} for slug in teams or []],
    }


def _thread(
    thread_id: str,
    *,
    resolved: bool = False,
    outdated: bool = False,
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "src/example.py",
        "line": 12,
        "originalLine": 10,
        "comments": {"totalCount": 2},
    }


def _graphql(
    *,
    decision: str | None,
    threads: list[dict[str, Any]],
    total_count: int | None = None,
    has_next_page: bool | None = None,
) -> dict[str, Any]:
    total = len(threads) if total_count is None else total_count
    has_more = total > len(threads) if has_next_page is None else has_next_page
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewDecision": decision,
                    "reviewThreads": {
                        "totalCount": total,
                        "pageInfo": {"hasNextPage": has_more},
                        "nodes": threads,
                    },
                }
            }
        }
    }


async def test_list_pr_reviews_probes_full_page_and_surfaces_pagination() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [_review(1, "APPROVED", head)],
            [_review(2, "COMMENTED", head)],
            _pr(head_sha=head),
        ]
    )

    result = await gh_list_pr_reviews(
        "octo",
        "repo",
        19,
        ctx=_context(client),
        per_page=1,
    )

    assert result.head_sha == head
    assert result.returned_count == 1
    assert result.has_more is True
    assert result.truncated is False
    assert result.reviews[0].commit_id == head
    assert "page=2" in client.calls[2][0]


async def test_review_state_classifies_current_and_stale_review_evidence() -> None:
    head = "b" * 40
    stale = "c" * 40
    reviews = [
        _review(1, "APPROVED", head, reviewer="alice"),
        _review(2, "APPROVED", stale, reviewer="bob"),
        _review(3, "CHANGES_REQUESTED", head, reviewer="carol"),
        _review(4, "CHANGES_REQUESTED", stale, reviewer="dave"),
        _review(5, "COMMENTED", head, reviewer="erin"),
    ]
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            reviews,
            _requested(["frank"], ["platform"]),
            _graphql(
                decision="CHANGES_REQUESTED",
                threads=[_thread("T1"), _thread("T2", resolved=True)],
            ),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_state(
        "octo",
        "repo",
        19,
        head,
        ctx=_context(client),
    )

    assert result.head_matches_expected is True
    assert result.exact_head_evidence is True
    assert [review.id for review in result.current_head_approvals] == [1]
    assert [review.id for review in result.stale_approvals] == [2]
    assert [review.id for review in result.current_head_change_requests] == [3]
    assert [review.id for review in result.stale_change_requests] == [4]
    assert [review.id for review in result.current_head_comments] == [5]
    assert result.requested_reviewers == ["frank"]
    assert result.requested_teams == ["platform"]
    assert [thread.id for thread in result.unresolved_review_threads] == ["T1"]
    assert result.review_decision == "CHANGES_REQUESTED"
    assert result.requirements_satisfied is False


async def test_review_state_reports_conservative_satisfied_state_only_for_clean_evidence() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [_review(1, "APPROVED", head, reviewer="alice")],
            _requested(),
            _graphql(decision="APPROVED", threads=[]),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_state(
        "octo",
        "repo",
        19,
        head,
        ctx=_context(client),
    )

    assert result.exact_head_evidence is True
    assert result.requirements_satisfied is True
    assert result.warning is None


async def test_review_state_initial_head_mismatch_returns_no_aggregate_evidence() -> None:
    expected = "b" * 40
    current = "c" * 40
    client = FakeGhClient([_pr(head_sha=current)])

    result = await gh_get_pr_review_state(
        "octo",
        "repo",
        19,
        expected,
        ctx=_context(client),
    )

    assert result.current_head_sha == current
    assert result.head_matches_expected is False
    assert result.exact_head_evidence is False
    assert result.reviews_evidence is None
    assert result.review_threads_evidence is None
    assert result.requirements_satisfied is None
    assert len(client.calls) == 1


async def test_review_state_discards_evidence_when_head_changes_during_read() -> None:
    expected = "b" * 40
    changed = "d" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=expected),
            [_review(1, "APPROVED", expected)],
            _requested(),
            _graphql(decision="APPROVED", threads=[]),
            _pr(head_sha=changed),
        ]
    )

    result = await gh_get_pr_review_state(
        "octo",
        "repo",
        19,
        expected,
        ctx=_context(client),
    )

    assert result.current_head_sha == changed
    assert result.head_matches_expected is False
    assert result.exact_head_evidence is False
    assert result.current_head_approvals == []
    assert result.requirements_satisfied is None
    assert result.warning is not None
    assert "discarded" in result.warning


async def test_review_state_partial_reviews_and_threads_never_report_satisfied() -> None:
    head = "b" * 40
    settings = Settings(default_max_results=1, hard_max_results=1)
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            [_review(1, "APPROVED", head)],
            [_review(2, "APPROVED", head, reviewer="second")],
            _requested(),
            _graphql(
                decision="APPROVED",
                threads=[_thread("T1", resolved=True)],
                total_count=2,
                has_next_page=True,
            ),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_state(
        "octo",
        "repo",
        19,
        head,
        ctx=_context(client, settings),
    )

    assert result.reviews_evidence is not None
    assert result.reviews_evidence.truncated is True
    assert result.review_threads_evidence is not None
    assert result.review_threads_evidence.truncated is True
    assert result.exact_head_evidence is False
    assert result.requirements_satisfied is None
    assert result.warning is not None
