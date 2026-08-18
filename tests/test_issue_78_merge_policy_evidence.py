"""Issue #78 regressions for complete fail-closed merge-policy evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from mcp_gh_server.merge_requirements_policy import read_effective_merge_policy_evidence
from mcp_gh_server.models import PullRequestCheck, PullRequestChecks
from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools import merge_requirements as merge_tool


@dataclass
class FakeGhClient:
    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _app(client: FakeGhClient) -> AppContext:
    return AppContext(client=client, settings=Settings())  # type: ignore[arg-type]


def _context(client: FakeGhClient) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=_app(client)))


def _source(result: Any, name: str) -> Any:
    return next(source for source in result.evidence_sources if source.source == name)


def _required_check_rule(context: str = "Pyrefly") -> dict[str, Any]:
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [{"context": context, "integration_id": None}],
        },
    }


async def test_classic_404_with_verified_admin_read_is_authoritative_absence() -> None:
    client = FakeGhClient([[], {"protected": True}, GitHubRequestError("not found", status_code=404), []])
    result = await read_effective_merge_policy_evidence(_app(client), "octo", "repo", "main")
    assert result.complete is True
    assert result.policy is not None
    assert result.warnings == []
    classic = _source(result, "classic_branch_protection")
    permission = _source(result, "classic_protection_admin_permission")
    assert classic.status == "absent"
    assert classic.http_status == 404
    assert classic.blocks_policy_evidence is False
    assert permission.status == "complete"
    assert any(args[:2] == ("api", "repos/octo/repo/rulesets/rule-suites") for args, _ in client.calls)


async def test_classic_404_without_admin_read_proof_remains_incomplete() -> None:
    client = FakeGhClient([
        [], {"protected": True}, GitHubRequestError("not found", status_code=404),
        GitHubRequestError("forbidden", status_code=403),
    ])
    result = await read_effective_merge_policy_evidence(_app(client), "octo", "repo", "main")
    assert result.complete is False
    assert result.policy is None
    classic = _source(result, "classic_branch_protection")
    permission = _source(result, "classic_protection_admin_permission")
    assert classic.status == "unavailable"
    assert classic.http_status == 404
    assert classic.blocks_policy_evidence is True
    assert classic.blocks_checks_evidence is True
    assert classic.blocks_allowed_merge_methods is True
    assert permission.status == "unavailable"
    assert permission.http_status == 403
    assert any("cannot be established" in warning for warning in result.warnings)


async def test_active_rules_compose_when_classic_404_is_verified_absent() -> None:
    client = FakeGhClient([
        [_required_check_rule("Pyrefly")], {"protected": True},
        GitHubRequestError("not found", status_code=404), [],
    ])
    result = await read_effective_merge_policy_evidence(_app(client), "octo", "repo", "main")
    assert result.complete is True
    assert result.policy is not None
    assert {(c.context, c.integration_id) for c in result.policy.required_status_checks.values()} == {("Pyrefly", None)}
    assert _source(result, "effective_branch_rules").status == "complete"
    assert _source(result, "classic_branch_protection").status == "absent"
    assert _source(result, "policy_composition").status == "complete"


async def test_firecrawl_285_shape_becomes_complete_only_with_all_policy_sources(monkeypatch: Any) -> None:
    base = "a" * 40
    head = "b" * 40
    metadata = {
        "base": {"ref": "main", "sha": base}, "head": {"ref": "feature", "sha": head},
        "mergeable": True, "mergeable_state": "clean",
    }
    metadata_reads = [metadata, metadata]
    async def fake_metadata(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return metadata_reads.pop(0)
    async def fake_checks(*_args: Any, **_kwargs: Any) -> PullRequestChecks:
        return PullRequestChecks(
            number=285, base_sha=base, head_sha=head, total_count=1, truncated=False,
            checks=[PullRequestCheck(name="Pyrefly", state="SUCCESS", bucket="pass", workflow="CI")],
        )
    async def fake_freshness(*_args: Any, **_kwargs: Any) -> tuple[bool, bool, list[str]]:
        return True, True, []
    async def fake_review_state(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            current_head_approvals=[], current_head_change_requests=[], stale_approvals=[],
            stale_change_requests=[], unresolved_review_threads=[], review_decision=None,
            requirements_satisfied=None, exact_head_evidence=True, head_matches_expected=True,
            warning=None,
        )
    monkeypatch.setattr(merge_tool, "_get_pr_metadata", fake_metadata)
    monkeypatch.setattr(merge_tool, "gh_get_pr_checks", fake_checks)
    monkeypatch.setattr(merge_tool, "_read_up_to_date", fake_freshness)
    monkeypatch.setattr(merge_tool, "gh_get_pr_review_state", fake_review_state)
    client = FakeGhClient([
        [_required_check_rule("Pyrefly")], {"protected": True},
        GitHubRequestError("not found", status_code=404), [],
        {"allow_merge_commit": True, "allow_squash_merge": True, "allow_rebase_merge": True},
    ])
    result = await merge_tool.gh_get_merge_requirements(
        "fvanevski", "firecrawl_skill", 285, head, ctx=_context(client)
    )
    assert result.policy_evidence_complete is True
    assert result.checks_evidence_complete is True
    assert result.allowed_merge_methods_complete is True
    assert [(c.context, c.integration_id) for c in result.required_status_checks] == [("Pyrefly", None)]
    assert [c.name for c in result.current_required_checks] == ["Pyrefly"]
    assert result.allowed_merge_methods == ["merge", "squash", "rebase"]
    assert result.warning is None
    assert (_source(result, "classic_branch_protection").status, _source(result, "classic_branch_protection").http_status) == ("absent", 404)
    assert _source(result, "classic_protection_admin_permission").status == "complete"
    assert _source(result, "current_required_checks").status == "complete"
    assert _source(result, "pull_request_identity").status == "complete"
    assert not any(s.blocks_policy_evidence or s.blocks_checks_evidence or s.blocks_allowed_merge_methods for s in result.evidence_sources)


async def test_firecrawl_285_shape_stays_fail_closed_when_admin_probe_is_forbidden(monkeypatch: Any) -> None:
    base = "a" * 40
    head = "b" * 40
    metadata = {
        "base": {"ref": "main", "sha": base}, "head": {"ref": "feature", "sha": head},
        "mergeable": True, "mergeable_state": "clean",
    }
    metadata_reads = [metadata, metadata]
    async def fake_metadata(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return metadata_reads.pop(0)
    async def fake_checks(*_args: Any, **_kwargs: Any) -> PullRequestChecks:
        return PullRequestChecks(
            number=285, base_sha=base, head_sha=head, total_count=1, truncated=False,
            checks=[PullRequestCheck(name="Pyrefly", state="SUCCESS", bucket="pass", workflow="CI")],
        )
    async def fake_freshness(*_args: Any, **_kwargs: Any) -> tuple[bool, bool, list[str]]:
        return True, True, []
    async def fake_review_state(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            current_head_approvals=[], current_head_change_requests=[], stale_approvals=[],
            stale_change_requests=[], unresolved_review_threads=[], review_decision=None,
            requirements_satisfied=None, exact_head_evidence=True, head_matches_expected=True,
            warning=None,
        )
    monkeypatch.setattr(merge_tool, "_get_pr_metadata", fake_metadata)
    monkeypatch.setattr(merge_tool, "gh_get_pr_checks", fake_checks)
    monkeypatch.setattr(merge_tool, "_read_up_to_date", fake_freshness)
    monkeypatch.setattr(merge_tool, "gh_get_pr_review_state", fake_review_state)
    client = FakeGhClient([
        [_required_check_rule("Pyrefly")], {"protected": True},
        GitHubRequestError("not found", status_code=404), GitHubRequestError("forbidden", status_code=403),
        {"allow_merge_commit": True, "allow_squash_merge": True, "allow_rebase_merge": True},
    ])
    result = await merge_tool.gh_get_merge_requirements(
        "fvanevski", "firecrawl_skill", 285, head, ctx=_context(client)
    )
    assert result.policy_evidence_complete is False
    assert result.checks_evidence_complete is False
    assert result.allowed_merge_methods_complete is False
    assert result.required_status_checks == []
    assert result.allowed_merge_methods == []
    assert _source(result, "classic_branch_protection").status == "unavailable"
    assert _source(result, "classic_protection_admin_permission").status == "unavailable"
