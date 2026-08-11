"""Focused Git reference and branch tools."""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..gh_client import GhClient
from ..models import BranchCreate, BranchCreateFromSha, GitObjectType, GitRefInfo, GitRefInput
from ..request_governor import GitHubRequestError
from ..tooling import (
    ADD_EXTERNAL,
    OBJECT_SHA_RE,
    OWNER_RE,
    READ_EXTERNAL,
    REPO_RE,
    AppContext,
    api_json_write,
    app_from_context,
    logger,
    mcp,
    require_write_enabled,
    validate_branch,
    validate_repository,
)

_MAX_TAG_PEEL_DEPTH = 16


def _parse_git_object(raw: object, *, resource: str) -> tuple[GitObjectType, str, str]:
    """Validate one GitHub Git-object descriptor without upgrading ambiguous evidence."""

    if not isinstance(raw, dict):
        raise RuntimeError(f"GitHub returned no Git object for {resource}")
    object_type = raw.get("type")
    object_sha = raw.get("sha")
    object_url = raw.get("url")
    if object_type not in {"commit", "tag", "tree", "blob"}:
        raise RuntimeError(f"GitHub returned an unsupported Git object type for {resource}")
    if not isinstance(object_sha, str) or not OBJECT_SHA_RE.fullmatch(object_sha):
        raise RuntimeError(f"GitHub returned no exact 40-character object SHA for {resource}")
    if not isinstance(object_url, str) or not object_url:
        raise RuntimeError(f"GitHub returned no object URL for {resource}")
    return cast(GitObjectType, object_type), object_sha.casefold(), object_url


async def _peel_annotated_tag(
    client: GhClient,
    owner: str,
    repo: str,
    tag_sha: str,
) -> str | None:
    """Peel a bounded chain of annotated tag objects to a commit when one exists."""

    current_sha = tag_sha
    seen: set[str] = set()
    for _ in range(_MAX_TAG_PEEL_DEPTH):
        if current_sha in seen:
            raise RuntimeError("GitHub annotated-tag peel returned a cyclic object chain")
        seen.add(current_sha)
        payload = await client.run(
            "api",
            f"repos/{owner}/{repo}/git/tags/{current_sha}",
            "-X",
            "GET",
        )
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"GitHub returned an unexpected response while peeling tag object {current_sha}"
            )
        returned_sha = payload.get("sha")
        if (
            not isinstance(returned_sha, str)
            or not OBJECT_SHA_RE.fullmatch(returned_sha)
            or returned_sha.casefold() != current_sha
        ):
            raise RuntimeError(
                "GitHub annotated-tag read did not preserve the exact requested tag-object SHA"
            )
        target_type, target_sha, _ = _parse_git_object(
            payload.get("object"),
            resource=f"annotated tag object {current_sha}",
        )
        if target_type == "commit":
            return target_sha
        if target_type != "tag":
            return None
        current_sha = target_sha

    raise RuntimeError(
        f"GitHub annotated-tag peel exceeded the {_MAX_TAG_PEEL_DEPTH}-object safety bound"
    )


async def _confirm_contents_read_access(client: GhClient, owner: str, repo: str) -> None:
    """Prove Contents-read access before classifying an exact-ref 404 as missing."""

    result = await client.run(
        "api",
        f"repos/{owner}/{repo}/branches",
        "-X",
        "GET",
        "-f",
        "per_page=1",
    )
    if not isinstance(result, list):
        raise RuntimeError(
            "GitHub returned an unexpected Contents-read probe response; ref absence is unverified"
        )


@mcp.tool(
    title="Get exact Git reference",
    description=(
        "Read-only: resolve one exact branch or tag reference path such as heads/main or "
        "tags/v1.0.0. This tool never performs matching-reference or prefix discovery. "
        "Annotated tag objects retain their tag-object identity and are peeled through "
        "bounded exact tag-object reads to a commit when applicable."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_ref(
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
    ref: Annotated[
        str,
        Field(
            description=(
                "One exact Git ref path relative to refs/, formatted as heads/<branch> or "
                "tags/<tag>. Do not pass refs/, a bare branch/tag name, or a matching prefix."
            ),
            min_length=6,
            max_length=1024,
            pattern=r"^(?:heads|tags)/.+$",
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> GitRefInfo:
    """Resolve exactly one branch/tag ref while preserving immutable Git object identity."""

    logger.info("MCP tool invocation reached server: tool=gh_get_ref")
    request = GitRefInput(owner=owner, repo=repo, ref=ref)
    validate_repository(request.owner, request.repo)
    app = app_from_context(ctx)
    expected_ref = f"refs/{request.ref}"
    ref_path = quote(request.ref, safe="/")

    try:
        payload = await app.client.run(
            "api",
            f"repos/{request.owner}/{request.repo}/git/ref/{ref_path}",
            "-X",
            "GET",
        )
    except GitHubRequestError as error:
        if error.status_code != 404:
            raise
        await _confirm_contents_read_access(app.client, request.owner, request.repo)
        return GitRefInfo(ref=expected_ref, found=False)

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub exact-reference lookup returned a non-object response")
    returned_ref = payload.get("ref")
    if returned_ref != expected_ref:
        raise RuntimeError(
            "GitHub exact-reference lookup returned a different ref; refusing ambiguous evidence"
        )
    object_type, object_sha, object_url = _parse_git_object(
        payload.get("object"),
        resource=expected_ref,
    )
    peeled_commit_sha = (
        await _peel_annotated_tag(app.client, request.owner, request.repo, object_sha)
        if object_type == "tag"
        else None
    )
    return GitRefInfo(
        ref=expected_ref,
        found=True,
        object_type=object_type,
        object_sha=object_sha,
        object_url=object_url,
        peeled_commit_sha=peeled_commit_sha,
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
