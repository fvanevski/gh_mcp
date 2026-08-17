"""Focused regressions for issue #75 action-specific PR review identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_gh_server.tools.pr_review_eligibility as eligibility_module
import mcp_gh_server.tools.pr_review_writes as review_writes_module
from mcp_gh_server.pr_review_eligibility_models import PullRequestReviewEligibility
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.reviewer_auth import ReviewerIdentity
from mcp_gh_server.server import (
    AppContext,
    gh_approve_pr,
    gh_comment_pr_review,
    gh_get_pr_review_eligibility,
    gh_request_pr_changes,
    mcp,
)
from mcp_gh_server.settings import Settings


@dataclass
class FakeClient:
    read_results: list[Any] = field(default_factory=list)
    write_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        result = self.write_results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


@dataclass
class FakeReviewerPrincipal:
    identity: ReviewerIdentity
    client: FakeClient

    async def resolve_identity_read_only(self, owner: str, repo: str) -> ReviewerIdentity:
        del owner, repo
        return self.identity

    async def client_for_review(
        self,
        owner: str,
        repo: str,
        *,
        expected_login: str,
    ) -> FakeClient:
        del owner, repo
        if self.identity.login.casefold() != expected_login.casefold():
            raise ValueError("authenticated reviewer login mismatch; no review was attempted")
        return self.client


def _settings(*, reviewer_token: bool = False) -> Settings:
    kwargs: dict[str, Any] = {"allow_write_commands": True}
    if reviewer_token:
        kwargs["reviewer_token"] = "reviewer-token"
    return Settings(**kwargs)


def _context(client: FakeClient, *, settings: Settings | None = None) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=settings or _settings(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _pr(head: str, author: str = "author") -> dict[str, Any]:
    return {"head": {"sha": head}, "user": {"login": author}}


def _review(
    review_id: int,
    *,
    state: str,
    head: str,
    actor: str,
    body: str,
) -> dict[str, Any]:
    return {
        "id": review_id,
        "state": state,
        "commit_id": head,
        "body": body,
        "user": {"login": actor},
        "submitted_at": "2026-08-17T20:00:00Z",
        "html_url": f"https://github.com/octo/repo/pull/9#pullrequestreview-{review_id}",
    }


def _review_posts(client: FakeClient) -> list[tuple[str, tuple[str, ...], dict[str, Any]]]:
    return [
        call
        for call in client.calls
        if call[0] == "write"
        and call[1]
        and call[1][0] == "api"
        and any("/reviews" in arg for arg in call[1])
    ]


def _install_fake_principal(
    monkeypatch: pytest.MonkeyPatch,
    principal: FakeReviewerPrincipal | None,
) -> None:
    replacement = SimpleNamespace(from_settings=lambda settings: principal)
    monkeypatch.setattr(review_writes_module, "ReviewerPrincipal", replacement)
    monkeypatch.setattr(eligibility_module, "ReviewerPrincipal", replacement)


@pytest.mark.asyncio
async def test_eligibility_author_is_ordinary_identity_without_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    client = FakeClient(read_results=[_pr(head), {"login": "author"}, _pr(head)])
    _install_fake_principal(monkeypatch, None)

    result = await gh_get_pr_review_eligibility(
        "octo", "repo", 9, head, ctx=_context(client)
    )

    assert isinstance(result, PullRequestReviewEligibility)
    assert result.head_matches_expected is True
    assert result.pr_author_login == "author"
    assert result.ordinary_login == "author"
    assert result.reviewer_login is None
    assert result.approval_eligible is False
    assert result.comment_review_available is True
    assert result.reason == "reviewer_not_configured"


@pytest.mark.asyncio
async def test_eligibility_distinct_reviewer_is_approval_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    client = FakeClient(read_results=[_pr(head), {"login": "author"}, _pr(head)])
    reviewer = FakeClient()
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="reviewer-bot[bot]", kind="github_app"),
        reviewer,
    )
    _install_fake_principal(monkeypatch, principal)

    result = await gh_get_pr_review_eligibility(
        "octo", "repo", 9, head, ctx=_context(client, settings=_settings(reviewer_token=True))
    )

    assert result.approval_eligible is True
    assert result.comment_review_available is True
    assert result.reviewer_login == "reviewer-bot[bot]"
    assert result.reason == "eligible"


@pytest.mark.asyncio
async def test_eligibility_reviewer_equals_author_is_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    client = FakeClient(read_results=[_pr(head), {"login": "author"}, _pr(head)])
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="AUTHOR", kind="static_token"),
        FakeClient(),
    )
    _install_fake_principal(monkeypatch, principal)

    result = await gh_get_pr_review_eligibility(
        "octo", "repo", 9, head, ctx=_context(client, settings=_settings(reviewer_token=True))
    )

    assert result.approval_eligible is False
    assert result.reason == "reviewer_is_pr_author"


@pytest.mark.asyncio
async def test_eligibility_head_moved_fails_closed_before_identity_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(read_results=[_pr("b" * 40)])
    _install_fake_principal(monkeypatch, None)

    result = await gh_get_pr_review_eligibility(
        "octo", "repo", 9, "a" * 40, ctx=_context(client)
    )

    assert result.head_matches_expected is False
    assert result.approval_eligible is False
    assert result.comment_review_available is False
    assert result.reason == "head_mismatch"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_approve_distinct_reviewer_posts_once_and_verifies_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    body = "Approved after exact-head review."
    ordinary = FakeClient(
        read_results=[
            _pr(head),
            _pr(head),
            _review(101, state="APPROVED", head=head, actor="reviewer", body=body),
        ]
    )
    reviewer = FakeClient(
        write_results=[
            GitHubRequestResult(
                value={"id": 101, "state": "APPROVED"},
                metadata=GitHubRequestMetadata(request_id="review-101"),
            )
        ]
    )
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="reviewer", kind="static_token"),
        reviewer,
    )
    _install_fake_principal(monkeypatch, principal)

    result = await gh_approve_pr(
        "octo",
        "repo",
        9,
        head,
        "reviewer",
        ctx=_context(ordinary, settings=_settings(reviewer_token=True)),
        body=body,
    )

    assert result.state == "APPROVED"
    assert result.author == "reviewer"
    assert result.commit_sha == head
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "review-101"
    assert len(_review_posts(reviewer)) == 1
    assert _review_posts(ordinary) == []


@pytest.mark.asyncio
async def test_approve_wrong_expected_reviewer_login_has_zero_review_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    ordinary = FakeClient(read_results=[_pr(head)])
    reviewer = FakeClient()
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="reviewer", kind="static_token"),
        reviewer,
    )
    _install_fake_principal(monkeypatch, principal)

    with pytest.raises(ValueError, match="expected_reviewer_login"):
        await gh_approve_pr(
            "octo",
            "repo",
            9,
            head,
            "other-reviewer",
            ctx=_context(ordinary, settings=_settings(reviewer_token=True)),
        )

    assert _review_posts(reviewer) == []
    assert _review_posts(ordinary) == []


@pytest.mark.asyncio
async def test_approve_reviewer_equals_author_has_zero_review_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    ordinary = FakeClient(read_results=[_pr(head, "reviewer")])
    reviewer = FakeClient()
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="REVIEWER", kind="static_token"),
        reviewer,
    )
    _install_fake_principal(monkeypatch, principal)

    with pytest.raises(ValueError, match="cannot approve its own pull request"):
        await gh_approve_pr(
            "octo",
            "repo",
            9,
            head,
            "reviewer",
            ctx=_context(ordinary, settings=_settings(reviewer_token=True)),
        )

    assert _review_posts(reviewer) == []
    assert _review_posts(ordinary) == []


@pytest.mark.asyncio
async def test_approve_head_moved_has_zero_review_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary = FakeClient(read_results=[_pr("b" * 40)])
    reviewer = FakeClient()
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="reviewer", kind="static_token"),
        reviewer,
    )
    _install_fake_principal(monkeypatch, principal)

    with pytest.raises(RuntimeError, match="precondition mismatch"):
        await gh_approve_pr(
            "octo",
            "repo",
            9,
            "a" * 40,
            "reviewer",
            ctx=_context(ordinary, settings=_settings(reviewer_token=True)),
        )

    assert _review_posts(reviewer) == []
    assert _review_posts(ordinary) == []


@pytest.mark.asyncio
async def test_comment_review_allows_author_and_verifies_commented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    body = "Central disposition: APPROVE. GitHub state is COMMENTED."
    ordinary = FakeClient(
        read_results=[
            _pr(head),
            {"login": "author"},
            _pr(head),
            _review(102, state="COMMENTED", head=head, actor="author", body=body),
        ],
        write_results=[{"id": 102, "state": "COMMENTED"}],
    )
    _install_fake_principal(monkeypatch, None)

    result = await gh_comment_pr_review(
        "octo", "repo", 9, head, body, ctx=_context(ordinary)
    )

    assert result.action == "comment"
    assert result.state == "COMMENTED"
    assert result.author == "author"
    assert result.state != "APPROVED"
    assert len(_review_posts(ordinary)) == 1


@pytest.mark.asyncio
async def test_request_changes_verifies_state_actor_head_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    body = "Blocking: fix the exact-head defect."
    ordinary = FakeClient(
        read_results=[
            _pr(head),
            _pr(head),
            _review(
                103,
                state="CHANGES_REQUESTED",
                head=head,
                actor="reviewer",
                body=body,
            ),
        ]
    )
    reviewer = FakeClient(write_results=[{"id": 103, "state": "CHANGES_REQUESTED"}])
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="reviewer", kind="static_token"),
        reviewer,
    )
    _install_fake_principal(monkeypatch, principal)

    result = await gh_request_pr_changes(
        "octo",
        "repo",
        9,
        head,
        "reviewer",
        body,
        ctx=_context(ordinary, settings=_settings(reviewer_token=True)),
    )

    assert result.state == "CHANGES_REQUESTED"
    assert result.author == "reviewer"
    assert result.commit_sha == head
    assert result.body == body
    assert result.state_matches_requested is True
    assert len(_review_posts(reviewer)) == 1


@pytest.mark.asyncio
async def test_approve_never_silently_falls_back_to_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    ordinary = FakeClient(read_results=[_pr(head)])
    _install_fake_principal(monkeypatch, None)

    with pytest.raises(RuntimeError, match="reviewer_not_configured"):
        await gh_approve_pr(
            "octo", "repo", 9, head, "reviewer", ctx=_context(ordinary)
        )

    assert _review_posts(ordinary) == []


@pytest.mark.asyncio
async def test_ambiguous_review_write_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    ordinary = FakeClient(
        read_results=[
            _pr(head),
            _pr(head),
        ]
    )
    reviewer = FakeClient(
        write_results=[
            GitHubRequestError(
                "transport reset after review send",
                retryable=True,
                ambiguous=True,
                metadata=GitHubRequestMetadata(request_id="ambiguous-review"),
            )
        ]
    )
    principal = FakeReviewerPrincipal(
        ReviewerIdentity(login="reviewer", kind="static_token"),
        reviewer,
    )
    _install_fake_principal(monkeypatch, principal)

    result = await gh_approve_pr(
        "octo",
        "repo",
        9,
        head,
        "reviewer",
        ctx=_context(ordinary, settings=_settings(reviewer_token=True)),
    )

    assert result.write_completed is None
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning
    assert len(_review_posts(reviewer)) == 1


@pytest.mark.asyncio
async def test_public_review_schemas_expose_no_credential_selector() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    forbidden = {
        "token",
        "credential",
        "credentials",
        "reviewer_token",
        "app_id",
        "installation_id",
        "private_key",
        "private_key_file",
        "auth",
        "authorization",
    }
    for name in ("gh_approve_pr", "gh_request_pr_changes", "gh_comment_pr_review"):
        properties = set(tools[name].input_schema["properties"])
        assert forbidden.isdisjoint(properties)
    assert "expected_reviewer_login" in tools["gh_approve_pr"].input_schema["properties"]
    assert "expected_reviewer_login" in tools["gh_request_pr_changes"].input_schema["properties"]
    assert "expected_reviewer_login" not in tools["gh_comment_pr_review"].input_schema["properties"]


@pytest.mark.asyncio
async def test_action_specific_review_metadata_is_truthful_and_bounded() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    descriptions: set[str] = set()
    for name in ("gh_approve_pr", "gh_request_pr_changes", "gh_comment_pr_review"):
        tool = tools[name]
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is False
        assert tool.annotations.open_world_hint is True
        descriptions.add(tool.description or "")
        properties = tool.input_schema["properties"]
        assert properties["expected_head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
        assert properties["owner"]["maxLength"] == 39
        assert properties["repo"]["maxLength"] == 100
        assert properties["body"]["maxLength"] == 65_536
    assert len(descriptions) == 3


@pytest.mark.asyncio
async def test_multiplexed_review_tool_is_retired_from_public_inventory() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "gh_submit_pr_review" not in tools
    assert {
        "gh_approve_pr",
        "gh_request_pr_changes",
        "gh_comment_pr_review",
    } <= set(tools)


@pytest.mark.asyncio
async def test_comment_review_uses_only_ordinary_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    body = "COMMENTED only."
    ordinary = FakeClient(
        read_results=[
            _pr(head),
            {"login": "author"},
            _pr(head),
            _review(104, state="COMMENTED", head=head, actor="author", body=body),
        ],
        write_results=[{"id": 104, "state": "COMMENTED"}],
    )

    replacement = SimpleNamespace(
        from_settings=lambda settings: (_ for _ in ()).throw(
            AssertionError("comment review must not resolve reviewer credentials")
        )
    )
    monkeypatch.setattr(review_writes_module, "ReviewerPrincipal", replacement)

    result = await gh_comment_pr_review(
        "octo", "repo", 9, head, body, ctx=_context(ordinary)
    )
    assert result.state == "COMMENTED"
    assert len(_review_posts(ordinary)) == 1
