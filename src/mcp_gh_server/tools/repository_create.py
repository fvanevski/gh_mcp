"""Canonical exact-target repository creation."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from ..repository_create_models import RepositoryCreateResult
from ..request_governor import GitHubRequestResult
from ..tooling import AppContext, app_from_context, logger, require_write_enabled
from ..write_contracts import execute_write_readback

_REPOSITORY_READBACK_FIELDS = "nameWithOwner,url,isPrivate,description,isEmpty"


def _matches_requested_repository(
    snapshot: dict[str, Any],
    *,
    owner: str,
    repo: str,
    private: bool,
    description: str | None,
    auto_init: bool,
) -> bool:
    """Compare authoritative repository readback with the requested observable state."""

    name_with_owner = snapshot.get("nameWithOwner")
    if not isinstance(name_with_owner, str):
        return False
    if name_with_owner.casefold() != f"{owner}/{repo}".casefold():
        return False
    if snapshot.get("isPrivate") is not private:
        return False
    if snapshot.get("description") != description:
        return False

    is_empty = snapshot.get("isEmpty")
    if isinstance(is_empty, bool) and (not is_empty) is not auto_init:
        return False
    return True


async def gh_create_repo(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    private: bool = False,
    auto_init: bool = False,
) -> RepositoryCreateResult:
    """Create exactly one allowlisted repository and verify its authoritative state."""

    logger.info("MCP tool invocation reached server: tool=gh_create_repo")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="repo_create")

    full_name = f"{owner}/{repo}"
    create_args = ["repo", "create", full_name, "--private" if private else "--public"]
    if description is not None:
        create_args.extend(["--description", description])
    if auto_init:
        create_args.append("--add-readme")

    async def write() -> GitHubRequestResult[Any]:
        return await app.client.run_with_metadata(*create_args, json_output=False)

    async def readback() -> dict[str, Any]:
        value = await app.client.run(
            "repo",
            "view",
            full_name,
            "--json",
            _REPOSITORY_READBACK_FIELDS,
        )
        if not isinstance(value, dict):
            raise RuntimeError(f"GitHub returned an invalid repository readback for {full_name}")
        return value

    execution = await execute_write_readback(
        resource=f"repository {full_name}",
        write=write,
        readback=readback,
        state_matches_requested=lambda snapshot: _matches_requested_repository(
            snapshot,
            owner=owner,
            repo=repo,
            private=private,
            description=description,
            auto_init=auto_init,
        ),
    )

    snapshot = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    name_with_owner = snapshot.get("nameWithOwner")
    url = snapshot.get("url")
    is_private = snapshot.get("isPrivate")
    readback_description = snapshot.get("description")
    is_empty = snapshot.get("isEmpty")

    return RepositoryCreateResult(
        owner=owner,
        repo=repo,
        name_with_owner=name_with_owner if isinstance(name_with_owner, str) else None,
        url=url if isinstance(url, str) else None,
        is_private=is_private if isinstance(is_private, bool) else None,
        description=readback_description if isinstance(readback_description, str) else None,
        initialized=(not is_empty) if isinstance(is_empty, bool) else None,
        **execution.outcome.model_dump(),
    )
