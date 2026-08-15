"""Regressions for the gh_list_issues label-filter CLI contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from mcp_gh_server.server import AppContext, gh_list_issues
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record issue-list argv and return queued results."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.results.pop(0)

    def clamp_max_results(self, requested: int | None) -> int:
        return 30 if requested is None else requested


def _context(client: FakeGhClient) -> Any:
    app = AppContext(client=client, settings=Settings())  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


async def test_list_issues_uses_supported_singular_label_flag() -> None:
    items = [{"number": 7, "title": "Filtered", "state": "OPEN"}]
    client = FakeGhClient([items])

    result = await gh_list_issues(
        "octo",
        "repo",
        ctx=_context(client),
        state="all",
        per_page=20,
        labels="bug,priority",
    )

    args = client.calls[0][0]
    assert "--labels" not in args
    assert args[args.index("--label") + 1] == "bug,priority"
    assert result.items == items
    assert result.total_count == 1


async def test_list_issues_omits_label_flag_without_filter() -> None:
    client = FakeGhClient([[]])

    await gh_list_issues("octo", "repo", ctx=_context(client), per_page=20)

    args = client.calls[0][0]
    assert "--label" not in args
    assert "--labels" not in args
