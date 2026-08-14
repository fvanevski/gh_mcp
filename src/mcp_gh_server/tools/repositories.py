"""Repository metadata and content read tools."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..models import RepoInfo, RepositoryFile, SearchResults
from ..tooling import (
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repo_path,
    validate_repository,
)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_get_repo(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
) -> RepoInfo:
    """Get details of a specific repository."""

    app = app_from_context(ctx)
    fields = (
        "nameWithOwner,name,owner,description,url,isPrivate,isFork,primaryLanguage,"
        "stargazerCount,forkCount,createdAt,pushedAt,defaultBranchRef,licenseInfo"
    )
    result = await app.client.run(
        "repo",
        "view",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    owner_obj = result.get("owner")
    if isinstance(owner_obj, dict):
        result["owner"] = owner_obj.get("login")
    lang_obj = result.get("primaryLanguage")
    if isinstance(lang_obj, dict):
        result["primaryLanguage"] = lang_obj.get("name")
    branch_obj = result.get("defaultBranchRef")
    if isinstance(branch_obj, dict):
        result["defaultBranchRef"] = branch_obj.get("name")
    return RepoInfo.model_validate(result)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_repos(
    *,
    ctx: Context[AppContext],
    username: str | None = None,
    type: str = "all",
    per_page: int | None = None,
    sort: str = "updated",
    direction: str = "desc",
) -> SearchResults:
    """List repositories for a user or organization.

    type: all, owner, member, public, private, fork.
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "nameWithOwner,name,description,stargazerCount,forkCount,primaryLanguage,"
        "createdAt,pushedAt,defaultBranchRef"
    )
    args = ["repo", "list"]
    if username:
        args.append(username)
    args.extend(
        [
            "--json",
            fields,
            "--limit",
            str(limit),
        ]
    )

    t = type.lower()
    if t == "fork":
        args.append("--fork")
    elif t == "source":
        args.append("--source")
    elif t in ("public", "private", "internal"):
        args.extend(["--visibility", t])

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"repos for {username or 'current user'} ({type})",
    )


@mcp.tool(
    title="Read repository file",
    description=(
        "Read-only: fetch the complete contents and blob metadata for one repository "
        "file at a branch, tag, or commit ref. This tool never modifies GitHub."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_file_contents(
    owner: Annotated[
        str,
        Field(description="GitHub repository owner or organization login.", min_length=1),
    ],
    repo: Annotated[
        str,
        Field(description="GitHub repository name without the owner prefix.", min_length=1),
    ],
    path: Annotated[
        str,
        Field(description="Repository-relative path of the file to read.", min_length=1),
    ],
    ref: Annotated[
        str,
        Field(
            description="Branch, tag, or full commit SHA to read without modifying it.",
            min_length=1,
            max_length=1024,
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> RepositoryFile:
    """Read the complete contents of one repository file at an exact ref."""

    logger.info("MCP tool invocation reached server: tool=gh_get_file_contents")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    validate_repo_path(path)
    if not ref or len(ref) > 1024:
        raise ValueError("ref must be a non-empty Git ref or commit SHA")

    metadata = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
        "-X",
        "GET",
        "-f",
        f"ref={ref}",
    )
    if not isinstance(metadata, dict) or metadata.get("type") == "dir":
        raise ValueError(f"repository path is not a file: {path!r}")
    sha = metadata.get("sha")
    if not isinstance(sha, str) or not OBJECT_SHA_RE.fullmatch(sha):
        raise RuntimeError(f"GitHub did not return a blob SHA for {path!r} at {ref!r}")

    blob = await app.client.run("api", f"repos/{owner}/{repo}/git/blobs/{sha}")
    raw_content = blob.get("content") if isinstance(blob, dict) else None
    if not isinstance(raw_content, str) or blob.get("encoding") != "base64":
        raise RuntimeError(f"GitHub did not return base64 blob content for {path!r}")
    try:
        decoded = base64.b64decode("".join(raw_content.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"GitHub returned invalid base64 content for {path!r}") from exc

    try:
        content = decoded.decode("utf-8")
        encoding: Literal["utf-8", "base64"] = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(decoded).decode("ascii")
        encoding = "base64"

    return RepositoryFile(
        path=path,
        ref=ref,
        sha=sha,
        size=len(decoded),
        content=content,
        encoding=encoding,
    )
