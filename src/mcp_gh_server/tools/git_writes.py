"""Canonical exact-outcome Git reference write implementations."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..git_write_models import BranchCreateFromSha
from ..request_governor import GitHubRequestResult
from ..tooling import (
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    require_write_enabled,
    validate_branch,
)
from ..write_contracts import (
    WritePrecondition,
    execute_write_readback,
    require_write_precondition,
    run_api_json_write_with_metadata,
)


async def gh_create_branch_from_sha(
    owner: Annotated[str, Field(min_length=1, max_length=39, pattern=OWNER_RE.pattern)],
    repo: Annotated[str, Field(min_length=1, max_length=100, pattern=REPO_RE.pattern)],
    name: Annotated[str, Field(min_length=1, max_length=1024)],
    base_sha: Annotated[str, Field(pattern=r"^[0-9A-Fa-f]{40}$")],
    *,
    ctx: Context[AppContext],
) -> BranchCreateFromSha:
    """Create one new branch at an exact commit without moving an existing ref."""

    logger.info("MCP tool invocation reached server: tool=gh_create_branch_from_sha")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="branch_create")
    validate_branch(name)
    if not OBJECT_SHA_RE.fullmatch(base_sha):
        raise ValueError("base_sha must be a full 40-character Git object SHA")
    expected = base_sha.casefold()
    ref = f"refs/heads/{name}"
    branch_path = quote(name, safe="/")

    async def current_base() -> str:
        commit = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/git/commits/{expected}",
            "-X",
            "GET",
        )
        resolved_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(resolved_sha, str) or not OBJECT_SHA_RE.fullmatch(resolved_sha):
            raise RuntimeError(
                "GitHub did not resolve base_sha to a full commit SHA; no branch was created"
            )
        return resolved_sha.casefold()

    async def precondition() -> WritePrecondition[str]:
        return await require_write_precondition(
            current_base,
            expected,
            label="Branch base commit",
        )

    async def write() -> GitHubRequestResult[Any]:
        return await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/git/refs",
            {"ref": ref, "sha": expected},
        )

    async def readback() -> dict[str, Any]:
        existing_ref = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
            "-X",
            "GET",
        )
        if not isinstance(existing_ref, dict):
            raise RuntimeError("GitHub returned a non-object branch readback")
        return existing_ref

    def state_matches_requested(existing_ref: dict[str, Any]) -> bool:
        existing_object = existing_ref.get("object")
        existing_sha = existing_object.get("sha") if isinstance(existing_object, dict) else None
        existing_name = existing_ref.get("ref")
        return (
            existing_name == ref
            and isinstance(existing_sha, str)
            and existing_sha.casefold() == expected
        )

    execution = await execute_write_readback(
        resource="Branch creation",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=state_matches_requested,
    )
    outcome = execution.outcome
    existing_ref = execution.readback_value
    existing_sha: str | None = None
    if existing_ref is not None:
        existing_object = existing_ref.get("object")
        value = existing_object.get("sha") if isinstance(existing_object, dict) else None
        if isinstance(value, str) and OBJECT_SHA_RE.fullmatch(value):
            existing_sha = value.casefold()

    if (
        execution.error is not None
        and outcome.write_completed is False
        and outcome.state_matches_requested is not True
    ):
        if existing_sha is not None and existing_sha != expected:
            raise RuntimeError(
                f"Branch {name!r} already exists at {existing_sha}; no ref was modified"
            ) from execution.error
        raise execution.error

    created = outcome.write_completed is True
    if outcome.write_completed is False and outcome.state_matches_requested is True:
        message = (
            f"Branch '{name}' already exists at the requested exact commit; no write was performed."
        )
    elif outcome.warning is not None:
        message = outcome.warning
    else:
        message = f"Branch '{name}' created at exact commit {expected}."

    return BranchCreateFromSha(
        name=name,
        base_sha=expected,
        ref=ref,
        created=created,
        message=message,
        **outcome.model_dump(),
    )
