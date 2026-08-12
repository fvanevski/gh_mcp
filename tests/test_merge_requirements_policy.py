"""Focused policy-completeness regressions for merge requirements."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from mcp_gh_server.merge_requirements_policy import read_effective_merge_policy
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    results: list[Any]

    async def run(self, *args: str, **kwargs: Any) -> Any:
        del args, kwargs
        return self.results.pop(0)


async def test_required_reviewers_make_policy_evidence_incomplete() -> None:
    rules = [
        {
            "type": "pull_request",
            "parameters": {
                "allowed_merge_methods": ["squash"],
                "required_approving_review_count": 1,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
                "required_reviewers": [
                    {
                        "minimum_approvals": 1,
                        "file_patterns": ["src/**"],
                        "reviewer_id": 123,
                    }
                ],
            },
        }
    ]
    client = FakeGhClient([rules, {"protected": False}])
    app = AppContext(client=client, settings=Settings())  # type: ignore[arg-type]

    policy, complete, warnings = await read_effective_merge_policy(
        app,
        "octo",
        "repo",
        "main",
    )

    assert policy is None
    assert complete is False
    assert len(warnings) == 1
    assert "required reviewers" in warnings[0]
