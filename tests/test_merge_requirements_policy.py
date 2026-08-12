"""Focused policy-completeness regressions for merge requirements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp_gh_server.merge_requirements_policy import read_effective_merge_policy
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.results.pop(0)


def _app(client: FakeGhClient, settings: Settings | None = None) -> AppContext:
    return AppContext(
        client=client,  # type: ignore[arg-type]
        settings=settings or Settings(),
    )


def _pull_request_rule() -> dict[str, Any]:
    return {
        "type": "pull_request",
        "parameters": {
            "allowed_merge_methods": ["squash"],
            "required_approving_review_count": 1,
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_review_thread_resolution": False,
        },
    }


async def test_required_reviewers_make_policy_evidence_incomplete() -> None:
    rule = _pull_request_rule()
    rule["parameters"]["required_reviewers"] = [
        {
            "minimum_approvals": 1,
            "file_patterns": ["src/**"],
            "reviewer": {"id": 123, "type": "Team"},
        }
    ]
    client = FakeGhClient([[rule], {"protected": False}])

    policy, complete, warnings = await read_effective_merge_policy(
        _app(client),
        "octo",
        "repo",
        "main",
    )

    assert policy is None
    assert complete is False
    assert len(warnings) == 1
    assert "required reviewers" in warnings[0]


async def test_exact_active_rule_bound_is_not_reported_as_truncated() -> None:
    settings = Settings(default_max_results=2, hard_max_results=2)
    rules = [_pull_request_rule(), {"type": "required_linear_history"}]
    client = FakeGhClient([rules, [], {"protected": False}])

    policy, complete, warnings = await read_effective_merge_policy(
        _app(client, settings),
        "octo",
        "repo",
        "main",
    )

    assert complete is True
    assert policy is not None
    assert policy.allowed_merge_methods == {"squash"}
    assert warnings == []
    probe_args = client.calls[1][0]
    assert "page=3" in probe_args
    assert "per_page=1" in probe_args


async def test_strict_ruleset_without_checks_does_not_require_up_to_date_head() -> None:
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [],
            },
        }
    ]
    client = FakeGhClient([rules, {"protected": False}])

    policy, complete, warnings = await read_effective_merge_policy(
        _app(client),
        "octo",
        "repo",
        "main",
    )

    assert complete is True
    assert policy is not None
    assert policy.up_to_date_required is False
    assert warnings == []
