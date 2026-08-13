"""GitHub repository, issue, and source discovery tools."""

from __future__ import annotations

import shlex

from mcp.server.mcpserver import Context

from ..models import SearchResults
from ..tooling import READ_EXTERNAL, AppContext, app_from_context, mcp, parse_search_result


async def _authoritative_search_total(
    app: AppContext,
    endpoint: str,
    query: str,
    returned_items: int,
) -> int:
    """Read and validate one authoritative bounded Search REST count."""

    result = await app.client.run(
        "api",
        endpoint,
        "-X",
        "GET",
        "-f",
        f"q={query}",
        "-f",
        "per_page=1",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub Search count endpoint returned a non-object response")

    incomplete_results = result.get("incomplete_results")
    if incomplete_results is True:
        raise RuntimeError("GitHub Search count evidence is incomplete")
    if incomplete_results is not False:
        raise RuntimeError("GitHub Search count evidence has malformed incomplete_results")

    total_count = result.get("total_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or total_count < returned_items
    ):
        raise RuntimeError("GitHub Search count evidence has malformed total_count")
    return total_count


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
    query_args = shlex.split(query)
    args.extend(query_args)
    result = await app.client.run(*args)
    items, _ = parse_search_result(result)
    total = await _authoritative_search_total(
        app,
        "search/repositories",
        " ".join(query_args),
        len(items),
    )
    truncated = len(items) < total
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
    query_args = shlex.split(query)
    args.extend(query_args)
    result = await app.client.run(*args)
    items, _ = parse_search_result(result)
    total = await _authoritative_search_total(
        app,
        "search/issues",
        " ".join(query_args),
        len(items),
    )
    truncated = len(items) < total
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
    query_args = shlex.split(query)
    args.extend(query_args)
    result = await app.client.run(*args)
    items, _ = parse_search_result(result)
    total = await _authoritative_search_total(
        app,
        "search/code",
        " ".join(query_args),
        len(items),
    )
    truncated = len(items) < total
    return SearchResults(total_count=total, items=items, truncated=truncated, query=query)
