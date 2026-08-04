"""MCP 2.0 tool registration for GitHub CLI operations."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Elicit, Resolve
from mcp_types import ToolAnnotations

from .gh_client import GhClient
from .models import (
    BranchCreate,
    CommentCreate,
    PullRequestEdit,
    CommandApproval,
    IssueCreate,
    IssueEdit,
    IssueInfo,
    LabelCreate,
    LabelEdit,
    MilestoneCreate,
    PullRequestCreate,
    PullRequestInfo,
    ReleaseCreate,
    ReleaseInfo,
    RepoCreate,
    RepoInfo,
    SearchResults,
    WorkflowInfo,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunWatchResult,
)
from .settings import Settings, get_settings

_READ_ONLY_TOOL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    open_world_hint=False,
)
_WRITE_TOOL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)


@dataclass(slots=True)
class AppContext:
    client: GhClient
    settings: Settings


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    del server
    settings = get_settings()
    client = GhClient(settings=settings)
    try:
        yield AppContext(client=client, settings=settings)
    finally:
        pass  # No persistent resources to clean up


mcp = MCPServer(
    "GitHub CLI",
    instructions=(
        "Interact with GitHub via the ``gh`` CLI. Prefer catalog tools before "
        "writing. Use search tools for discovery. Use write tools (issue, PR, repo, "
        "release creation) only when a GitHub change is necessary; they are disabled "
        "unless explicitly enabled and, by default, require human confirmation through "
        "MCP elicitation."
    ),
    lifespan=app_lifespan,
    version="0.1.0",
)


def _app(ctx: Context[AppContext]) -> AppContext:
    return ctx.request_context.lifespan_context


async def _resolve_write_approval(
    ctx: Context,
) -> CommandApproval | Elicit[CommandApproval]:
    """Resolve write approval outside model-controlled tool arguments."""

    app = ctx.request_context.lifespan_context
    if not isinstance(app, AppContext):
        raise RuntimeError("MCP lifespan context is unavailable")

    if not app.settings.allow_write_commands:
        return CommandApproval(approved=False)
    if not app.settings.confirm_write_commands:
        return CommandApproval(approved=True)

    return Elicit(
        "Execute this GitHub write command? It may create or modify "
        "GitHub resources. Review the tool call carefully before approving.",
        CommandApproval,
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_info(ctx: Context[AppContext]) -> dict[str, Any]:
    """Return gh CLI version, authentication status, and active account."""

    app = _app(ctx)
    # gh auth status uses --json hosts (not version)
    auth_result = app.client.run(
        "auth",
        "status",
        "--json",
        "hosts",
    )
    hosts = auth_result.get("hosts", {})
    # gh --version prints to stdout (not JSON)
    version_result = app.client.run("version", json_output=False)
    version_line = version_result.get("stdout", "") or ""
    version = version_line.strip().split()[2] if len(version_line.strip().split()) > 2 else "unknown"

    # Find the first active host
    active_account: str | None = None
    hostname: str | None = None
    for host, accounts in hosts.items():
        if isinstance(accounts, list):
            for acct in accounts:
                if isinstance(acct, dict) and acct.get("active"):
                    active_account = acct.get("login")
                    hostname = host
                    break
        if active_account:
            break

    return {
        "version": version,
        "authenticated": active_account is not None,
        "active_account": active_account,
        "hostname": hostname,
    }


def _parse_search_result(
    result: Any,
) -> tuple[list[Any], int]:
    """Parse gh search output which may be a list or a dict with 'results' key."""
    if isinstance(result, list):
        return result, 0
    if isinstance(result, dict):
        items = result.get("results", [])
        total = result.get("totalCount", 0)
        if isinstance(items, list):
            return items, total
    return [], 0


@mcp.tool(annotations=_READ_ONLY_TOOL)
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

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "fullName,name,description,stargazersCount,forksCount,language,"
        "createdAt,updatedAt,license"
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
    result = app.client.run(*args)
    items, total = _parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(
        total_count=total,
        items=items,
        truncated=truncated,
        query=query,
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
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

    app = _app(ctx)
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
    result = app.client.run(*args)
    items, total = _parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(
        total_count=total,
        items=items,
        truncated=truncated,
        query=query,
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_search_code(
    query: str,
    *,
    ctx: Context[AppContext],
    per_page: int | None = None,
) -> SearchResults:
    """Search GitHub source code.

    Supports all GitHub code search qualifiers (e.g. 'func name:main language:python').
    """

    app = _app(ctx)
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
    result = app.client.run(*args)
    items, total = _parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(
        total_count=total,
        items=items,
        truncated=truncated,
        query=query,
    )


# ---------------------------------------------------------------------------
# Issue tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
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

    app = _app(ctx)
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

    result = app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} ({state})",
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_get_issue(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
) -> IssueInfo:
    """Get details of a specific issue or pull request."""

    app = _app(ctx)
    fields = (
        "title,url,number,state,author,body,createdAt,updatedAt,closedAt,labels,comments,milestone"
    )
    result = app.client.run(
        "issue",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    # gh returns author as an object with login; normalize to string
    author_obj = result.get("author")
    if isinstance(author_obj, dict):
        result["author"] = author_obj.get("login")
    return IssueInfo.model_validate(result)


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    *,
    ctx: Context[AppContext],
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> IssueCreate:
    """Create a new issue in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return IssueCreate(
            number=0,
            title=title,
            url="",
            message="Issue creation cancelled; no GitHub issue was created.",
        )

    args = [
        "issue",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
    ]
    if body:
        args.extend(["--body", body])
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if assignees:
        for assignee in assignees:
            args.extend(["--assignee", assignee])

    result = app.client.run(*args, "--json", "title,number,url")
    return IssueCreate(
        number=result.get("number", 0),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Issue created successfully.",
    )


