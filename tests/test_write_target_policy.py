"""Regression coverage for issue #54 high-risk target policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.server import (
    AppContext,
    gh_create_release,
    gh_create_repo,
    gh_run_workflow,
    gh_run_workflow_exact,
    mcp,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.tooling import (
    _configured_repository_targets,
    _configured_workflow_targets,
    require_write_enabled,
)
from mcp_gh_server.workflow_selector import WORKFLOW_PATH_RE, resolve_workflow_id
from mcp_gh_server.write_contracts import WritePreconditionMismatch
from test_write_wrappers import FakeGhClient

WORKFLOW_PATH = ".github/workflows/Release.yml"
OTHER_WORKFLOW_PATH = ".github/workflows/Other.yml"


def _context(client: FakeGhClient, **overrides: Any) -> Any:
    settings = Settings(**overrides)
    app = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _ref(sha: str) -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "object": {
            "type": "commit",
            "sha": sha,
            "url": f"https://api.github.com/repos/octo/repo/git/commits/{sha}",
        },
    }


async def test_defaults_fail_closed_for_high_risk_gates_and_target_lists() -> None:
    settings = Settings()
    assert settings.allow_repo_creation is False
    assert settings.allow_release_creation is False
    assert settings.allow_workflow_dispatch is False
    assert settings.allowed_repo_creation_targets == ""
    assert settings.allowed_workflow_dispatch_targets == ""

    client = FakeGhClient([])
    ctx = _context(client)
    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_create_repo("octo/new-repo", ctx=ctx)
    with pytest.raises(RuntimeError, match="writes are disabled"):
        await gh_run_workflow("octo", "repo", 17, ctx=ctx)
    assert client.calls == []


async def test_repo_creation_exact_prospective_target_does_not_require_owner_allowlist() -> None:
    url = "https://github.com/octo/new-repo"
    client = FakeGhClient(
        [
            {"stdout": f"{url}\n"},
            {"nameWithOwner": "octo/new-repo", "url": url},
        ]
    )
    ctx = _context(
        client,
        allow_write_commands=True,
        allow_repo_creation=True,
        allowed_repositories="octo/existing,octo/new-repo",
        allowed_repo_creation_targets="octo/new-repo",
    )

    result = await gh_create_repo("octo/new-repo", ctx=ctx)

    assert result.name == "octo/new-repo"
    assert result.url == url
    assert len(client.calls) == 2
    assert "octo" not in ctx.request_context.lifespan_context.settings.allowed_owners


async def test_repo_creation_target_mismatch_fails_before_any_github_call() -> None:
    client = FakeGhClient([])
    ctx = _context(
        client,
        allow_write_commands=True,
        allow_repo_creation=True,
        allowed_repositories="octo/wrong-name",
        allowed_repo_creation_targets="octo/exact-repo",
    )

    with pytest.raises(RuntimeError, match="ALLOWED_REPO_CREATION_TARGETS"):
        await gh_create_repo("octo/wrong-name", ctx=ctx)
    assert client.calls == []


async def test_workflow_dispatch_numeric_target_mismatch_fails_before_any_github_call() -> None:
    client = FakeGhClient([])
    ctx = _context(
        client,
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_repositories="octo/repo",
        allowed_workflow_dispatch_targets="octo/repo@99",
    )

    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow("octo", "repo", 17, ctx=ctx)
    assert client.calls == []


async def test_workflow_dispatch_path_target_mismatch_fails_before_resolution() -> None:
    client = FakeGhClient([])
    ctx = _context(
        client,
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_repositories="octo/repo",
        allowed_workflow_dispatch_targets=f"octo/repo@{OTHER_WORKFLOW_PATH}",
    )

    with pytest.raises(RuntimeError, match="ALLOWED_WORKFLOW_DISPATCH_TARGETS"):
        await gh_run_workflow("octo", "repo", WORKFLOW_PATH, ctx=ctx)
    assert client.calls == []


async def test_workflow_path_resolves_by_file_and_preserves_exact_case() -> None:
    client = FakeGhClient([{"id": 17, "path": WORKFLOW_PATH}])
    ctx = _context(client)
    app = ctx.request_context.lifespan_context

    workflow_id = await resolve_workflow_id(app, "octo", "repo", WORKFLOW_PATH)

    assert workflow_id == 17
    assert client.calls == [
        (
            (
                "api",
                "repos/octo/repo/actions/workflows/Release.yml",
                "-X",
                "GET",
            ),
            {},
        )
    ]


async def test_workflow_path_resolution_rejects_case_mismatch() -> None:
    client = FakeGhClient([{"id": 17, "path": WORKFLOW_PATH.lower()}])
    ctx = _context(client)
    app = ctx.request_context.lifespan_context

    with pytest.raises(RuntimeError, match="expected exact path"):
        await resolve_workflow_id(app, "octo", "repo", WORKFLOW_PATH)
    assert len(client.calls) == 1


async def test_legacy_workflow_dispatch_accepts_exact_authorized_path() -> None:
    url = "https://github.com/octo/repo/actions/runs/123"
    client = FakeGhClient(
        [
            {"id": 17, "path": WORKFLOW_PATH},
            {"stdout": f"Workflow dispatched: {url}\n"},
            {"databaseId": 123, "url": url},
        ]
    )
    ctx = _context(
        client,
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_repositories="octo/repo",
        allowed_workflow_dispatch_targets=f"octo/repo@{WORKFLOW_PATH}",
    )

    result = await gh_run_workflow("octo", "repo", WORKFLOW_PATH, ctx=ctx)

    assert result.run_id == 123
    assert client.calls[0][0][1] == "repos/octo/repo/actions/workflows/Release.yml"
    assert client.calls[1][0][:3] == ("workflow", "run", "17")


async def test_exact_workflow_path_resolves_before_exact_ref_precondition() -> None:
    expected = "1" * 40
    current = "2" * 40
    client = FakeGhClient(
        [
            {"id": 17, "path": WORKFLOW_PATH},
            _ref(current),
        ]
    )
    ctx = _context(
        client,
        allow_write_commands=True,
        allow_workflow_dispatch=True,
        allowed_repositories="octo/repo",
        allowed_workflow_dispatch_targets=f"octo/repo@{WORKFLOW_PATH}",
    )

    with pytest.raises(WritePreconditionMismatch, match="no write was attempted"):
        await gh_run_workflow_exact(
            "octo",
            "repo",
            WORKFLOW_PATH,
            "heads/main",
            expected,
            ctx=ctx,
        )

    assert len(client.calls) == 2
    assert client.calls[0][0][1] == "repos/octo/repo/actions/workflows/Release.yml"
    assert not any("dispatches" in argument for args, _ in client.calls for argument in args)


async def test_release_preserves_master_fine_gate_and_repository_policy() -> None:
    url = "https://github.com/octo/repo/releases/tag/v1"
    client = FakeGhClient(
        [
            {"stdout": f"{url}\n"},
            {"tagName": "v1", "url": url, "isDraft": False, "isPrerelease": False},
        ]
    )
    ctx = _context(
        client,
        allow_write_commands=True,
        allow_release_creation=True,
        allowed_repositories="octo/repo",
    )

    result = await gh_create_release("octo", "repo", tag_name="v1", ctx=ctx)
    assert result.tag_name == "v1"
    assert len(client.calls) == 2

    blocked_client = FakeGhClient([])
    blocked_ctx = _context(
        blocked_client,
        allow_write_commands=True,
        allow_release_creation=False,
        allowed_repositories="octo/repo",
    )
    with pytest.raises(RuntimeError, match="ALLOW_RELEASE_CREATION"):
        await gh_create_release("octo", "repo", tag_name="v1", ctx=blocked_ctx)
    assert blocked_client.calls == []


async def test_ordinary_repository_policy_ignores_new_target_lists() -> None:
    client = FakeGhClient([])
    ctx = _context(
        client,
        allow_write_commands=True,
        allowed_repositories="octo/repo",
        allowed_repo_creation_targets="",
        allowed_workflow_dispatch_targets="",
    )
    app = ctx.request_context.lifespan_context

    require_write_enabled(app, "octo", "repo", action="issue_edit")
    with pytest.raises(RuntimeError, match="not allowed for repository"):
        require_write_enabled(app, "octo", "other", action="issue_edit")
    assert client.calls == []


async def test_public_workflow_schema_advertises_bounded_id_or_canonical_path() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    for tool_name in ("gh_run_workflow", "gh_run_workflow_exact"):
        workflow = tools[tool_name].input_schema["properties"]["workflow_id"]
        branches = workflow["anyOf"]
        integer = next(branch for branch in branches if branch.get("type") == "integer")
        path = next(branch for branch in branches if branch.get("type") == "string")
        assert integer["minimum"] == 1
        assert path["pattern"] == WORKFLOW_PATH_RE.pattern
        assert path["maxLength"] == 1024
        assert "canonical" in workflow["description"]


def test_target_parsers_preserve_exact_path_case_and_casefold_repository_identity() -> None:
    assert _configured_repository_targets("Octo/New-Repo", env_name="TEST") == {
        "octo/new-repo"
    }
    targets = _configured_workflow_targets(
        f"Octo/Repo@{WORKFLOW_PATH}",
        env_name="TEST",
    )
    assert targets == {("octo/repo", WORKFLOW_PATH)}
    assert ("octo/repo", WORKFLOW_PATH.lower()) not in targets
