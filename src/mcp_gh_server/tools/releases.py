"""GitHub release tools."""

from __future__ import annotations

from mcp.server.mcpserver import Context

from ..models import ReleaseCreate, ReleaseInfo, SearchResults
from ..tooling import (
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    mcp,
    readback_warning,
    require_write_enabled,
)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_releases(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    per_page: int | None = None,
) -> SearchResults:
    """List releases in a repository."""

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "tagName,name,isDraft,isPrerelease,createdAt,publishedAt"
    result = await app.client.run(
        "release",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--limit",
        str(limit),
    )
    items = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} releases",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_get_release(
    owner: str,
    repo: str,
    tag: str,
    *,
    ctx: Context[AppContext],
) -> ReleaseInfo:
    """Get details of a specific release."""

    app = app_from_context(ctx)
    fields = "tagName,name,url,isDraft,isPrerelease,createdAt,publishedAt"
    result = await app.client.run(
        "release",
        "view",
        tag,
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    return ReleaseInfo.model_validate(result)


async def gh_create_release(
    owner: str,
    repo: str,
    tag_name: str,
    *,
    ctx: Context[AppContext],
    name: str | None = None,
    body: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
    target: str | None = None,
) -> ReleaseCreate:
    """Create a new release in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="release_create")
    args = [
        "release",
        "create",
        tag_name,
        "--repo",
        f"{owner}/{repo}",
        "--notes-file",
        "-",
    ]
    if name:
        args.extend(["--title", name])
    if draft:
        args.append("--draft")
    if prerelease:
        args.append("--prerelease")
    if target:
        args.extend(["--target", target])

    await app.client.run(*args, json_output=False, stdin_text=body or "")
    try:
        result = await app.client.run(
            "release",
            "view",
            tag_name,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "tagName,url",
        )
    except RuntimeError:
        warning = readback_warning("Release", tag_name)
        return ReleaseCreate(
            tag_name=tag_name,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return ReleaseCreate(
        tag_name=result.get("tagName", tag_name),
        url=result.get("url", ""),
        message="Release created successfully.",
    )