# ---------------------------------------------------------------------------
# Pull request tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_list_prs(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "open",
    per_page: int | None = None,
) -> SearchResults:
    """List pull requests in a repository.

    state: open, closed, or all (default: open).
    """

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "title,url,number,state,author,body,createdAt,updatedAt,closedAt,"
        "labels,comments,headRefName,baseRefName,isDraft,"
        "additions,deletions,changedFiles"
    )
    result = app.client.run(
        "pr",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--state",
        state,
        "--limit",
        str(limit),
    )
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} PRs ({state})",
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_get_pr(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
) -> PullRequestInfo:
    """Get details of a specific pull request."""

    app = _app(ctx)
    fields = (
        "title,url,number,state,author,body,createdAt,updatedAt,closedAt,"
        "labels,comments,headRefName,baseRefName,isDraft,"
        "additions,deletions,changedFiles"
    )
    result = app.client.run(
        "pr",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    # gh returns author as an object with login; normalize to string
    author_obj = result.get("author")
    if isinstance(author_obj, dict):
        result["author"] = author_obj.get("login")
    return PullRequestInfo.model_validate(result)


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_create_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    *,
    ctx: Context[AppContext],
    draft: bool = False,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    review_users: list[str] | None = None,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> PullRequestCreate:
    """Create a new pull request in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return PullRequestCreate(
            number=0,
            title=title,
            url="",
            message="Pull request creation cancelled; no GitHub PR was created.",
        )

    args = [
        "pr",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
        "--body",
        body,
        "--head",
        head,
        "--base",
        base,
    ]
    if draft:
        args.append("--draft")
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if assignees:
        for assignee in assignees:
            args.extend(["--assignee", assignee])
    if review_users:
        for user in review_users:
            args.extend(["--reviewer", user])

    result = app.client.run(*args, "--json", "title,number,url")
    return PullRequestCreate(
        number=result.get("number", 0),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Pull request created successfully.",
    )


# ---------------------------------------------------------------------------
# Repository tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_get_repo(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
) -> RepoInfo:
    """Get details of a specific repository."""

    app = _app(ctx)
    # gh repo view uses positional repo arg, not --repo
    # Field names: nameWithOwner (not fullName), stargazerCount (not stargazers),
    #              forkCount (not forks)
    fields = (
        "nameWithOwner,name,owner,description,url,isPrivate,isFork,primaryLanguage,"
        "stargazerCount,forkCount,createdAt,pushedAt,defaultBranchRef,licenseInfo"
    )
    result = app.client.run(
        "repo",
        "view",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    # gh returns owner and primaryLanguage as objects; normalize to strings
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


@mcp.tool(annotations=_READ_ONLY_TOOL)
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

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "nameWithOwner,name,description,stargazerCount,forkCount,primaryLanguage,"
        "createdAt,pushedAt,defaultBranchRef"
    )
    args = ["repo", "list"]
    if username:
        args.append(username)
    args.extend([
        "--json",
        fields,
        "--limit",
        str(limit),
    ])
    
    # Translate type
    t = type.lower()
    if t == "fork":
        args.append("--fork")
    elif t == "source":
        args.append("--source")
    elif t in ("public", "private", "internal"):
        args.extend(["--visibility", t])

    result = app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"repos for {username or 'current user'} ({type})",
    )


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_create_repo(
    name: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    private: bool = False,
    auto_init: bool = False,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> RepoCreate:
    """Create a new repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return RepoCreate(
            full_name=name,
            url="",
            message="Repository creation cancelled; no GitHub repo was created.",
        )

    args = [
        "repo",
        "create",
        name,
        "--json",
        "nameWithOwner,url",
    ]
    if description:
        args.extend(["--description", description])
    if private:
        args.append("--private")
    if auto_init:
        args.append("--enable-gitignore")
        args.append("--enable-wiki")

    result = app.client.run(*args)
    return RepoCreate(
        full_name=result.get("nameWithOwner", name),
        url=result.get("url", ""),
        message="Repository created successfully.",
    )


# ---------------------------------------------------------------------------
# Release tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_list_releases(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    per_page: int | None = None,
) -> SearchResults:
    """List releases in a repository."""

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "tagName,name,isDraft,isPrerelease,createdAt,publishedAt"
    result = app.client.run(
        "release",
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
        query=f"{owner}/{repo} releases",
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_get_release(
    owner: str,
    repo: str,
    tag: str,
    *,
    ctx: Context[AppContext],
) -> ReleaseInfo:
    """Get details of a specific release."""

    app = _app(ctx)
    fields = "tagName,name,url,isDraft,isPrerelease,createdAt,publishedAt"
    result = app.client.run(
        "release",
        "view",
        tag,
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    return ReleaseInfo.model_validate(result)


@mcp.tool(annotations=_WRITE_TOOL)
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
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> ReleaseCreate:
    """Create a new release in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return ReleaseCreate(
            tag_name=tag_name,
            url="",
            message="Release creation cancelled; no GitHub release was created.",
        )

    args = [
        "release",
        "create",
        tag_name,
        "--repo",
        f"{owner}/{repo}",
        "--json",
        "tagName,url",
    ]
    if name:
        args.extend(["--title", name])
    if body:
        args.extend(["--notes", body])
    if draft:
        args.append("--draft")
    if prerelease:
        args.append("--prerelease")
    if target:
        args.extend(["--target", target])

    result = app.client.run(*args)
    return ReleaseCreate(
        tag_name=result.get("tagName", tag_name),
        url=result.get("url", ""),
        message="Release created successfully.",
    )


# ---------------------------------------------------------------------------
# Workflow tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_list_workflows(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "active",
    per_page: int | None = None,
) -> SearchResults:
    """List GitHub Actions workflows in a repository.

    state: active, all, disabled, disabled_inactivity, disabled_fork.
    """

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "id,name,path,state"
    args = [
        "workflow",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--limit",
        str(limit),
    ]
    if state != "active":
        args.extend(["--all"])

    result = app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} workflows ({state})",
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_get_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    *,
    ctx: Context[AppContext],
) -> WorkflowInfo:
    """Get details of a specific GitHub Actions workflow."""

    app = _app(ctx)
    fields = "id,name,path,state"
    result = app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/workflows/{workflow_id}",
    )
    return WorkflowInfo.model_validate(result)


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_run_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    ref: str = "main",
    *,
    ctx: Context[AppContext],
    fields: list[str] | None = None,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> WorkflowRunCreate:
    """Trigger a workflow dispatch event for a GitHub Actions workflow.

    The workflow must support an `on.workflow_dispatch` trigger.
    Use `fields` to pass inputs as key=value pairs (e.g. ["key=value"]).
    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true.
    """

    app = _app(ctx)
    if not approval.approved:
        return WorkflowRunCreate(
            run_id=None,
            url="",
            message="Workflow dispatch cancelled; no GitHub Actions run was created.",
        )

    args = [
        "workflow",
        "run",
        str(workflow_id),
        "--repo",
        f"{owner}/{repo}",
        "--ref",
        ref,
        "--json",
        "databaseId,url",
    ]
    if fields:
        for field in fields:
            args.extend(["-f", field])

    result = app.client.run(*args)
    return WorkflowRunCreate(
        run_id=result.get("databaseId"),
        url=result.get("url"),
        message="Workflow dispatch triggered successfully.",
    )


# ---------------------------------------------------------------------------
# Run tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_list_runs(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    branch: str | None = None,
    status: str | None = None,
    per_page: int | None = None,
) -> SearchResults:
    """List recent GitHub Actions workflow runs.

    status: completed, in_progress, queued, pending, requested, waiting,
            actionable, null (all).
    """

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "databaseId,name,displayTitle,headBranch,headSha,conclusion,status,"
        "event,url,createdAt,updatedAt,startedAt,workflowName"
    )
    args = [
        "run",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--limit",
        str(limit),
    ]
    if branch:
        args.extend(["--branch", branch])
    if status:
        args.extend(["--status", status])

    result = app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} runs",
    )


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_get_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    ctx: Context[AppContext],
) -> WorkflowRun:
    """Get details of a specific GitHub Actions workflow run."""

    app = _app(ctx)
    fields = (
        "databaseId,name,displayTitle,headBranch,headSha,conclusion,status,"
        "event,url,createdAt,updatedAt,startedAt,workflowName"
    )
    result = app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    return WorkflowRun.model_validate(result)


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_watch_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    ctx: Context[AppContext],
    interval: int = 10,
    compact: bool = False,
    exit_status: bool = False,
) -> WorkflowRunWatchResult:
    """Watch a GitHub Actions workflow run until completion.

    This is a blocking call that waits for the run to finish.
    Uses gh run watch with --interval, --compact, and --exit-status flags.
    """

    app = _app(ctx)
    args = [
        "run",
        "watch",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--interval",
        str(interval),
    ]
    if compact:
        args.append("--compact")
    if exit_status:
        args.append("--exit-status")

    # gh run watch is a blocking command that outputs to stdout/stderr
    result = app.client.run(*args, json_output=False)

    # After watch completes, fetch final status
    view_args = [
        "run",
        "view",
        str(run_id),
        "--json",
        "status,conclusion,url",
    ]
    if owner and repo:
        view_args.extend(["--repo", f"{owner}/{repo}"])
    
    view_result = app.client.run(*view_args)
    if isinstance(view_result, dict):
        conclusion = view_result.get("conclusion", "unknown")
        return WorkflowRunWatchResult(
            run_id=run_id,
            conclusion=conclusion,
            status=view_result.get("status"),
            url=view_result.get("url"),
            message=f"Run #{run_id} completed with conclusion: {conclusion}",
        )

    return WorkflowRunWatchResult(
        run_id=run_id,
        message="Watch completed (unable to parse final status)",
    )


# ---------------------------------------------------------------------------
# Issue edit tool
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_edit_issue(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
    title: str | None = None,
    body: str | None = None,
    labels_add: list[str] | None = None,
    labels_remove: list[str] | None = None,
    assignees_add: list[str] | None = None,
    assignees_remove: list[str] | None = None,
    milestone: int | None = None,
    remove_milestone: bool = False,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> IssueEdit:
    """Edit an existing issue in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return IssueEdit(
            number=number,
            title="",
            state="",
            url="",
            message="Issue edit cancelled; no changes were made.",
        )

    args = [
        "issue",
        "edit",
        str(number),
        "--repo",
        f"{owner}/{repo}",
    ]
    if title:
        args.extend(["--title", title])
    if body:
        args.extend(["--body", body])
    if labels_add:
        for label in labels_add:
            args.extend(["--add-label", label])
    if labels_remove:
        for label in labels_remove:
            args.extend(["--remove-label", label])
    if assignees_add:
        for assignee in assignees_add:
            args.extend(["--add-assignee", assignee])
    if assignees_remove:
        for assignee in assignees_remove:
            args.extend(["--remove-assignee", assignee])
    if milestone is not None:
        args.extend(["--milestone", str(milestone)])
    if remove_milestone:
        args.append("--remove-milestone")

    result = app.client.run(*args, "--json", "title,number,state,url")
    return IssueEdit(
        number=result.get("number", number),
        title=result.get("title", ""),
        state=result.get("state", ""),
        url=result.get("url", ""),
        message="Issue edited successfully.",
    )


# ---------------------------------------------------------------------------
# Label tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_list_labels(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    per_page: int | None = None,
) -> SearchResults:
    """List labels in a repository."""

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "name,color,description,createdAt,updatedAt,url,isDefault"
    result = app.client.run(
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


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_create_label(
    owner: str,
    repo: str,
    name: str,
    color: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    force: bool = False,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> LabelCreate:
    """Create a new label in a repository.

    color: 6-character hex color code (e.g. 'ff0000' for red).
    force: overwrite existing label's color and description if it exists.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return LabelCreate(
            name=name,
            color=color,
            description=description,
            url="",
            message="Label creation cancelled; no GitHub label was created.",
        )

    args = [
        "label",
        "create",
        name,
        "--repo",
        f"{owner}/{repo}",
        "--color",
        color,
    ]
    if description:
        args.extend(["--description", description])
    if force:
        args.append("--force")

    result = app.client.run(*args, "--json", "name,color,description,url")
    return LabelCreate(
        name=result.get("name", name),
        color=result.get("color", color),
        description=result.get("description", description),
        url=result.get("url", ""),
        message="Label created successfully.",
    )


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_edit_label(
    owner: str,
    repo: str,
    name: str,
    *,
    ctx: Context[AppContext],
    new_name: str | None = None,
    color: str | None = None,
    description: str | None = None,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> LabelEdit:
    """Edit an existing label in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return LabelEdit(
            name=name,
            color="",
            description="",
            url="",
            message="Label edit cancelled; no GitHub label was edited.",
        )

    args = [
        "label",
        "edit",
        name,
        "--repo",
        f"{owner}/{repo}",
    ]
    if new_name:
        args.extend(["--new-name", new_name])
    if color:
        args.extend(["--color", color])
    if description:
        args.extend(["--description", description])

    result = app.client.run(*args, "--json", "name,color,description,url")
    return LabelEdit(
        name=result.get("name", name),
        color=result.get("color", ""),
        description=result.get("description", ""),
        url=result.get("url", ""),
        message="Label edited successfully.",
    )


# ---------------------------------------------------------------------------
# Milestone tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_TOOL)
async def gh_list_milestones(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "all",
    per_page: int | None = None,
) -> SearchResults:
    """List milestones in a repository via the GitHub API.

    state: open, closed, or all (default: all).
    """

    app = _app(ctx)
    limit = app.client.clamp_max_results(per_page)
    result = app.client.run(
        "api",
        f"repos/{owner}/{repo}/milestones",
        "-f",
        f"per_page={limit}",
        "-f",
        f"state={state}",
    )
    # gh api returns the raw JSON response
    if isinstance(result, dict) and "stdout" in result:
        raw = result["stdout"]
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            items = []
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


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_create_milestone(
    owner: str,
    repo: str,
    title: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    due_on: str | None = None,
    state: str = "open",
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> MilestoneCreate:
    """Create a new milestone in a repository via the GitHub API.

    due_on: due date in ISO format (e.g. '2026-12-31').
    state: open or closed (default: open).

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. With the
    default MCP_GH_CONFIRM_WRITE_COMMANDS=true, MCP asks a human to approve
    the command before executing.
    """

    app = _app(ctx)
    if not approval.approved:
        return MilestoneCreate(
            number=0,
            title=title,
            url="",
            message="Milestone creation cancelled; no GitHub milestone was created.",
        )

    payload: dict[str, Any] = {"title": title, "state": state}
    if description:
        payload["description"] = description
    if due_on:
        payload["due_on"] = due_on

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        f.flush()
        payload_path = f.name

    try:
        result = app.client.run(
            "api",
            f"repos/{owner}/{repo}/milestones",
            "-X",
            "POST",
            "-i",
            payload_path,
            "--jq",
            "{number,title,url}",
        )
    finally:
        os.unlink(payload_path)

    return MilestoneCreate(
        number=result.get("number", 0),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Milestone created successfully.",
    )


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Comment / Branch / Edit PR Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_WRITE_TOOL)
async def gh_create_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    *,
    ctx: Context[AppContext],
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> CommentCreate:
    """Post a comment on an issue or pull request."""
    app = _app(ctx)
    if not approval.approved:
        return CommentCreate(url="", message="Comment creation cancelled.")

    result = app.client.run(
        "issue",
        "comment",
        str(issue_number),
        "--repo",
        f"{owner}/{repo}",
        "--body",
        body,
    )
    # gh issue comment outputs the URL of the created comment to stdout
    stdout = result.get("stdout", "") if isinstance(result, dict) else str(result)
    return CommentCreate(
        url=stdout.strip() if stdout else "",
        message="Comment posted successfully.",
    )


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_create_branch(
    owner: str,
    repo: str,
    issue_number: int,
    name: str,
    *,
    ctx: Context[AppContext],
    base: str | None = None,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> BranchCreate:
    """Create a new branch for an issue."""
    app = _app(ctx)
    if not approval.approved:
        return BranchCreate(name=name, message="Branch creation cancelled.")

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
        
    app.client.run(*args)
    return BranchCreate(
        name=name,
        message=f"Branch '{name}' created successfully for issue #{issue_number}.",
    )


@mcp.tool(annotations=_WRITE_TOOL)
async def gh_edit_pr(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
    title: str | None = None,
    body: str | None = None,
    labels_add: list[str] | None = None,
    labels_remove: list[str] | None = None,
    assignees_add: list[str] | None = None,
    assignees_remove: list[str] | None = None,
    base: str | None = None,
    approval: Annotated[CommandApproval, Resolve(_resolve_write_approval)],
) -> PullRequestEdit:
    """Edit an existing pull request."""
    app = _app(ctx)
    if not approval.approved:
        return PullRequestEdit(
            number=number,
            title="",
            url="",
            message="PR edit cancelled; no changes were made.",
        )

    args = [
        "pr",
        "edit",
        str(number),
        "--repo",
        f"{owner}/{repo}",
    ]
    if title:
        args.extend(["--title", title])
    if body:
        args.extend(["--body", body])
    if labels_add:
        for label in labels_add:
            args.extend(["--add-label", label])
    if labels_remove:
        for label in labels_remove:
            args.extend(["--remove-label", label])
    if assignees_add:
        for assignee in assignees_add:
            args.extend(["--add-assignee", assignee])
    if assignees_remove:
        for assignee in assignees_remove:
            args.extend(["--remove-assignee", assignee])
    if base:
        args.extend(["--base", base])

    app.client.run(*args)
    
    # Fetch updated details
    fields = "title,url"
    info_result = app.client.run(
        "pr",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    
    return PullRequestEdit(
        number=number,
        title=info_result.get("title", ""),
        url=info_result.get("url", ""),
        message="Pull request updated successfully.",
    )
