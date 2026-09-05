"""Regression coverage for exact-ref guarded workflow dispatch."""

from __future__ import annotations

import asyncio
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
    MAX_WORKFLOW_INPUT_CHARACTERS,
    MAX_WORKFLOW_INPUTS,
    _WORKFLOW_DISPATCH_RESERVATIONS,
    WorkflowDispatchDuplicateError,
    WorkflowDispatchRefAmbiguityError,
    _WorkflowDispatchReservation,
    gh_run_workflow_exact,
)
from mcp_gh_server.workflow_dispatch_models import WorkflowDispatchExactResult
from mcp_gh_server.write_contracts import WritePreconditionMismatch

WORKFLOW_PATH = ".github/workflows/release.yml"


@dataclass
class WorkflowDispatchClient:
    """Protocol fake with independent read and governed-write queues."""

    read_results: list[Any] = field(default_factory=list)
    write_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    write_entered: asyncio.Event | None = None
    release_write: asyncio.Event | None = None

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
        if self.write_entered is not None:
            self.write_entered.set()
        if self.release_write is not None:
            await self.release_write.wait()
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


def _sha(value: int) -> str:
    return f"{value:040x}"


def _ref(sha: str, *, ref: str = "refs/heads/main") -> dict[str, Any]:
    return {
        "ref": ref,
        "object": {
            "type": "commit",
            "sha": sha,
            "url": f"https://api.github.com/repos/octo/repo/git/commits/{sha}",
        },
    }


def _annotated_ref(tag_object_sha: str, *, ref: str = "refs/tags/v1.0.0") -> dict[str, Any]:
    return {
        "ref": ref,
        "object": {
            "type": "tag",
            "sha": tag_object_sha,
            "url": f"https://api.github.com/repos/octo/repo/git/tags/{tag_object_sha}",
        },
    }


def _tag_object(tag_object_sha: str, commit_sha: str) -> dict[str, Any]:
    return {
        "sha": tag_object_sha,
        "object": {
            "type": "commit",
            "sha": commit_sha,
            "url": f"https://api.github.com/repos/octo/repo/git/commits/{commit_sha}",
        },
    }


def _missing_ref() -> list[Any]:
    return [GitHubRequestError("missing ref", status_code=404), []]


def _run(
    run_id: int,
    sha: str,
    *,
    workflow_id: int = 17,
    event: str = "workflow_dispatch",
    status: str = "queued",
    head_branch: str = "main",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "name": "Release",
        "display_title": "Release exact revision",
        "head_branch": head_branch,
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


def _workflow(
    *,
    workflow_id: int = 17,
    path: str = WORKFLOW_PATH,
    state: str = "active",
) -> dict[str, Any]:
    return {
        "id": workflow_id,
        "name": "Release",
        "path": path,
        "state": state,
    }


def _receipt(run_id: int) -> dict[str, Any]:
    return {
        "workflow_run_id": run_id,
        "run_url": f"https://api.github.com/repos/octo/repo/actions/runs/{run_id}",
        "html_url": f"https://github.com/octo/repo/actions/runs/{run_id}",
    }


def _successful_reads(sha: str, run_id: int) -> list[Any]:
    return [
        _ref(sha),
        _runs(),
        _runs(),
        *_missing_ref(),
        _ref(sha),
        _workflow(),
        _run(run_id, sha),
    ]


def _exact_args(sha: str, *, ref: str = "heads/main") -> tuple[str, str, int, str, str, str]:
    return "octo", "repo", 17, WORKFLOW_PATH, ref, sha


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
        "run_event",
    } == set(schema["properties"])


async def test_stale_ref_fails_before_duplicate_query_or_dispatch() -> None:
    expected = _sha(1)
    current = _sha(2)
    client = WorkflowDispatchClient(read_results=[_ref(current)])

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_run_workflow_exact(*_exact_args(expected), ctx=_context(client))

    assert [kind for kind, _, _ in client.calls] == ["read"]
    assert client.payloads == []


