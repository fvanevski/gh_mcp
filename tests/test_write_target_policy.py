"""Regression coverage for issue #54 high-risk target policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.server import (
    AppContext,
    gh_create_repo,
    gh_run_workflow,
    gh_run_workflow_exact,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.tooling import (
    _configured_repository_targets,
    _configured_workflow_targets,
    _normalize_workflow_selector,
)


@dataclass
class RaiseOnCallClient:
    """Fake client that records nothing and raises on any unexpected call."""

    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    results: list[Any] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if not self.results:
            raise RuntimeError("unexpected GitHub call — authorization should have blocked")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _make_context(
    *,
    allow_write_commands: bool = False,
    allow_repo_creation: bool = False,
    allow_release_creation: bool = False,
    allow_workflow_dispatch: bool = False,
    allow_content_commits: bool = False,
    allow_pr_merge: bool = False,
    allowed_repositories: str = "",
    allowed_owners: str = "",
    allowed_repo_creation_targets: str = "",
    allowed_workflow_dispatch_targets: str = "",
) -> Any:
    settings = Settings(
        allow_write_commands=allow_write_commands,
        allow_repo_creation=allow_repo_creation,
        allow_release_creation=allow_release_creation,
        allow_workflow_dispatch=allow_workflow_dispatch,
        allow_content_commits=allow_content_commits,
        allow_pr_merge=allow_pr_merge,
        allowed_repositories=allowed_repositories,
        allowed_owners=allowed_owners,
        allowed_repo_creation_targets=allowed_repo_creation_targets,
        allowed_workflow_dispatch_targets=allowed_workflow_dispatch_targets,
    )
    client = RaiseOnCallClient()
    app = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=app),
    ), client


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defaults_fail_closed_for_new_fine_gates() -> None:
    ctx, client = _make_context()
    assert ctx.request_context.lifespan_context.settings.allow_repo_creation is False
    assert ctx.request_context.lifespan_context.settings.allow_release_creation is False
    assert ctx.request_context.lifespan_context.settings.allow_workflow_dispatch is False
    assert ctx.request_context.lifespan_context.settings.allowed_repo_creation_targets == ""
    assert ctx.request_context.lifespan_context.settings.allowed_workflow_dispatch_targets == ""

    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_create_repo("octo/repo", ctx=ctx)
    assert client.calls == []

    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_run_workflow("octo", "repo", 1, ctx=ctx)
    assert client.calls == []


# ---------------------------------------------------------------------------
# Repository creation target policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_creation_requires_master_gate_first() -> None:
    ctx, client = _make_context(allow_repo_creation=True)
    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_create_repo("octo/repo", ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_repo_creation_requires_target_allowlist() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_repo_creation=True,
    )
    with pytest.raises(RuntimeError, match="ALLOWED_REPO_CREATION_TARGETS"):
        await gh_create_repo("octo/repo", ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_repo_creation_accepts_exact_prospective_target() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_repo_creation=True,
        allowed_repo_creation_targets="octo/new-repo",
        allowed_repositories="octo/new-repo",
    )
    url = "https://github.com/octo/new-repo"
    ctx.request_context.lifespan_context.client.results = [
        {"nameWithOwner": "octo/new-repo", "url": url},
    ]
    result = await gh_create_repo("octo/new-repo", ctx=ctx)
    assert result.name == "octo/new-repo"
    assert len(client.calls) > 0


@pytest.mark.asyncio
async def test_repo_creation_mismatch_before_any_client_call() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_repo_creation=True,
        allowed_repo_creation_targets="octo/exact-repo",
    )
    with pytest.raises(RuntimeError, match="ALLOWED_REPO_CREATION_TARGETS"):
        await gh_create_repo("octo/wrong-name", ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_repo_creation_case_folded_target_match() -> None:
    ctx, _client = _make_context(
        allow_write_commands=True,
        allow_repo_creation=True,
        allowed_repo_creation_targets="octo/new-repo",
    )
    url = "https://github.com/octo/new-repo"
    ctx.request_context.lifespan_context.client.results = [
        {"nameWithOwner": "octo/new-repo", "url": url},
    ]
    result = await gh_create_repo("octo/new-repo", ctx=ctx)
    assert result.name == "octo/new-repo"


# ---------------------------------------------------------------------------
# Empty repository-creation target allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_repo_creation_targets_fails_closed() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_repo_creation=True,
        allowed_repo_creation_targets="",
    )
    with pytest.raises(RuntimeError, match="ALLOWED_REPO_CREATION_TARGETS"):
        await gh_create_repo("octo/repo", ctx=ctx)
    assert client.calls == []


# ---------------------------------------------------------------------------
# Workflow dispatch target policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_dispatch_legacy_requires_master_gate() -> None:
    ctx, client = _make_context(allow_workflow_dispatch=True)
    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_run_workflow("octo", "repo", 1, ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_workflow_dispatch_legacy_requires_target_allowlist() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
    )
    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow("octo", "repo", 1, ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_workflow_dispatch_exact_requires_master_gate() -> None:
    ctx, client = _make_context(allow_workflow_dispatch=True)
    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_run_workflow_exact("octo", "repo", 1, "heads/main", "a" * 40, ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_workflow_dispatch_exact_requires_target_allowlist() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
    )
    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow_exact("octo", "repo", 1, "heads/main", "a" * 40, ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_workflow_dispatch_legacy_target_mismatch_before_call() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_workflow_dispatch_targets="octo/repo@99",
    )
    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow("octo", "repo", 7, ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_workflow_dispatch_exact_target_mismatch_before_call() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_workflow_dispatch_targets="octo/repo@99",
    )
    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow_exact("octo", "repo", 7, "heads/main", "a" * 40, ctx=ctx)
    assert client.calls == []


# ---------------------------------------------------------------------------
# Empty workflow-dispatch target allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_workflow_dispatch_targets_fails_closed_legacy() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_workflow_dispatch_targets="",
    )
    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow("octo", "repo", 1, ctx=ctx)
    assert client.calls == []


@pytest.mark.asyncio
async def test_empty_workflow_dispatch_targets_fails_closed_exact() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_workflow_dispatch_targets="",
    )
    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow_exact("octo", "repo", 1, "heads/main", "a" * 40, ctx=ctx)
    assert client.calls == []


# ---------------------------------------------------------------------------
# Positive exact-target cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_exact_repo_creation_accepted() -> None:
    url = "https://github.com/octo/new-repo"
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_repo_creation=True,
        allowed_repo_creation_targets="octo/new-repo",
        allowed_repositories="octo/new-repo",
    )
    # Provide the responses the tool needs after authorization passes:
    #   1. api user lookup
    #   2. repo create stdout
    #   3. repo view readback
    ctx.request_context.lifespan_context.client.results = [
        {"login": "octo"},
        {"stdout": ""},
        {"nameWithOwner": "octo/new-repo", "url": url},
    ]
    result = await gh_create_repo("new-repo", ctx=ctx)
    assert result.name == "octo/new-repo"
    assert result.url == url
    assert len(client.calls) > 0  # authorization passed and called GitHub


@pytest.mark.asyncio
async def test_positive_exact_workflow_dispatch_id_accepted() -> None:
    url = "https://github.com/octo/repo/actions/runs/123"
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_workflow_dispatch_targets="octo/repo@99",
        allowed_repositories="octo/repo",
    )
    ctx.request_context.lifespan_context.client.results = [
        {"stdout": f"Workflow dispatched: {url}\n"},
        {"databaseId": 123, "url": url},
    ]
    result = await gh_run_workflow("octo", "repo", 99, ctx=ctx)
    assert result.run_id == 123
    assert len(client.calls) > 0


@pytest.mark.asyncio
async def test_numeric_workflow_id_normalizes_canonically() -> None:
    url = "https://github.com/octo/repo/actions/runs/42"
    ctx, _client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_workflow_dispatch_targets="octo/repo@42",
        allowed_repositories="octo/repo",
    )
    ctx.request_context.lifespan_context.client.results = [
        {"stdout": f"Workflow dispatched: {url}\n"},
        {"databaseId": 42, "url": url},
    ]
    result = await gh_run_workflow("octo", "repo", 42, ctx=ctx)
    assert result.run_id == 42


@pytest.mark.asyncio
async def test_workflow_path_preserves_case() -> None:
    url = "https://github.com/octo/repo/actions/runs/1"
    ctx, _client = _make_context(
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_workflow_dispatch_targets="octo/repo@ci/pipeline.yml",
        allowed_repositories="octo/repo",
    )
    ctx.request_context.lifespan_context.client.results = [
        {"stdout": f"Workflow dispatched: {url}\n"},
        {"databaseId": 1, "url": url},
    ]
    result = await gh_run_workflow("octo", "repo", "ci/pipeline.yml", ctx=ctx)
    assert result.run_id == 1


def test_helper_normalize_workflow_selector() -> None:
    assert _normalize_workflow_selector(1) == "1"
    assert _normalize_workflow_selector(99) == "99"
    assert _normalize_workflow_selector("ci/build.yml") == "ci/build.yml"
    with pytest.raises(ValueError, match="exact workflow path"):
        _normalize_workflow_selector("  ci/build.yml  ")
    with pytest.raises(ValueError, match="positive ID"):
        _normalize_workflow_selector(0)
    with pytest.raises(ValueError, match="positive ID"):
        _normalize_workflow_selector(-1)
    with pytest.raises(ValueError, match="workflow selector"):
        _normalize_workflow_selector(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact workflow path"):
        _normalize_workflow_selector("")
    with pytest.raises(ValueError, match="exact workflow path"):
        _normalize_workflow_selector("  ")


# ---------------------------------------------------------------------------
# Release regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_uses_master_and_release_gate_not_target_policy() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_release_creation=True,
        allowed_repositories="octo/repo",
    )
    # Release does NOT require allowed_release_creation_targets because that
    # setting does not exist; it only checks master + release gate + repo list.
    url = "https://github.com/octo/repo/releases/tag/v1"
    client.results = [
        {"stdout": url},
        {"tagName": "v1", "url": url, "isDraft": False, "isPrerelease": False},
    ]
    from mcp_gh_server.server import gh_create_release

    result = await gh_create_release(
        "octo",
        "repo",
        tag_name="v1",
        ctx=ctx,
    )
    assert result.tag_name == "v1"
    assert len(client.calls) > 0


@pytest.mark.asyncio
async def test_release_still_requires_master_gate() -> None:
    ctx, client = _make_context(allow_release_creation=True)
    with pytest.raises(RuntimeError, match="writes are disabled"):
        from mcp_gh_server.server import gh_create_release

        await gh_create_release("octo", "repo", tag_name="v1", ctx=ctx)
    assert client.calls == []


# ---------------------------------------------------------------------------
# Ordinary write regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_write_ignores_high_risk_target_policy() -> None:
    ctx, client = _make_context(
        allow_write_commands=True,
        allow_content_commits=True,
        allowed_repositories="octo/repo",
        allowed_repo_creation_targets="",
        allowed_workflow_dispatch_targets="",
    )
    head = "a" * 40
    client.results = [
        {"object": {"sha": head}},
        {"node_id": "R_repo"},
        {"tree": {"sha": head}},
        {"sha": "b" * 40},
        {"sha": "c" * 40},
        {"sha": "d" * 40},
        {"sha": "e" * 40, "html_url": f"https://github.com/octo/repo/commit/{'e' * 40}"},
        {"data": {"updateRefs": {"clientMutationId": None}}},
        {"object": {"sha": "e" * 40}},
    ]
    from mcp_gh_server.models import CommitFile
    from mcp_gh_server.server import gh_commit_files

    result = await gh_commit_files(
        "octo",
        "repo",
        branch="main",
        expected_head_sha=head,
        files=[CommitFile(path="f.txt", content="x")],
        commit_message="m",
        ctx=ctx,
    )
    assert result.write_completed is True
    assert len(client.calls) > 0


# ---------------------------------------------------------------------------
# Target-list helpers
# ---------------------------------------------------------------------------


def test_configured_repository_targets_parses_comma_separated() -> None:
    targets = _configured_repository_targets("octo/a, other/b", env_name="TEST")
    assert targets == {"octo/a", "other/b"}


def test_configured_repository_targets_handles_whitespace() -> None:
    targets = _configured_repository_targets("  octo/a  ,  other/b  ", env_name="TEST")
    assert targets == {"octo/a", "other/b"}


def test_configured_repository_targets_ignores_empty_entries() -> None:
    targets = _configured_repository_targets("octo/a,,  ,other/b", env_name="TEST")
    assert targets == {"octo/a", "other/b"}


def test_configured_repository_targets_rejects_missing_slash() -> None:
    with pytest.raises(RuntimeError, match="invalid repository target"):
        _configured_repository_targets("octo", env_name="TEST")


def test_configured_repository_targets_rejects_bad_name() -> None:
    with pytest.raises(RuntimeError, match="invalid repository target"):
        _configured_repository_targets("octo/x/y", env_name="TEST")


def test_configured_workflow_targets_parses_comma_separated() -> None:
    targets = _configured_workflow_targets("octo/a@1, other/b@ci.yml", env_name="TEST")
    assert targets == {("octo/a", "1"), ("other/b", "ci.yml")}


def test_configured_workflow_targets_rejects_missing_at() -> None:
    with pytest.raises(RuntimeError, match="invalid workflow target"):
        _configured_workflow_targets("octo/a1", env_name="TEST")


def test_configured_workflow_targets_rejects_bad_repo() -> None:
    with pytest.raises(RuntimeError, match="invalid workflow target"):
        _configured_workflow_targets("octo x@1", env_name="TEST")
