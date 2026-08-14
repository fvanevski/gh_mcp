"""Focused regressions for issue #58 canonical issue-domain write migration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_gh_server.legacy_issue_write_adapters as legacy_issue_writes
import mcp_gh_server.write_tool_schema as write_tool_schema
from mcp_gh_server.issue_write_models import (
    IssueCreateResult,
    IssueEditResult,
    LabelCreateResult,
    LabelEditResult,
    MilestoneCreateResult,
)
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import (
    AppContext,
    gh_create_issue,
    gh_create_label,
    gh_create_milestone,
    gh_edit_issue,
    gh_edit_label,
    mcp,
)
from mcp_gh_server.settings import Settings


@dataclass
class FakeCanonicalClient:
    """Separate one mutation result from authoritative readback results."""

    write_result: Any
    read_results: list[Any]
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        if isinstance(self.write_result, Exception):
            raise self.write_result
        return GitHubRequestResult(
            value=self.write_result,
            metadata=GitHubRequestMetadata(request_id="req-58"),
        )

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        if not self.read_results:
            raise RuntimeError("unexpected read")
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(client: FakeCanonicalClient) -> Any:
    settings = Settings(allow_write_commands=True)
    app = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


Invoke = Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True)
class WriteCase:
    name: str
    invoke: Invoke
    write_value: Any
    success_readback: dict[str, Any]
    mismatch_readback: dict[str, Any]
    result_type: type[Any]


async def _create_issue(ctx: Any) -> IssueCreateResult:
    return await gh_create_issue("octo", "repo", "Requested", ctx=ctx)


async def _edit_issue(ctx: Any) -> IssueEditResult:
    return await gh_edit_issue("octo", "repo", 4, ctx=ctx, title="Requested")


async def _create_label(ctx: Any) -> LabelCreateResult:
    return await gh_create_label("octo", "repo", "bug", "ff0000", ctx=ctx, description="Issue")


async def _edit_label(ctx: Any) -> LabelEditResult:
    return await gh_edit_label("octo", "repo", "bug", ctx=ctx, color="00ff00")


async def _create_milestone(ctx: Any) -> MilestoneCreateResult:
    return await gh_create_milestone("octo", "repo", "v1", ctx=ctx, description="Release")


CASES = (
    WriteCase(
        name="create_issue",
        invoke=_create_issue,
        write_value={"stdout": "https://github.com/octo/repo/issues/42\n"},
        success_readback={
            "number": 42,
            "title": "Requested",
            "url": "https://github.com/octo/repo/issues/42",
        },
        mismatch_readback={
            "number": 42,
            "title": "Different",
            "url": "https://github.com/octo/repo/issues/42",
        },
        result_type=IssueCreateResult,
    ),
    WriteCase(
        name="edit_issue",
        invoke=_edit_issue,
        write_value={"stdout": ""},
        success_readback={
            "number": 4,
            "title": "Requested",
            "state": "OPEN",
            "url": "https://github.com/octo/repo/issues/4",
        },
        mismatch_readback={
            "number": 4,
            "title": "Different",
            "state": "OPEN",
            "url": "https://github.com/octo/repo/issues/4",
        },
        result_type=IssueEditResult,
    ),
    WriteCase(
        name="create_label",
        invoke=_create_label,
        write_value={"stdout": ""},
        success_readback={
            "name": "bug",
            "color": "ff0000",
            "description": "Issue",
            "url": "https://api.github.com/repos/octo/repo/labels/bug",
        },
        mismatch_readback={
            "name": "bug",
            "color": "000000",
            "description": "Issue",
            "url": "https://api.github.com/repos/octo/repo/labels/bug",
        },
        result_type=LabelCreateResult,
    ),
    WriteCase(
        name="edit_label",
        invoke=_edit_label,
        write_value={"stdout": ""},
        success_readback={
            "name": "bug",
            "color": "00ff00",
            "description": None,
            "url": "https://api.github.com/repos/octo/repo/labels/bug",
        },
        mismatch_readback={
            "name": "bug",
            "color": "000000",
            "description": None,
            "url": "https://api.github.com/repos/octo/repo/labels/bug",
        },
        result_type=LabelEditResult,
    ),
    WriteCase(
        name="create_milestone",
        invoke=_create_milestone,
        write_value={
            "number": 7,
            "title": "v1",
            "state": "open",
            "description": "Release",
            "url": "https://api.github.com/repos/octo/repo/milestones/7",
        },
        success_readback={
            "number": 7,
            "title": "v1",
            "state": "open",
            "description": "Release",
            "url": "https://api.github.com/repos/octo/repo/milestones/7",
        },
        mismatch_readback={
            "number": 7,
            "title": "Different",
            "state": "open",
            "description": "Release",
            "url": "https://api.github.com/repos/octo/repo/milestones/7",
        },
        result_type=MilestoneCreateResult,
    ),
)


def _write_calls(client: FakeCanonicalClient) -> list[tuple[str, tuple[str, ...], dict[str, Any]]]:
    return [call for call in client.calls if call[0] == "write"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_migrated_write_success_is_verified(case: WriteCase) -> None:
    client = FakeCanonicalClient(case.write_value, [case.success_readback])
    result = await case.invoke(_context(client))
    assert isinstance(result, case.result_type)
    assert result.precondition_checked is False
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.request_id == "req-58"
    assert result.warning is None
    assert len(_write_calls(client)) == 1


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_migrated_write_known_failure_is_not_retried(case: WriteCase) -> None:
    client = FakeCanonicalClient(
        GitHubRequestError(
            "GitHub rejected the mutation",
            status_code=422,
            ambiguous=False,
            metadata=GitHubRequestMetadata(request_id="req-failed"),
        ),
        [case.success_readback],
    )
    result = await case.invoke(_context(client))
    assert isinstance(result, case.result_type)
    assert result.write_completed is False
    assert len(_write_calls(client)) == 1
    assert result.warning is not None
    assert "write failed" in result.warning


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_migrated_write_semantic_mismatch_is_explicit(case: WriteCase) -> None:
    client = FakeCanonicalClient(case.write_value, [case.mismatch_readback])
    result = await case.invoke(_context(client))
    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert len(_write_calls(client)) == 1
    assert result.warning is not None
    assert "does not match" in result.warning


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_migrated_write_readback_failure_is_partial_success(case: WriteCase) -> None:
    client = FakeCanonicalClient(case.write_value, [RuntimeError("readback unavailable")])
    result = await case.invoke(_context(client))
    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert len(_write_calls(client)) == 1
    assert result.warning is not None
    assert "Do not retry automatically" in result.warning


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_migrated_write_ambiguity_never_replays_mutation(case: WriteCase) -> None:
    client = FakeCanonicalClient(
        GitHubRequestError(
            "transport reset after send",
            retryable=True,
            ambiguous=True,
            metadata=GitHubRequestMetadata(request_id="req-ambiguous"),
        ),
        [case.success_readback],
    )
    result = await case.invoke(_context(client))
    assert result.write_completed is None
    assert len(_write_calls(client)) == 1
    assert result.warning is not None
    assert "outcome is unknown" in result.warning
    assert "Do not retry" in result.warning


async def test_migrated_public_output_schemas_expose_shared_outcome_metadata() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    outcome_fields = {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
    }
    for name in (
        "gh_create_issue",
        "gh_edit_issue",
        "gh_create_label",
        "gh_edit_label",
        "gh_create_milestone",
    ):
        assert outcome_fields <= set(tools[name].output_schema["properties"])


async def test_upsert_label_is_absent_from_public_registry() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "gh_upsert_label" not in tools


def test_public_facade_delegates_migrated_writes_to_canonical_module() -> None:
    for implementation in (
        write_tool_schema._gh_create_issue,
        write_tool_schema._gh_edit_issue,
        write_tool_schema._gh_create_label,
        write_tool_schema._gh_edit_label,
        write_tool_schema._gh_create_milestone,
    ):
        assert implementation.__module__ == "mcp_gh_server.tools.issue_writes"


def test_legacy_issue_write_aggregate_no_longer_reexports_migrated_writes() -> None:
    assert legacy_issue_writes.__all__ == []
    for name in (
        "gh_create_issue",
        "gh_edit_issue",
        "gh_create_label",
        "gh_edit_label",
        "gh_create_milestone",
        "gh_upsert_label",
    ):
        assert not hasattr(legacy_issue_writes, name)