async def test_existing_nonterminal_matching_dispatch_fails_closed_without_write() -> None:
    sha = _sha(3)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(total_count=0),
            _runs(total_count=1),
        ]
    )

    with pytest.raises(
        WorkflowDispatchDuplicateError, match=r"nonterminal.*no write was attempted"
    ):
        await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert [kind for kind, _, _ in client.calls] == ["read", "read", "read"]
    assert client.payloads == []
    completed_args = client.calls[1][1]
    list_args = client.calls[2][1]
    assert list_args[1] == "repos/octo/repo/actions/workflows/17/runs"
    assert f"head_sha={sha}" in list_args
    assert "event=workflow_dispatch" in list_args
    assert not any(argument.startswith("status=") for argument in list_args)
    assert f"head_sha={sha}" in completed_args
    assert "event=workflow_dispatch" in completed_args
    assert "status=completed" in completed_args


async def test_completed_historical_dispatch_does_not_block_new_dispatch() -> None:
    sha = _sha(30)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(total_count=1),
            _runs(total_count=1),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
            _run(91, sha),
        ],
        write_results=[GitHubRequestResult(value=_receipt(91))],
    )

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert result.state_matches_requested is True
    assert result.run_id == 91
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    completed_args = client.calls[1][1]
    assert "status=completed" in completed_args


async def test_concurrent_new_dispatch_between_terminal_probes_fails_closed() -> None:
    sha = _sha(32)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(total_count=1),
            _runs(total_count=2),
        ]
    )

    with pytest.raises(WorkflowDispatchDuplicateError, match="1 matching nonterminal"):
        await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 0


async def test_terminal_history_counter_regression_is_uncertain_and_never_dispatches() -> None:
    sha = _sha(33)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(total_count=1),
            _runs(total_count=0),
        ]
    )

    with pytest.raises(RuntimeError, match=r"history changed.*no write was attempted"):
        await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 0


async def test_same_name_branch_and_tag_fails_closed_even_at_same_sha() -> None:
    sha = _sha(4)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            _ref(sha, ref="refs/tags/main"),
        ]
    )

    with pytest.raises(WorkflowDispatchRefAmbiguityError, match="both refs/heads/main"):
        await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 0
    assert client.payloads == []


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (_workflow(workflow_id=18), "workflow ID 17"),
        (_workflow(path=".github/workflows/other.yml"), "expected exact path"),
        (_workflow(state="disabled_manually"), "is not active"),
    ],
)
async def test_workflow_identity_or_state_mismatch_fails_immediately_before_write(
    workflow: dict[str, Any],
    message: str,
) -> None:
    sha = _sha(5)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            workflow,
        ]
    )

    with pytest.raises(RuntimeError, match=message):
        await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 0
    assert client.calls[-1][1] == (
        "api",
        "repos/octo/repo/actions/workflows/17",
        "-X",
        "GET",
    )


async def test_successful_dispatch_uses_returned_run_identity_for_readback() -> None:
    sha = _sha(6)
    client = WorkflowDispatchClient(
        read_results=_successful_reads(sha, 92),
        write_results=[
            GitHubRequestResult(
                value=_receipt(92),
                metadata=GitHubRequestMetadata(request_id="req-dispatch-92"),
            )
        ],
    )

    result = await gh_run_workflow_exact(
        *_exact_args(sha.upper()),
        ctx=_context(client),
        inputs={"environment": "prod", "force": "false"},
    )

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert client.payloads == [
        {
            "ref": "main",
            "return_run_details": True,
            "inputs": {"environment": "prod", "force": "false"},
        }
    ]
    write_index = next(index for index, (kind, _, _) in enumerate(client.calls) if kind == "write")
    workflow_read_args = client.calls[write_index - 1][1]
    assert workflow_read_args == (
        "api",
        "repos/octo/repo/actions/workflows/17",
        "-X",
        "GET",
    )
    write_args = client.calls[write_index][1]
    assert write_args[:4] == (
        "api",
        "repos/octo/repo/actions/workflows/17/dispatches",
        "-X",
        "POST",
    )
    readback_args = client.calls[-1][1]
    assert readback_args == (
        "api",
        "repos/octo/repo/actions/runs/92",
        "-X",
        "GET",
    )
    assert result.workflow_id == 17
    assert result.ref == "heads/main"
    assert result.expected_ref_sha == sha
    assert result.resolved_ref_sha == sha
    assert result.matching_run_count == 1
    assert result.run_id == 92
    assert result.run_url == "https://github.com/octo/repo/actions/runs/92"
    assert result.run_status == "queued"
    assert result.run_head_sha == sha
    assert result.run_event == "workflow_dispatch"
    assert result.precondition_checked is True
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-dispatch-92"
    assert result.warning is None


