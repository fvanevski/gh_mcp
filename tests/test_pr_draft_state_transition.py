"""Regression coverage for exact-head pull-request draft-state transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.pr_draft_state_models import PullRequestDraftStateTransitionResult
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext, gh_set_pr_draft_state
from mcp_gh_server.settings import Settings
from mcp_gh_server.write_contracts import WritePreconditionMismatch


@dataclass
class DraftStateClient:
    """Protocol fake with separate authoritative-read and governed-write queues."""

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


def _context(client: DraftStateClient, *, writes_enabled: bool = True) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=writes_enabled),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _snapshot(
    *,
    head_sha: str = "a" * 40,
    is_draft: bool,
) -> dict[str, Any]:
    return {
        "number": 20,
        "head": {"sha": head_sha},
        "draft": is_draft,
        "html_url": "https://github.com/octo/repo/pull/20",
    }


def test_result_model_exposes_exact_write_outcome_contract() -> None:
    schema = PullRequestDraftStateTransitionResult.model_json_schema()

    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "number",
        "previous_head_sha",
        "current_head_sha",
        "previous_is_draft",
        "current_is_draft",
        "url",
    } == set(schema["properties"])


async def test_draft_to_ready_checks_exact_state_writes_once_and_verifies_readback() -> None:
    head = "a" * 40
    client = DraftStateClient(
        read_results=[
            _snapshot(head_sha=head, is_draft=True),
            _snapshot(head_sha=head, is_draft=False),
        ],
        write_results=[
            GitHubRequestResult(
                value={"stdout": "Pull request marked as ready for review"},
                metadata=GitHubRequestMetadata(request_id="req-ready"),
            )
        ],
    )

    result = await gh_set_pr_draft_state(
        "octo",
        "repo",
        20,
        head.upper(),
        True,
        False,
        ctx=_context(client),
    )

    assert [kind for kind, _, _ in client.calls] == ["read", "write", "read"]
    assert client.calls[1][1] == ("pr", "ready", "20", "--repo", "octo/repo")
    assert client.calls[1][2] == {"json_output": False}
    assert result.previous_head_sha == head
    assert result.current_head_sha == head
    assert result.previous_is_draft is True
    assert result.current_is_draft is False
    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-ready"
    assert result.warning is None


async def test_ready_to_draft_uses_undo_and_verifies_readback() -> None:
    head = "b" * 40
    client = DraftStateClient(
        read_results=[
            _snapshot(head_sha=head, is_draft=False),
            _snapshot(head_sha=head, is_draft=True),
        ],
        write_results=[GitHubRequestResult(value={"stdout": "Converted to draft"})],
    )

    result = await gh_set_pr_draft_state(
        "octo",
        "repo",
        20,
        head,
        False,
        True,
        ctx=_context(client),
    )

    assert client.calls[1][1] == ("pr", "ready", "20", "--repo", "octo/repo", "--undo")
    assert result.previous_is_draft is False
    assert result.current_is_draft is True
    assert result.state_matches_requested is True


async def test_stale_expected_head_fails_before_mutation_or_readback() -> None:
    client = DraftStateClient(read_results=[_snapshot(head_sha="b" * 40, is_draft=True)])

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_set_pr_draft_state(
            "octo",
            "repo",
            20,
            "a" * 40,
            True,
            False,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read"]


async def test_stale_expected_draft_state_fails_before_mutation_or_readback() -> None:
    head = "a" * 40
    client = DraftStateClient(read_results=[_snapshot(head_sha=head, is_draft=False)])

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_set_pr_draft_state(
            "octo",
            "repo",
            20,
            head,
            True,
            False,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read"]


@pytest.mark.parametrize("state", [True, False])
async def test_noop_transition_is_rejected_without_github_calls(state: bool) -> None:
    client = DraftStateClient()

    with pytest.raises(ValueError, match="actual draft-state transition"):
        await gh_set_pr_draft_state(
            "octo",
            "repo",
            20,
            "a" * 40,
            state,
            state,
            ctx=_context(client),
        )

    assert client.calls == []


async def test_known_mutation_failure_is_not_replayed_and_returns_failed_outcome() -> None:
    head = "a" * 40
    client = DraftStateClient(
        read_results=[
            _snapshot(head_sha=head, is_draft=True),
            _snapshot(head_sha=head, is_draft=True),
        ],
        write_results=[
            GitHubRequestError(
                "validation failed",
                status_code=422,
                ambiguous=False,
                metadata=GitHubRequestMetadata(request_id="req-failed-transition"),
            )
        ],
    )

    result = await gh_set_pr_draft_state(
        "octo",
        "repo",
        20,
        head,
        True,
        False,
        ctx=_context(client),
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.previous_is_draft is True
    assert result.current_is_draft is True
    assert result.write_completed is False
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.request_id == "req-failed-transition"
    assert result.warning is not None
    assert "was not retried" in result.warning


async def test_readback_failure_preserves_completed_write_as_unverified() -> None:
    head = "a" * 40
    client = DraftStateClient(
        read_results=[
            _snapshot(head_sha=head, is_draft=True),
            RuntimeError("readback unavailable"),
        ],
        write_results=[
            GitHubRequestResult(
                value={"stdout": "ready"},
                metadata=GitHubRequestMetadata(request_id="req-readback-failed"),
            )
        ],
    )

    result = await gh_set_pr_draft_state(
        "octo",
        "repo",
        20,
        head,
        True,
        False,
        ctx=_context(client),
    )

    assert result.current_head_sha is None
    assert result.current_is_draft is None
    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-readback-failed"
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


async def test_head_movement_after_write_prevents_verified_success() -> None:
    original_head = "a" * 40
    moved_head = "b" * 40
    client = DraftStateClient(
        read_results=[
            _snapshot(head_sha=original_head, is_draft=True),
            _snapshot(head_sha=moved_head, is_draft=False),
        ],
        write_results=[GitHubRequestResult(value={"stdout": "ready"})],
    )

    result = await gh_set_pr_draft_state(
        "octo",
        "repo",
        20,
        original_head,
        True,
        False,
        ctx=_context(client),
    )

    assert result.previous_head_sha == original_head
    assert result.current_head_sha == moved_head
    assert result.current_is_draft is False
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.warning is not None
    assert "does not match the requested state" in result.warning


async def test_wrong_draft_state_readback_is_not_verified_success() -> None:
    head = "a" * 40
    client = DraftStateClient(
        read_results=[
            _snapshot(head_sha=head, is_draft=True),
            _snapshot(head_sha=head, is_draft=True),
        ],
        write_results=[GitHubRequestResult(value={"stdout": "ready"})],
    )

    result = await gh_set_pr_draft_state(
        "octo",
        "repo",
        20,
        head,
        True,
        False,
        ctx=_context(client),
    )

    assert result.current_is_draft is True
    assert result.state_matches_requested is False
    assert result.warning is not None


async def test_write_gate_blocks_transition_before_any_github_call() -> None:
    client = DraftStateClient()

    with pytest.raises(RuntimeError, match="MCP_GH_ALLOW_WRITE_COMMANDS"):
        await gh_set_pr_draft_state(
            "octo",
            "repo",
            20,
            "a" * 40,
            True,
            False,
            ctx=_context(client, writes_enabled=False),
        )

    assert client.calls == []
