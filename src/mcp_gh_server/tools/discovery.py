"""GitHub repository, issue, and source discovery tools."""

from __future__ import annotations

import json
import shlex

from mcp.server.mcpserver import Context

from ..models import SearchResults
from ..tooling import READ_EXTERNAL, AppContext, app_from_context, mcp, parse_search_result


def _quote_search_component(value: str) -> str:
    """Quote one GitHub Search keyword or qualifier value as gh search does."""

    if any(character in value for character in ' "\t\r\n'):
        return json.dumps(value, ensure_ascii=False)
    return value


def _search_query_from_args(query_args: list[str]) -> str:
    """Recreate the Search REST query generated from gh search argv components."""

    formatted: list[str] = []
    for argument in query_args:
        qualifier, separator, value = argument.partition(":")
        if separator:
            formatted.append(f"{qualifier}:{_quote_search_component(value)}")
        else:
            formatted.append(_quote_search_component(argument))
    return " ".join(formatted)


async def _search_count_evidence(
    app: AppContext,
    endpoint: str,
    query: str,
    returned_items: int,
) -> tuple[int, bool]:
    """Read bounded Search REST count evidence and its completeness state."""

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
    if not isinstance(incomplete_results, bool):
        raise RuntimeError("GitHub Search count evidence has malformed incomplete_results")

    total_count = result.get("total_count")
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        raise RuntimeError("GitHub Search count evidence has malformed total_count")

    count_incomplete = incomplete_results or total_count < returned_items
    return max(total_count, returned_items), count_incomplete


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
    total, count_incomplete = await _search_count_evidence(
        app,
        "search/repositories",
        _search_query_from_args(query_args),
        len(items),
    )
    truncated = count_incomplete or len(items) < total
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
    total, count_incomplete = await _search_count_evidence(
        app,
        "search/issues",
        _search_query_from_args(query_args),
        len(items),
    )
    truncated = count_incomplete or len(items) < total
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
    total, count_incomplete = await _search_count_evidence(
        app,
        "search/code",
        _search_query_from_args(query_args),
        len(items),
    )
    truncated = count_incomplete or len(items) < total
    return SearchResults(total_count=total, items=items, truncated=truncated, query=query)