async def test_annotated_tag_dispatch_uses_peeled_commit_and_short_tag_name() -> None:
    commit_sha = _sha(7)
    tag_sha = _sha(8)
    client = WorkflowDispatchClient(
        read_results=[
            _annotated_ref(tag_sha),
            _tag_object(tag_sha, commit_sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _annotated_ref(tag_sha),
            _tag_object(tag_sha, commit_sha),
            _workflow(),
            _run(96, commit_sha, head_branch="v1.0.0"),
        ],
        write_results=[GitHubRequestResult(value=_receipt(96))],
    )

    result = await gh_run_workflow_exact(
        *_exact_args(commit_sha, ref="tags/v1.0.0"),
        ctx=_context(client),
    )

    assert client.payloads == [{"ref": "v1.0.0", "return_run_details": True}]
    assert result.run_id == 96
    assert result.run_head_sha == commit_sha
    assert result.run_event == "workflow_dispatch"
    assert result.state_matches_requested is True


async def test_ref_movement_after_duplicate_guard_stops_before_write() -> None:
    expected = _sha(9)
    moved = _sha(10)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(expected),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(moved),
        ]
    )

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_run_workflow_exact(*_exact_args(expected), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 0
    assert client.payloads == []


async def test_post_dispatch_ref_movement_is_bound_to_returned_run_and_fails_closed() -> None:
    expected = _sha(11)
    moved = _sha(12)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(expected),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(expected),
            _workflow(),
            _run(97, moved),
        ],
        write_results=[GitHubRequestResult(value=_receipt(97))],
    )

    result = await gh_run_workflow_exact(*_exact_args(expected), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.run_id == 97
    assert result.run_head_sha == moved
    assert result.run_event == "workflow_dispatch"
    assert result.warning is not None
    assert "ref may have moved" in result.warning
    assert "no retry was attempted" in result.warning


async def test_returned_run_id_mismatch_does_not_accept_another_run() -> None:
    sha = _sha(13)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
            _run(99, sha),
        ],
        write_results=[GitHubRequestResult(value=_receipt(98))],
    )

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.run_id is None
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


async def test_malformed_success_receipt_never_guesses_created_run_from_discovery() -> None:
    sha = _sha(14)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
        ],
        write_results=[GitHubRequestResult(value={})],
    )

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.run_id is None
    assert result.warning is not None
    assert "return_run_details identity" in result.warning
    assert "not guessed from filtered discovery" in result.warning


async def test_known_dispatch_failure_is_not_replayed() -> None:
    sha = _sha(15)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
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

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is False
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.matching_run_count == 0
    assert result.request_id == "req-rejected"
    assert result.warning is not None
    assert "mutation was not retried" in result.warning


async def test_successful_dispatch_with_delayed_exact_run_readback_is_not_replayed() -> None:
    sha = _sha(16)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
            GitHubRequestError("run not visible yet", status_code=404),
        ],
        write_results=[
            GitHubRequestResult(
                value=_receipt(100),
                metadata=GitHubRequestMetadata(request_id="req-delayed"),
            )
        ],
    )

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.matching_run_count is None
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


async def test_ambiguous_transport_and_delayed_readback_returns_unknown_write_once() -> None:
    sha = _sha(17)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
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

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.matching_run_count == 0
    assert result.request_id == "req-ambiguous"
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "re-read authoritative state" in result.warning


async def test_ambiguous_transport_with_completed_history_never_reuses_old_run() -> None:
    sha = _sha(18)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(total_count=1),
            _runs(total_count=1),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
        ],
        write_results=[
            GitHubRequestError(
                "connection reset",
                ambiguous=True,
                retryable=True,
                metadata=GitHubRequestMetadata(request_id="req-ambiguous-history"),
            )
        ],
    )

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is None
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.matching_run_count is None
    assert result.run_id is None
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "authoritative readback also failed" in result.warning


