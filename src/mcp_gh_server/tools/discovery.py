"""GitHub repository, issue, and source discovery tools."""

from __future__ import annotations

import shlex

from mcp.server.mcpserver import Context

from ..models import SearchResults
from ..tooling import AppContext, READ_EXTERNAL, app_from_context, mcp, parse_search_result


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_search_repos(
    query: str,
    *,
    ctx: Context[AppContext],
    sort: str = "stars",
    order: str = "desc",
    per_page: int | None = None,
) -> SearchResults:
    """Search GitHub repositories.

    Supports all GitHub search qualifiers (e.g. 'language:python stars:>1000').
    Use 'is:fork' to exclude forks, 'archived:false' to exclude archived repos.
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "fullName,name,description,stargazersCount,forksCount,language,createdAt,updatedAt,license"
    )
    args = [
        "search",
        "repos",
        "--json",
        fields,
        "--sort",
        sort,
        "--order",
        order,
        "--limit",
        str(limit),
        "--",
    ]
    args.extend(shlex.split(query))
    result = await app.client.run(*args)
    items, total = parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(total_count=total, items=items, truncated=truncated, query=query)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_search_issues(
    query: str,
    *,
    ctx: Context[AppContext],
    sort: str = "updated",
    order: str = "desc",
    per_page: int | None = None,
) -> SearchResults:
    """Search GitHub issues and pull requests.

    Supports all GitHub search qualifiers (e.g. 'is:open label:bug author:user').
    Use 'is:pr' for pull requests only, 'is:issue' for issues only.
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "title,url,number,state,author,createdAt,updatedAt,labels,repository,commentsCount,body"
    )
    args = [
        "search",
        "issues",
        "--json",
        fields,
        "--sort",
        sort,
        "--order",
        order,
        "--limit",
        str(limit),
        "--",
    ]
    args.extend(shlex.split(query))
    result = await app.client.run(*args)
    items, total = parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(total_count=total, items=items, truncated=truncated, query=query)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_search_code(
    query: str,
    *,
    ctx: Context[AppContext],
    per_page: int | None = None,
) -> SearchResults:
    """Search GitHub source code.

    Supports all GitHub code search qualifiers (e.g. 'func name:main language:python').
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "path,repository,sha,url"
    args = [
        "search",
        "code",
        "--json",
        fields,
        "--limit",
        str(limit),
        "--",
    ]
    args.extend(shlex.split(query))
    result = await app.client.run(*args)
    items, total = parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(total_count=total, items=items, truncated=truncated, query=query)
