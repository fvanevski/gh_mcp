"""Issue, label, milestone, and issue-comment tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import Context

from ..models import CommentCreate, IssueInfo, SearchResults
from ..request_governor import GitHubRequestResult
from ..tooling import READ_EXTERNAL, AppContext, app_from_context, mcp, require_write_enabled
from ..write_contracts import execute_write_readback, run_api_json_write_with_metadata


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_issues(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "all",
    per_page: int | None = None,
    labels: str | None = None,
) -> SearchResults:
    """List issues in a repository.

    state: open, closed, or all (default: all).
    labels: comma-separated label filter.
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "title,url,number,state,author,body,createdAt,updatedAt,closedAt,labels,comments"
    args = [
        "issue",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--state",
        state,
        "--limit",
        str(limit),
    ]
    if labels:
        args.extend(["--labels", labels])

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} ({state})",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_get_issue(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
) -> IssueInfo:
    """Get details of a specific issue or pull request, including its body."""

    app = app_from_context(ctx)
    fields = (
        "title,url,number,state,author,body,createdAt,updatedAt,closedAt,labels,comments,milestone"
    )
    result = await app.client.run(
        "issue",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    author_obj = result.get("author")
    if isinstance(author_obj, dict):
        result["author"] = author_obj.get("login")
    return IssueInfo.model_validate(result)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_labels(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    per_page: int | None = None,
) -> SearchResults:
    """List labels in a repository."""

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "name,color,description,createdAt,updatedAt,url,isDefault"
    result = await app.client.run(
        "label",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--limit",
        str(limit),
    )
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} labels",
    )


@mcp.tool(
    description=(
        "List milestones in a repository via the GitHub API.\n\n"
        "state: open, closed, or all (default: all)."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_milestones(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "all",
    per_page: int | None = None,
) -> SearchResults:
    """List milestones in a repository via the GitHub API."""

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/milestones",
        "-X",
        "GET",
        "-f",
        f"per_page={limit}",
        "-f",
        f"state={state}",
    )
    items: list[Any]
    if isinstance(result, dict) and "stdout" in result:
        raw = result["stdout"]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        items = parsed if isinstance(parsed, list) else []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} milestones ({state})",
    )


@dataclass(frozen=True, slots=True)
class _CommentSnapshot:
    comment_id: int
    api_url: str
    html_url: str
    issue_url: str
    body: str | None


def _comment_snapshot(value: Any) -> _CommentSnapshot:
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned a non-object comment response")

    comment_id = value.get("id")
    api_url = value.get("url")
    html_url = value.get("html_url")
    issue_url = value.get("issue_url")
    raw_body = value.get("body")

    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
        raise RuntimeError("GitHub comment response did not include a positive comment id")
    if not isinstance(api_url, str) or not api_url:
        raise RuntimeError("GitHub comment response did not include an API URL")
    if not isinstance(html_url, str) or not html_url:
        raise RuntimeError("GitHub comment response did not include a canonical HTML URL")
    if not isinstance(issue_url, str) or not issue_url:
        raise RuntimeError("GitHub comment response did not include an issue URL")

    return _CommentSnapshot(
        comment_id=comment_id,
        api_url=api_url,
        html_url=html_url,
        issue_url=issue_url,
        body=raw_body if isinstance(raw_body, str) else None,
    )


def _url_path_endswith(url: str, suffix: str) -> bool:
    parsed = urlsplit(url)
    return bool(parsed.scheme and parsed.netloc) and parsed.path.rstrip("/").casefold().endswith(
        suffix.rstrip("/").casefold()
    )


def _comment_matches_target(
    snapshot: _CommentSnapshot,
    owner: str,
    repo: str,
    issue_number: int,
) -> bool:
    api_suffix = f"/repos/{owner}/{repo}/issues/comments/{snapshot.comment_id}"
    issue_suffix = f"/repos/{owner}/{repo}/issues/{issue_number}"
    html = urlsplit(snapshot.html_url)
    html_issue_suffix = f"/{owner}/{repo}/issues/{issue_number}"
    html_pr_suffix = f"/{owner}/{repo}/pull/{issue_number}"

    return (
        _url_path_endswith(snapshot.api_url, api_suffix)
        and _url_path_endswith(snapshot.issue_url, issue_suffix)
        and bool(html.scheme and html.netloc)
        and (
            html.path.rstrip("/").casefold().endswith(html_issue_suffix.casefold())
            or html.path.rstrip("/").casefold().endswith(html_pr_suffix.casefold())
        )
        and html.fragment.casefold() == f"issuecomment-{snapshot.comment_id}".casefold()
    )


async def gh_create_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    *,
    ctx: Context[AppContext],
) -> CommentCreate:
    """Post one issue or pull-request comment and verify it by immutable comment id."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="comment_create")
    if issue_number < 1:
        raise ValueError("issue_number must be positive")

    created: _CommentSnapshot | None = None

    async def write() -> GitHubRequestResult[Any]:
        nonlocal created
        result = await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/issues/{issue_number}/comments",
            {"body": body},
        )
        try:
            created = _comment_snapshot(result.value)
        except RuntimeError:
            created = None
        return result

    async def readback() -> _CommentSnapshot:
        if created is None:
            raise RuntimeError("Comment creation response did not expose stable comment identity")
        result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/issues/comments/{created.comment_id}",
            "-X",
            "GET",
        )
        return _comment_snapshot(result)

    def state_matches_requested(snapshot: _CommentSnapshot) -> bool:
        return (
            created is not None
            and snapshot.comment_id == created.comment_id
            and snapshot.api_url == created.api_url
            and snapshot.html_url == created.html_url
            and snapshot.issue_url == created.issue_url
            and _comment_matches_target(created, owner, repo, issue_number)
            and _comment_matches_target(snapshot, owner, repo, issue_number)
            and snapshot.body == body
        )

    execution = await execute_write_readback(
        resource=f"comment on {owner}/{repo}#{issue_number}",
        write=write,
        readback=readback,
        state_matches_requested=state_matches_requested,
    )
    outcome = execution.outcome
    comment_id = created.comment_id if created is not None else None
    url = created.html_url if created is not None else ""
    verified = outcome.state_matches_requested is True
    message = (
        "Comment created and verified successfully."
        if verified
        else outcome.warning or "Comment creation was not verified."
    )

    return CommentCreate(
        precondition_checked=outcome.precondition_checked,
        write_completed=outcome.write_completed,
        readback_completed=outcome.readback_completed,
        state_matches_requested=outcome.state_matches_requested,
        warning=outcome.warning,
        request_id=outcome.request_id,
        comment_id=comment_id,
        url=url,
        message=message,
    )
