"""Regression coverage for exact-ref guarded workflow dispatch."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.workflow_dispatch import (
    WorkflowDispatchDuplicateError,
    gh_run_workflow_exact,
)
from mcp_gh_server.workflow_dispatch_models import WorkflowDispatchExactResult
from mcp_gh_server.write_contracts import WritePreconditionMismatch


@dataclass
class WorkflowDispatchClient:
    """Protocol fake with independent read and governed-write queues."""

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

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(
    client: WorkflowDispatchClient,
    *,
    writes_enabled: bool = True,
    workflow_dispatch_enabled: bool = True,
) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            allow_write_commands=writes_enabled,
            allow_workflow_dispatch=workflow_dispatch_enabled,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _ref(sha: str, *, ref: str = "refs/heads/main") -> dict[str, Any]:
    return {
        "ref": ref,
        "object": {
            "type": "commit",
            "sha": sha,
            "url": f"https://api.github.com/repos/octo/repo/git/commits/{sha}",
        },
    }


def _run(
    run_id: int,
    sha: str,
    *,
    workflow_id: int = 17,
    event: str = "workflow_dispatch",
    status: str = "queued",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "name": "Release",
        "display_title": "Release exact revision",
        "head_branch": "main",
        "head_sha": sha,
        "conclusion": None,
        "status": status,
        "event": event,
        "html_url": f"https://github.com/octo/repo/actions/runs/{run_id}",
        "created_at": "2026-08-12T05:00:00Z",
        "updated_at": "2026-08-12T05:00:01Z",
        "run_started_at": None,
    }


def _runs(*runs: dict[str, Any], total_count: int | None = None) -> dict[str, Any]:
    return {
        "total_count": len(runs) if total_count is None else total_count,
        "workflow_runs": list(runs),
    }


def _workflow() -> dict[str, Any]:
    return {
        "id": 17,
        "name": "Release",
        "path": ".github/workflows/release.yml",
        "state": "active",
    }


def test_result_model_exposes_standard_exact_write_contract() -> None:
    schema = WorkflowDispatchExactResult.model_json_schema()

    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "workflow_id",
        "ref",
        "expected_ref_sha",
        "resolved_ref_sha",
        "matching_run_count",
        "run_id",
        "run_url",
        "run_status",
        "run_head_sha",
    } == set(schema["properties"])


async def test_stale_ref_fails_before_duplicate_query_or_dispatch() -> None:
    expected = "a" * 40
    current = "b" * 40
    client = WorkflowDispatchClient(read_results=[_ref(current)])

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_run_workflow_exact(
            "octo",
            "repo",
            17,
            "heads/main",
            expected,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read"]
    assert client.payloads == []


async def test_existing_matching_dispatch_fails_closed_without_write() -> None:
    sha = "a" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(_run(91, sha, status="completed")),
            _workflow(),
        ]
    )

    with pytest.raises(WorkflowDispatchDuplicateError, match="no write was attempted"):
        await gh_run_workflow_exact(
            "octo",
            "repo",
            17,
            "heads/main",
            sha,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read", "read", "read"]
    assert client.payloads == []
    list_args = client.calls[1][1]
    assert list_args[1] == "repos/octo/repo/actions/workflows/17/runs"
    assert f"head_sha={sha}" in list_args
    assert "event=workflow_dispatch" in list_args
    assert not any(argument.startswith("status=") for argument in list_args)


async def test_successful_dispatch_checks_ref_twice_writes_once_and_reads_exact_run() -> None:
    sha = "a" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _ref(sha),
            _runs(_run(92, sha)),
            _workflow(),
        ],
        write_results=[
            GitHubRequestResult(
                value={},
                metadata=GitHubRequestMetadata(request_id="req-dispatch-92"),
            )
        ],
    )

    result = await gh_run_workflow_exact(
        "octo",
        "repo",
        17,
        "heads/main",
        sha.upper(),
        ctx=_context(client),
        fields=["environment=prod", "force=false"],
    )

    assert [kind for kind, _, _ in client.calls] == [
        "read",
        "read",
        "read",
        "write",
        "read",
        "read",
    ]
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert client.payloads == [
        {"ref": "main", "inputs": {"environment": "prod", "force": "false"}}
    ]
    write_args = next(args for kind, args, _ in client.calls if kind == "write")
    assert write_args[:4] == (
        "api",
        "repos/octo/repo/actions/workflows/17/dispatches",
        "-X",
        "POST",
    )
    readback_args = client.calls[4][1]
    assert readback_args[1] == "repos/octo/repo/actions/workflows/17/runs"
    assert f"head_sha={sha}" in readback_args
    assert "event=workflow_dispatch" in readback_args
    assert result.workflow_id == 17
    assert result.ref == "heads/main"
    assert result.expected_ref_sha == sha
    assert result.resolved_ref_sha == sha
    assert result.matching_run_count == 1
    assert result.run_id == 92
    assert result.run_url == "https://github.com/octo/repo/actions/runs/92"
    assert result.run_status == "queued"
    assert result.run_head_sha == sha
    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-dispatch-92"
    assert result.warning is None


async def test_ref_movement_after_duplicate_guard_stops_before_write() -> None:
    expected = "a" * 40
    moved = "b" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(expected),
            _runs(),
            _ref(moved),
        ]
    )

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_run_workflow_exact(
            "octo",
            "repo",
            17,
            "heads/main",
            expected,
            ctx=_context(client),
        )

    assert [kind for kind, _, _ in client.calls] == ["read", "read", "read"]
    assert client.payloads == []


async def test_known_dispatch_failure_is_not_replayed() -> None:
    sha = "a" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _ref(sha),
            _runs(),
        ],
        write_results=[
            GitHubRequestError(
                "workflow dispatch rejected",
                status_code=422,
                ambiguous=False,
                metadata=GitHubRequestMetadata(request_id="req-rejected"),
            )
        ],
    )

    result = await gh_run_workflow_exact(
        "octo",
        "repo",
        17,
        "heads/main",
        sha,
        ctx=_context(client),
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is False
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.matching_run_count == 0
    assert result.request_id == "req-rejected"
    assert result.warning is not None
    assert "mutation was not retried" in result.warning


async def test_delayed_readback_never_causes_second_dispatch() -> None:
    sha = "a" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _ref(sha),
            _runs(),
        ],
        write_results=[
            GitHubRequestResult(
                value={},
                metadata=GitHubRequestMetadata(request_id="req-delayed"),
            )
        ],
    )

    result = await gh_run_workflow_exact(
        "octo",
        "repo",
        17,
        "heads/main",
        sha,
        ctx=_context(client),
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.matching_run_count == 0
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


async def test_ambiguous_transport_and_delayed_readback_returns_unknown_write_once() -> None:
    sha = "a" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _ref(sha),
            _runs(),
        ],
        write_results=[
            GitHubRequestError(
                "connection reset",
                ambiguous=True,
                retryable=True,
                metadata=GitHubRequestMetadata(request_id="req-ambiguous"),
            )
        ],
    )

    result = await gh_run_workflow_exact(
        "octo",
        "repo",
        17,
        "heads/main",
        sha,
        ctx=_context(client),
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.matching_run_count == 0
    assert result.request_id == "req-ambiguous"
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "re-read authoritative state" in result.warning


async def test_ambiguous_transport_stays_unknown_even_when_readback_matches() -> None:
    sha = "a" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _ref(sha),
            _runs(_run(93, sha)),
            _workflow(),
        ],
        write_results=[
            GitHubRequestError(
                "connection reset",
                ambiguous=True,
                retryable=True,
                metadata=GitHubRequestMetadata(request_id="req-ambiguous-match"),
            )
        ],
    )

    result = await gh_run_workflow_exact(
        "octo",
        "repo",
        17,
        "heads/main",
        sha,
        ctx=_context(client),
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.matching_run_count == 1
    assert result.run_id == 93
    assert result.warning is not None
    assert "Do not retry the mutation" in result.warning


async def test_multiple_exact_readback_runs_are_ambiguous_and_never_redispatched() -> None:
    sha = "a" * 40
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _ref(sha),
            _runs(_run(94, sha), _run(95, sha)),
            _workflow(),
        ],
        write_results=[GitHubRequestResult(value={})],
    )

    result = await gh_run_workflow_exact(
        "octo",
        "repo",
        17,
        "heads/main",
        sha,
        ctx=_context(client),
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.matching_run_count == 2
    assert result.run_id is None
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


async def test_workflow_dispatch_gate_blocks_before_any_github_call() -> None:
    client = WorkflowDispatchClient()

    with pytest.raises(RuntimeError, match="MCP_GH_ALLOW_WORKFLOW_DISPATCH"):
        await gh_run_workflow_exact(
            "octo",
            "repo",
            17,
            "heads/main",
            "a" * 40,
            ctx=_context(client, workflow_dispatch_enabled=False),
        )

    assert client.calls == []


async def test_duplicate_input_keys_are_rejected_before_ref_read() -> None:
    client = WorkflowDispatchClient()

    with pytest.raises(ValueError, match="supplied more than once"):
        await gh_run_workflow_exact(
            "octo",
            "repo",
            17,
            "heads/main",
            "a" * 40,
            ctx=_context(client),
            fields=["environment=prod", "environment=staging"],
        )

    assert client.calls == []
