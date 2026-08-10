"""Focused Git reference and branch tools."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..models import BranchCreate, BranchCreateFromSha
from ..tooling import (
    ADD_EXTERNAL,
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
    api_json_write,
    app_from_context,
    logger,
    mcp,
    require_write_enabled,
    validate_branch,
)


@mcp.tool(
    title="Create issue development branch",
    description=(
        "Additive write: create an issue development branch using a branch-name base. "
        "The base parameter does not accept a commit SHA; use gh_create_branch_from_sha "
        "when the branch must start at an immutable commit."
    ),
    annotations=ADD_EXTERNAL,
)
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

    await app.client.run(*args, json_output=False)
    return BranchCreate(
        name=name,
        message=f"Branch '{name}' created successfully for issue #{issue_number}.",
    )


@mcp.tool(
    title="Create branch from exact commit",
    description=(
        "Additive write: create one new branch at an exact 40-character commit SHA. "
        "The operation never moves or overwrites an existing branch, does not associate "
        "the branch with an issue, and performs no interactive prompting or MCP elicitation."
    ),
    annotations=ADD_EXTERNAL,
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
    """Create one new branch at an exact commit without moving an existing ref."""

    logger.info("MCP tool invocation reached server: tool=gh_create_branch_from_sha")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="branch_create")
    validate_branch(name)
    if not OBJECT_SHA_RE.fullmatch(base_sha):
        raise ValueError("base_sha must be a full 40-character Git object SHA")
    normalized_sha = base_sha.casefold()

    commit = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/git/commits/{normalized_sha}",
        "-X",
        "GET",
    )
    resolved_sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(resolved_sha, str) or resolved_sha.casefold() != normalized_sha:
        raise RuntimeError(
            "GitHub did not resolve base_sha to the same exact commit; no branch was created"
        )

    ref = f"refs/heads/{name}"
    branch_path = quote(name, safe="/")
    try:
        created_ref = await api_json_write(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/git/refs",
            {"ref": ref, "sha": normalized_sha},
        )
    except RuntimeError as write_error:
        try:
            existing_ref = await app.client.run(
                "api",
                f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
                "-X",
                "GET",
            )
        except RuntimeError:
            raise write_error from None
        existing_object = existing_ref.get("object") if isinstance(existing_ref, dict) else None
        existing_sha = existing_object.get("sha") if isinstance(existing_object, dict) else None
        if isinstance(existing_sha, str) and existing_sha.casefold() == normalized_sha:
            return BranchCreateFromSha(
                name=name,
                base_sha=normalized_sha,
                ref=ref,
                created=False,
                write_completed=False,
                message=(
                    f"Branch '{name}' already exists at the requested exact commit; "
                    "no write was performed."
                ),
            )
        if isinstance(existing_sha, str) and OBJECT_SHA_RE.fullmatch(existing_sha):
            raise RuntimeError(
                f"Branch {name!r} already exists at {existing_sha}; no ref was modified"
            ) from write_error
        raise write_error

    created_object = created_ref.get("object") if isinstance(created_ref, dict) else None
    created_sha = created_object.get("sha") if isinstance(created_object, dict) else None
    created_name = created_ref.get("ref") if isinstance(created_ref, dict) else None
    if (
        not isinstance(created_sha, str)
        or created_sha.casefold() != normalized_sha
        or created_name != ref
    ):
        warning = (
            "GitHub accepted the branch creation request but returned an unexpected ref; "
            "read the branch before continuing and do not retry automatically."
        )
        return BranchCreateFromSha(
            name=name,
            base_sha=normalized_sha,
            ref=ref,
            created=True,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    return BranchCreateFromSha(
        name=name,
        base_sha=normalized_sha,
        ref=ref,
        created=True,
        message=f"Branch '{name}' created at exact commit {normalized_sha}.",
    )
