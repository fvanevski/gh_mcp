"""Regression coverage for exact-head pull-request review-thread detail reads."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.pr_reviews import gh_get_pr_review_thread


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
    app = AppContext(client=client, settings=settings or Settings())  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _pr(base_sha: str = "a" * 40, head_sha: str = "b" * 40) -> dict[str, Any]:
    return {"base": {"sha": base_sha}, "head": {"sha": head_sha}}


def _comment(
    comment_id: str,
    body: Any,
    *,
    database_id: str | int | None = "101",
    author: str | None = "reviewer",
    reply_to_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "fullDatabaseId": database_id,
        "author": None if author is None else {"login": author},
        "authorAssociation": "COLLABORATOR",
        "createdAt": "2026-08-31T20:00:00Z",
        "updatedAt": "2026-08-31T20:01:00Z",
        "url": f"https://github.com/octo/repo/pull/19#discussion_r{comment_id}",
        "body": body,
        "replyTo": None if reply_to_id is None else {"id": reply_to_id},
    }


def _thread_graphql(
    *,
    thread_id: str = "PRRT_thread",
    owner: str = "octo",
    repo: str = "repo",
    number: int = 19,
    resolved: bool = False,
    outdated: bool = False,
    comments: list[dict[str, Any]] | None = None,
    total_count: int | None = None,
    has_next_page: bool | None = None,
) -> dict[str, Any]:
    nodes = comments or []
    total = len(nodes) if total_count is None else total_count
    has_more = total > len(nodes) if has_next_page is None else has_next_page
    return {
        "data": {
            "node": {
                "__typename": "PullRequestReviewThread",
                "id": thread_id,
                "isResolved": resolved,
                "isOutdated": outdated,
                "path": "src/example.py",
                "line": 12,
                "originalLine": 10,
                "repository": {"nameWithOwner": f"{owner}/{repo}"},
                "pullRequest": {"number": number},
                "comments": {
                    "totalCount": total,
                    "pageInfo": {"hasNextPage": has_more},
                    "nodes": nodes,
                },
            }
        }
    }


async def test_review_thread_returns_ordered_comments_and_thread_state() -> None:
    head = "b" * 40
    comments = [
        _comment("PRRC_first", "first body", database_id="101"),
        _comment("PRRC_second", "second body", database_id=102, reply_to_id="PRRC_first"),
    ]
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            _thread_graphql(
                resolved=True,
                outdated=True,
                comments=comments,
            ),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        head,
        "PRRT_thread",
        ctx=_context(client),
        max_comments=10,
        max_body_bytes=100,
    )

    assert result.head_matches_expected is True
    assert result.exact_head_evidence is True
    assert result.thread is not None
    assert result.thread.id == "PRRT_thread"
    assert result.thread.path == "src/example.py"
    assert result.thread.line == 12
    assert result.thread.original_line == 10
    assert result.thread.is_resolved is True
    assert result.thread.is_outdated is True
    assert result.thread.comment_count == 2
    assert [comment.id for comment in result.comments] == ["PRRC_first", "PRRC_second"]
    assert result.comments[0].database_id == 101
    assert result.comments[0].author == "reviewer"
    assert result.comments[0].author_association == "COLLABORATOR"
    assert result.comments[0].created_at == "2026-08-31T20:00:00Z"
    assert result.comments[0].updated_at == "2026-08-31T20:01:00Z"
    assert result.comments[0].url is not None
    assert result.comments[1].reply_to_id == "PRRC_first"
    assert result.comments_evidence is not None
    assert result.comments_evidence.total_count == 2
    assert result.comments_evidence.returned_count == 2
    assert result.comments_evidence.has_more is False
    assert result.comments_evidence.truncated is False
    graphql_args = client.calls[1][0]
    assert graphql_args[:2] == ("api", "graphql")
    assert "threadId=PRRT_thread" in graphql_args
    assert "first=10" in graphql_args
    query_arg = next(arg for arg in graphql_args if arg.startswith("query="))
    assert "node(id: $threadId)" in query_arg
    assert "mutation" not in query_arg.casefold()
    assert "--input" not in graphql_args


async def test_review_thread_initial_head_mismatch_issues_no_graphql_read() -> None:
    expected = "b" * 40
    current = "c" * 40
    client = FakeGhClient([_pr(head_sha=current)])

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        expected,
        "PRRT_thread",
        ctx=_context(client),
    )

    assert result.current_head_sha == current
    assert result.head_matches_expected is False
    assert result.exact_head_evidence is False
    assert result.thread is None
    assert result.comments == []
    assert result.comments_evidence is None
    assert len(client.calls) == 1


async def test_review_thread_discards_evidence_when_pr_identity_moves() -> None:
    expected = "b" * 40
    changed = "d" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=expected),
            _thread_graphql(comments=[_comment("PRRC_one", "body")]),
            _pr(head_sha=changed),
        ]
    )

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        expected,
        "PRRT_thread",
        ctx=_context(client),
    )

    assert result.current_head_sha == changed
    assert result.head_matches_expected is False
    assert result.exact_head_evidence is False
    assert result.thread is None
    assert result.comments == []
    assert result.comments_evidence is None
    assert result.warning is not None
    assert "discarded" in result.warning


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_thread_graphql(owner="other"), "requested repository"),
        (_thread_graphql(number=20), "requested pull request"),
        (_thread_graphql(thread_id="PRRT_other"), "different review thread"),
        ({"data": {"node": None}}, "not found or is inaccessible"),
        ({"data": {"node": {"__typename": "Issue"}}}, "not a pull-request review thread"),
    ],
)
async def test_review_thread_rejects_wrong_node_or_ownership(
    payload: dict[str, Any], message: str
) -> None:
    head = "b" * 40
    client = FakeGhClient([_pr(head_sha=head), payload])

    with pytest.raises(RuntimeError, match=message):
        await gh_get_pr_review_thread(
            "octo",
            "repo",
            19,
            head,
            "PRRT_thread",
            ctx=_context(client),
        )


async def test_review_thread_body_truncation_hashes_complete_unicode_body() -> None:
    head = "b" * 40
    body = "ééé"
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            _thread_graphql(comments=[_comment("PRRC_one", body)]),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        head,
        "PRRT_thread",
        ctx=_context(client),
        max_body_bytes=5,
    )

    comment = result.comments[0]
    assert comment.body == "éé"
    assert comment.body_bytes_returned == 4
    assert comment.body_total_bytes == 6
    assert comment.body_truncated is True
    assert comment.body_sha256 == hashlib.sha256(body.encode()).hexdigest()
    assert comment.body_warning is not None
    assert result.exact_head_evidence is True


async def test_review_thread_server_body_cap_is_independent_of_comment_completeness() -> None:
    head = "b" * 40
    settings = Settings(max_review_comment_body_bytes=4)
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            _thread_graphql(comments=[_comment("PRRC_one", "abcdef")]),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        head,
        "PRRT_thread",
        ctx=_context(client, settings),
        max_comments=10,
        max_body_bytes=10,
    )

    assert result.comments_evidence is not None
    assert result.comments_evidence.truncated is False
    assert result.comments[0].body == "abcd"
    assert result.comments[0].body_truncated is True
    assert result.comments[0].body_warning is not None
    assert "capped" in result.comments[0].body_warning


async def test_review_thread_comment_bound_reports_independent_pagination_truncation() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            _thread_graphql(
                comments=[_comment("PRRC_one", "complete")],
                total_count=2,
                has_next_page=True,
            ),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        head,
        "PRRT_thread",
        ctx=_context(client),
        max_comments=1,
        max_body_bytes=100,
    )

    assert result.exact_head_evidence is True
    assert result.comments_evidence is not None
    assert result.comments_evidence.total_count == 2
    assert result.comments_evidence.returned_count == 1
    assert result.comments_evidence.has_more is True
    assert result.comments_evidence.truncated is True
    assert result.comments_evidence.warning is not None
    assert result.comments[0].body_truncated is False


async def test_review_thread_server_comment_cap_is_explicit() -> None:
    head = "b" * 40
    settings = Settings(default_max_results=1, hard_max_results=1)
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            _thread_graphql(
                comments=[_comment("PRRC_one", "complete")],
                total_count=2,
                has_next_page=True,
            ),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        head,
        "PRRT_thread",
        ctx=_context(client, settings),
        max_comments=3,
    )

    assert result.comments_evidence is not None
    assert result.comments_evidence.per_page == 1
    assert result.comments_evidence.truncated is True
    assert result.comments_evidence.warning is not None
    assert "max_comments=3" in result.comments_evidence.warning
    assert "server hard limit of 1" in result.comments_evidence.warning


@pytest.mark.parametrize(
    "payload",
    [
        _thread_graphql(
            comments=[_comment("PRRC_one", "body")],
            total_count=1,
            has_next_page=True,
        ),
        _thread_graphql(comments=[], total_count=1, has_next_page=True),
    ],
)
async def test_review_thread_rejects_inconsistent_comment_pagination(
    payload: dict[str, Any],
) -> None:
    head = "b" * 40
    client = FakeGhClient([_pr(head_sha=head), payload])

    with pytest.raises(RuntimeError, match="review-comment"):
        await gh_get_pr_review_thread(
            "octo",
            "repo",
            19,
            head,
            "PRRT_thread",
            ctx=_context(client),
        )


@pytest.mark.parametrize(
    ("comment", "message"),
    [
        (_comment("", "body"), "valid node id"),
        (_comment("PRRC_one", None), "malformed body"),
        (_comment("PRRC_one", "body", database_id=True), "database identity"),
        (_comment("PRRC_one", "body", reply_to_id=""), "reply node id"),
    ],
)
async def test_review_thread_rejects_malformed_comment_evidence(
    comment: dict[str, Any], message: str
) -> None:
    head = "b" * 40
    client = FakeGhClient(
        [_pr(head_sha=head), _thread_graphql(comments=[comment])]
    )

    with pytest.raises(RuntimeError, match=message):
        await gh_get_pr_review_thread(
            "octo",
            "repo",
            19,
            head,
            "PRRT_thread",
            ctx=_context(client),
        )


async def test_review_thread_allows_deleted_author_and_missing_database_id() -> None:
    head = "b" * 40
    client = FakeGhClient(
        [
            _pr(head_sha=head),
            _thread_graphql(
                comments=[_comment("PRRC_one", "body", author=None, database_id=None)]
            ),
            _pr(head_sha=head),
        ]
    )

    result = await gh_get_pr_review_thread(
        "octo",
        "repo",
        19,
        head,
        "PRRT_thread",
        ctx=_context(client),
    )

    assert result.comments[0].author is None
    assert result.comments[0].database_id is None


async def test_review_thread_graphql_errors_fail_closed() -> None:
    head = "b" * 40
    client = FakeGhClient([_pr(head_sha=head), {"errors": [{"message": "denied"}]}])

    with pytest.raises(RuntimeError, match="GraphQL returned errors"):
        await gh_get_pr_review_thread(
            "octo",
            "repo",
            19,
            head,
            "PRRT_thread",
            ctx=_context(client),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"number": 0},
        {"expected_head_sha": "bad"},
        {"thread_id": ""},
        {"max_comments": 0},
        {"max_comments": 101},
        {"max_body_bytes": 0},
        {"max_body_bytes": 1_000_001},
    ],
)
async def test_review_thread_invalid_inputs_fail_before_external_requests(
    kwargs: dict[str, Any],
) -> None:
    client = FakeGhClient([])
    arguments: dict[str, Any] = {
        "owner": "octo",
        "repo": "repo",
        "number": 19,
        "expected_head_sha": "b" * 40,
        "thread_id": "PRRT_thread",
        "ctx": _context(client),
    }
    arguments.update(kwargs)

    with pytest.raises(ValueError):
        await gh_get_pr_review_thread(**arguments)

    assert client.calls == []