async def test_ambiguous_transport_stays_unknown_even_when_fallback_readback_matches() -> None:
    sha = _sha(31)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
            _runs(_run(101, sha)),
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

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.matching_run_count == 1
    assert result.run_id == 101
    assert result.run_event == "workflow_dispatch"
    assert result.warning is not None
    assert "Do not retry the mutation" in result.warning


async def test_multiple_fallback_matches_are_ambiguous_and_never_redispatched() -> None:
    sha = _sha(19)
    client = WorkflowDispatchClient(
        read_results=[
            _ref(sha),
            _runs(),
            _runs(),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
            _runs(_run(102, sha), _run(103, sha)),
            _workflow(),
        ],
        write_results=[
            GitHubRequestError(
                "connection reset",
                ambiguous=True,
                retryable=True,
            )
        ],
    )

    result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.matching_run_count == 2
    assert result.run_id is None
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


async def test_completed_local_reservation_is_released_before_new_dispatch() -> None:
    sha = _sha(20)
    key = ("octo", "repo", 17, sha)
    _WORKFLOW_DISPATCH_RESERVATIONS[key] = _WorkflowDispatchReservation(
        run_id=104,
        outcome_unknown=False,
    )
    client = WorkflowDispatchClient(
        read_results=[
            _run(104, sha, status="completed"),
            _ref(sha),
            _runs(total_count=1),
            _runs(total_count=1),
            *_missing_ref(),
            _ref(sha),
            _workflow(),
            _run(105, sha),
        ],
        write_results=[GitHubRequestResult(value=_receipt(105))],
    )

    try:
        result = await gh_run_workflow_exact(*_exact_args(sha), ctx=_context(client))
        assert result.state_matches_requested is True
        assert result.run_id == 105
        assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    finally:
        _WORKFLOW_DISPATCH_RESERVATIONS.pop(key, None)


async def test_concurrent_same_key_invocations_serialize_and_dispatch_once() -> None:
    sha = _sha(21)
    write_entered = asyncio.Event()
    release_write = asyncio.Event()
    client = WorkflowDispatchClient(
        read_results=[
            *_successful_reads(sha, 104),
            _run(104, sha),
        ],
        write_results=[GitHubRequestResult(value=_receipt(104))],
        write_entered=write_entered,
        release_write=release_write,
    )
    ctx = _context(client)

    first = asyncio.create_task(gh_run_workflow_exact(*_exact_args(sha), ctx=ctx))
    await write_entered.wait()
    second = asyncio.create_task(gh_run_workflow_exact(*_exact_args(sha), ctx=ctx))
    await asyncio.sleep(0)

    assert sum(kind == "write" for kind, _, _ in client.calls) == 1
    release_write.set()
    first_result = await first
    with pytest.raises(WorkflowDispatchDuplicateError, match="already issued by this server"):
        await second

    assert first_result.state_matches_requested is True
    assert sum(kind == "write" for kind, _, _ in client.calls) == 1


async def test_workflow_dispatch_gate_blocks_before_any_github_call() -> None:
    client = WorkflowDispatchClient()

    with pytest.raises(RuntimeError, match="MCP_GH_ALLOW_WORKFLOW_DISPATCH"):
        await gh_run_workflow_exact(
            *_exact_args(_sha(21)),
            ctx=_context(client, workflow_dispatch_enabled=False),
        )

    assert client.calls == []


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (["environment=prod"], "must be an object"),  # type: ignore[list-item]
        ({"": "prod"}, "keys must be non-empty"),
        ({"environment": 1}, "values must be strings"),  # type: ignore[dict-item]
        (
            {f"key-{index}": "value" for index in range(MAX_WORKFLOW_INPUTS + 1)},
            "at most 25 entries",
        ),
        (
            {"key": "x" * MAX_WORKFLOW_INPUT_CHARACTERS},
            "65,535 aggregate",
        ),
    ],
)
async def test_malformed_or_oversized_inputs_fail_before_github_reads(
    inputs: Any,
    message: str,
) -> None:
    client = WorkflowDispatchClient()

    with pytest.raises(ValueError, match=message):
        await gh_run_workflow_exact(
            *_exact_args(_sha(22)),
            ctx=_context(client),
            inputs=inputs,
        )

    assert client.calls == []
