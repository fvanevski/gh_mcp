"""0.6.x compatibility adapters for Git-reference writes."""

from __future__ import annotations

import logging
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from .legacy_write_support import (
    raise_known_unapplied,
    run_json_write_with_metadata,
    run_write_with_metadata,
)
from .models import BranchCreate, BranchCreateFromSha
from .request_governor import GitHubRequestResult
from .tooling import (
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
    app_from_context,
    require_write_enabled,
    validate_branch,
)
from .write_contracts import (
    WritePrecondition,
    execute_write_readback,
    legacy_write_status,
    require_write_precondition,
)

logger = logging.getLogger("mcp_gh_server.server")


async def gh_create_branch(
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner or organization login.",
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
        ),
    ],
    repo: Annotated[
        str,
        Field(
            description="GitHub repository name without the owner prefix.",
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
        ),
    ],
    issue_number: Annotated[int, Field(description="Positive issue number.", ge=1)],
    name: Annotated[
        str,
        Field(description="New development branch name.", min_length=1, max_length=1024),
    ],
    *,
    ctx: Context[AppContext],
    base: Annotated[
        str | None,
        Field(
            description=(
                "Existing branch name to use as the base. Full commit SHAs are rejected; "
                "use gh_create_branch_from_sha for an immutable base."
            ),
            min_length=1,
            max_length=1024,
        ),
    ] = None,
) -> BranchCreate:
    """Create a new branch for an issue from an existing branch name."""

    logger.info("MCP tool invocation reached server: tool=gh_create_branch")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="branch_create")
    validate_branch(name)
    if base is not None:
        if OBJECT_SHA_RE.fullmatch(base):
            raise ValueError(
                "base accepts branch names only; use gh_create_branch_from_sha "
                "with base_sha for an immutable commit base"
            )
        validate_branch(base)

    args = [
        "issue",
        "develop",
        str(issue_number),
        "--repo",
        f"{owner}/{repo}",
        "--name",
        name,
    ]
    if base:
        args.extend(["--base", base])

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(app.client, *args, json_output=False)

    async def readback() -> dict[str, Any]:
        # ``gh issue develop`` does not return a stable branch resource identity in
        # the frozen 0.6.x contract. Do not upgrade process success to verified
        # semantic success without an exact ref readback contract.
        raise RuntimeError(
            "legacy issue-development branch creation has no exact readback identity"
        )

    execution = await execute_write_readback(
        resource="Issue development branch",
        write=write,
        readback=readback,
        state_matches_requested=lambda _value: False,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    message = status.warning or f"Branch '{name}' created successfully for issue #{issue_number}."
    return BranchCreate(
        name=name,
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )


async def gh_create_branch_from_sha(
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner or organization login.",
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
        ),
    ],
    repo: Annotated[
        str,
        Field(
            description="GitHub repository name without the owner prefix.",
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
        ),
    ],
    name: Annotated[
        str,
        Field(description="New branch name to create.", min_length=1, max_length=1024),
    ],
    base_sha: Annotated[
        str,
        Field(
            description="Exact 40-character commit SHA at which to create the branch.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> BranchCreateFromSha:
    """Create an exact branch with authoritative readback and no write replay."""

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
        return await run_json_write_with_metadata(
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

    status = legacy_write_status(outcome)
    created = outcome.write_completed is True

    if outcome.write_completed is False and outcome.state_matches_requested is True:
        message = (
            f"Branch '{name}' already exists at the requested exact commit; no write was performed."
        )
    elif status.warning is not None:
        message = status.warning
    else:
        message = f"Branch '{name}' created at exact commit {expected}."

    return BranchCreateFromSha(
        name=name,
        base_sha=expected,
        ref=ref,
        created=created,
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
