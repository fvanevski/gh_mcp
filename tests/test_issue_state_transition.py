"""Regression coverage for the exact issue lifecycle transition tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.issue_state_models import IssueStateTransitionResult
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext, gh_set_issue_state
from mcp_gh_server.settings import Settings
from mcp_gh_server.write_contracts import WritePreconditionMismatch


@dataclass
class IssueStateClient:
    """Protocol fake with distinct read and governed-write result queues."""

    read_results: list[Any] = field(default_factory=list)
    write_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        if "--input" in args:
            payload_path = Path(args[args.index("--input") + 1])
            self.payloads.append(json.loads(payload_path.read_text()))
        result = self.write_results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


def _context(client: IssueStateClient, *, writes_enabled: bool = True) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=writes_enabled),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _snapshot(
    *,
    state: str,
    reason: str | None,
    closed_at: str | None = None,
    updated_at: str = "2026-08-11T20:00:00Z",
) -> dict[str, Any]:
    return {
        "number": 18,
        "state": state,
        "state_reason": reason,
        "closed_at": closed_at,
        "updated_at": updated_at,
        "html_url": "https://github.com/octo/repo/issues/18",
    }


def test_result_model_exposes_exact_write_outcome_contract() -> None:
    schema = IssueStateTransitionResult.model_json_schema()

    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "number",
        "previous_state",
        "new_state",
        "state_reason",
        "closed_at",
        "reopened_at",
        "url",
    } == set(schema["properties"])


async def test_close_completed_checks_state_writes_once_and_verifies_readback() -> None:
    closed_at = "2026-08-11T20:01:00Z"
    client = IssueStateClient(
        read_results=[
            _snapshot(state="open", reason=None),
            _snapshot(state="closed", reason="completed", closed_at=closed_at),
        ],
        write_results=[
            GitHubRequestResult(
                value={},
                metadata=GitHubRequestMetadata(request_id="req-close-completed"),
            )
        ],
    )

    result = await gh_set_issue_state(
        "octo",
        "repo",
        18,
        "open",
        "closed",
        "completed",
        ctx=_context(client),
    )

    assert [kind for kind, _, _ in client.calls] == ["read", "write", "read"]
    assert client.payloads == [{"state": "closed", "state_reason": "completed"}]
    assert result.previous_state == "open"
    assert result.new_state == "closed"
    assert result.state_reason == "completed"
    assert result.closed_at == closed_at
    assert result.reopened_at is None
    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-close-completed"
    assert result.warning is None


@pytest.mark.parametrize("reason", ["not_planned", "duplicate"])
async def test_close_supports_other_valid_closed_reasons(reason: str) -> None:
    client = IssueStateClient(
        read_results=[
            _snapshot(state="open", reason=None),
            _snapshot(
                state="closed",
                reason=reason,
                closed_at="2026-08-11T20:02:00Z",
            ),
        ],
        write_results=[GitHubRequestResult(value={})],
    )

    result = await gh_set_issue_state(
        "octo",
        "repo",
        18,
        "open",
        "closed",
        reason,  # type: ignore[arg-type]
        ctx=_context(client),
    )

    assert result.state_reason == reason
    assert result.state_matches_requested is True
    assert client.payloads == [{"state": "closed", "state_reason": reason}]


async def test_reopen_prefers_confirmed_transition_timestamp_and_verifies_readback() -> None:
    reopened_at = "2026-08-11T20:03:00Z"
    readback_updated_at = "2026-08-11T20:03:01Z"
    client = IssueStateClient(
        read_results=[
            _snapshot(
                state="closed",
                reason="completed",
                closed_at="2026-08-11T19:00:00Z",
            ),
            _snapshot(state="open", reason="reopened", updated_at=readback_updated_at),
        ],
        write_results=[
            GitHubRequestResult(
                value=_snapshot(state="open", reason="reopened", updated_at=reopened_at)
            )
        ],
    )

    result = await gh_set_issue_state(
        "octo",
        "repo",
        18,
        "closed",
        "open",
        "reopened",
        ctx=_context(client),
    )

    assert client.payloads == [{"state": "open", "state_reason": "reopened"}]
    assert result.previous_state == "closed"
    assert result.new_state == "open"
    assert result.state_reason == "reopened"
    assert result.closed_at is None
    assert result.reopened_at == reopened_at
    assert result.state_matches_requested is True


async def test_stale_expected_state_fails_before_mutation_or_readback() -> None:
    client = IssueStateClient(
        read_results=[
            _snapshot(
                state="closed",
                reason="completed",
                closed_at="2026-08-11T19:00:00Z",
            )
        ]
    )

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_set_issue_state(
            "octo",
            "repo",
            18,
            "open",
            "closed",
            "completed",
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read"]
    assert client.payloads == []


@pytest.mark.parametrize(
    ("expected_state", "new_state", "reason"),
    [
        ("open", "closed", "reopened"),
        ("closed", "open", "completed"),
        ("closed", "open", "not_planned"),
        ("open", "open", "reopened"),
        ("closed", "closed", "completed"),
    ],
)
async def test_invalid_state_reason_combinations_fail_without_github_calls(
    expected_state: str,
    new_state: str,
    reason: str,
) -> None:
    client = IssueStateClient()

    with pytest.raises(ValueError):
        await gh_set_issue_state(
            "octo",
            "repo",
            18,
            expected_state,  # type: ignore[arg-type]
            new_state,  # type: ignore[arg-type]
            reason,  # type: ignore[arg-type]
            ctx=_context(client),
        )

    assert client.calls == []


async def test_known_mutation_failure_is_not_replayed_and_returns_failed_outcome() -> None:
    client = IssueStateClient(
        read_results=[
            _snapshot(state="open", reason=None),
            _snapshot(state="open", reason=None),
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

    result = await gh_set_issue_state(
        "octo",
        "repo",
        18,
        "open",
        "closed",
        "completed",
        ctx=_context(client),
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.previous_state == "open"
    assert result.new_state == "open"
    assert result.state_reason is None
    assert result.write_completed is False
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.request_id == "req-failed-transition"
    assert result.warning is not None
    assert "was not retried" in result.warning


async def test_readback_failure_preserves_completed_write_as_unverified() -> None:
    client = IssueStateClient(
        read_results=[
            _snapshot(state="open", reason=None),
            RuntimeError("readback unavailable"),
        ],
        write_results=[
            GitHubRequestResult(
                value={},
                metadata=GitHubRequestMetadata(request_id="req-readback-failed"),
            )
        ],
    )

    result = await gh_set_issue_state(
        "octo",
        "repo",
        18,
        "open",
        "closed",
        "completed",
        ctx=_context(client),
    )

    assert result.previous_state == "open"
    assert result.new_state is None
    assert result.state_reason is None
    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.request_id == "req-readback-failed"
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


async def test_wrong_reason_readback_is_not_verified_success() -> None:
    client = IssueStateClient(
        read_results=[
            _snapshot(state="open", reason=None),
            _snapshot(
                state="closed",
                reason="not_planned",
                closed_at="2026-08-11T20:04:00Z",
            ),
        ],
        write_results=[GitHubRequestResult(value={})],
    )

    result = await gh_set_issue_state(
        "octo",
        "repo",
        18,
        "open",
        "closed",
        "completed",
        ctx=_context(client),
    )

    assert result.new_state == "closed"
    assert result.state_reason == "not_planned"
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.warning is not None
    assert "does not match the requested state" in result.warning


async def test_pull_request_target_is_rejected_before_mutation() -> None:
    pull_request = _snapshot(state="open", reason=None)
    pull_request["pull_request"] = {"url": "https://api.github.com/repos/octo/repo/pulls/18"}
    client = IssueStateClient(read_results=[pull_request])

    with pytest.raises(RuntimeError, match="accepts issues only"):
        await gh_set_issue_state(
            "octo",
            "repo",
            18,
            "open",
            "closed",
            "completed",
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read"]
    assert client.payloads == []


async def test_write_gate_blocks_transition_before_any_github_call() -> None:
    client = IssueStateClient()

    with pytest.raises(RuntimeError, match="MCP_GH_ALLOW_WRITE_COMMANDS"):
        await gh_set_issue_state(
            "octo",
            "repo",
            18,
            "open",
            "closed",
            "completed",
            ctx=_context(client, writes_enabled=False),
        )

    assert client.calls == []
