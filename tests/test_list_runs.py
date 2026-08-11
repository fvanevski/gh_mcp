"""Regression coverage for exact workflow-run discovery filters and completeness."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.server import AppContext, gh_list_runs
from mcp_gh_server.settings import Settings


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


def _run(run_id: int, *, head_sha: str | None = None) -> dict[str, Any]:
    sha = head_sha or f"{run_id:040x}"
    return {
        "id": run_id,
        "name": "CI",
        "display_title": f"Run {run_id}",
        "head_branch": "main",
        "head_sha": sha,
        "conclusion": "success",
        "status": "completed",
        "event": "push",
        "html_url": f"https://github.com/octo/repo/actions/runs/{run_id}",
        "created_at": "2026-08-01T10:00:00Z",
        "updated_at": "2026-08-01T10:01:00Z",
        "run_started_at": "2026-08-01T10:00:05Z",
    }


def _payload(total_count: int, runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"total_count": total_count, "workflow_runs": runs}


async def test_exact_filters_are_composed_on_authoritative_workflow_route() -> None:
    sha = "A" * 40
    client = FakeGhClient([_payload(1, [_run(11, head_sha=sha.casefold())])])

    result = await gh_list_runs(
        "octo",
        "repo",
        ctx=_context(client),
        branch="main",
        status="success",
        per_page=25,
        workflow_id=17,
        head_sha=sha,
        event="push",
        actor="octocat",
        created_from="2026-08-01T03:00:00-07:00",
        created_to="2026-08-02T12:30:00+02:00",
        check_suite_id=91,
        page=1,
    )

    assert client.calls == [
        (
            (
                "api",
                "repos/octo/repo/actions/workflows/17/runs",
                "-X",
                "GET",
                "-f",
                "page=1",
                "-f",
                "per_page=25",
                "-f",
                "exclude_pull_requests=true",
                "-f",
                "branch=main",
                "-f",
                "status=success",
                "-f",
                "event=push",
                "-f",
                "actor=octocat",
                "-f",
                "created=2026-08-01T10:00:00Z..2026-08-02T10:30:00Z",
                "-f",
                "check_suite_id=91",
                "-f",
                f"head_sha={sha.casefold()}",
            ),
            {},
        )
    ]
    assert result.total_count == 1
    assert result.page == 1
    assert result.per_page == 25
    assert result.has_more is False
    assert result.truncated is False
    assert result.warning is None
    assert result.items[0]["databaseId"] == 11
    assert result.items[0]["workflowName"] == "CI"


async def test_empty_result_is_complete_and_uses_default_page_size() -> None:
    client = FakeGhClient([_payload(0, [])])

    result = await gh_list_runs("octo", "repo", ctx=_context(client))

    assert result.total_count == 0
    assert result.items == []
    assert result.page == 1
    assert result.per_page == 30
    assert result.has_more is False
    assert result.truncated is False
    assert client.calls[0][0] == (
        "api",
        "repos/octo/repo/actions/runs",
        "-X",
        "GET",
        "-f",
        "page=1",
        "-f",
        "per_page=30",
        "-f",
        "exclude_pull_requests=true",
    )


async def test_second_page_reports_authoritative_last_page() -> None:
    client = FakeGhClient([_payload(3, [_run(3)])])

    result = await gh_list_runs(
        "octo",
        "repo",
        ctx=_context(client),
        page=2,
        per_page=2,
    )

    assert result.total_count == 3
    assert result.page == 2
    assert result.per_page == 2
    assert result.has_more is False
    assert result.truncated is False
    assert len(result.items) == 1


async def test_server_hard_cap_reports_incompleteness_instead_of_silent_clipping() -> None:
    settings = Settings(default_max_results=2, hard_max_results=2)
    client = FakeGhClient([_payload(3, [_run(1), _run(2)])])

    result = await gh_list_runs(
        "octo",
        "repo",
        ctx=_context(client, settings),
        per_page=5,
    )

    assert result.per_page == 2
    assert result.has_more is True
    assert result.truncated is True
    assert result.warning is not None
    assert "capped at the server hard limit of 2" in result.warning
    assert "per_page=2" in client.calls[0][0]


async def test_filtered_search_boundary_never_claims_global_completeness() -> None:
    settings = Settings(default_max_results=100, hard_max_results=100)
    runs = [_run(index) for index in range(901, 1001)]
    client = FakeGhClient([_payload(1_200, runs)])

    result = await gh_list_runs(
        "octo",
        "repo",
        ctx=_context(client, settings),
        branch="main",
        page=10,
        per_page=100,
    )

    assert result.total_count == 1_200
    assert result.has_more is False
    assert result.truncated is True
    assert result.warning is not None
    assert "1,000 results" in result.warning


async def test_legacy_branch_status_and_per_page_callers_keep_item_shape() -> None:
    client = FakeGhClient([_payload(1, [_run(5)])])

    result = await gh_list_runs(
        "octo",
        "repo",
        ctx=_context(client),
        branch="main",
        status="completed",
        per_page=10,
    )

    assert client.calls[0][0][1] == "repos/octo/repo/actions/runs"
    assert "branch=main" in client.calls[0][0]
    assert "status=completed" in client.calls[0][0]
    assert set(result.items[0]) == {
        "databaseId",
        "name",
        "displayTitle",
        "headBranch",
        "headSha",
        "conclusion",
        "status",
        "event",
        "url",
        "createdAt",
        "updatedAt",
        "startedAt",
        "workflowName",
    }


@pytest.mark.parametrize(
    ("created_from", "created_to"),
    [
        ("2026-08-01T10:00:00", None),
        ("2026-08-01", None),
        ("2026-08-02T10:00:00Z", "2026-08-01T10:00:00Z"),
        ("2026-08-01T10:00:00.500Z", None),
    ],
)
async def test_creation_bounds_reject_ambiguous_or_reversed_ranges(
    created_from: str | None,
    created_to: str | None,
) -> None:
    client = FakeGhClient([])

    with pytest.raises(ValueError):
        await gh_list_runs(
            "octo",
            "repo",
            ctx=_context(client),
            created_from=created_from,
            created_to=created_to,
        )

    assert client.calls == []
