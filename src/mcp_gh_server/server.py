"""MCP 2.0 tool registration for GitHub CLI operations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shlex
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__
from .gh_client import GhClient
from .models import (
    BranchCreate,
    CommentCreate,
    CommitFile,
    CommitFilesResult,
    IssueCreate,
    IssueEdit,
    IssueInfo,
    LabelCreate,
    LabelEdit,
    MilestoneCreate,
    PullRequestCheck,
    PullRequestChecks,
    PullRequestCommit,
    PullRequestCommitsPage,
    PullRequestCreate,
    PullRequestDiff,
    PullRequestEdit,
    PullRequestFile,
    PullRequestFilesPage,
    PullRequestInfo,
    PullRequestMerge,
    PullRequestReviewSubmission,
    ReleaseCreate,
    ReleaseInfo,
    RepoCreate,
    RepoInfo,
    RepositoryFile,
    SearchResults,
    ServerInfo,
    WorkflowInfo,
    WorkflowJob,
    WorkflowJobsPage,
    WorkflowJobStep,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunFailedLogs,
    WorkflowRunWatchResult,
)
from .settings import Settings, get_settings

_READ_EXTERNAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
_READ_LOCAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_ADD_EXTERNAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
_MUTATE_EXTERNAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_OBJECT_SHA_RE = re.compile(r"^[0-9A-Fa-f]{40}$")

logger = logging.getLogger(__name__)


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
        "writing. Use search tools for discovery. Read complete files before changing "
        "them. Use write tools (including atomic content commits) only when a GitHub "
        "change is necessary; they are disabled "
        "unless explicitly enabled. Approval is handled by the MCP host; the server "
        "also enforces deployment repository and operation policy."
    ),
    lifespan=app_lifespan,
    version=__version__,
)


def _app(ctx: Context[AppContext]) -> AppContext:
    return ctx.request_context.lifespan_context


def _configured_values(raw: str) -> set[str]:
    return {value.strip().casefold() for value in raw.split(",") if value.strip()}


def _validate_repository(owner: str, repo: str) -> None:
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repo) or repo in {".", ".."}:
        raise ValueError("owner and repo must be canonical GitHub names without path separators")


def _require_write_enabled(
    app: AppContext,
    owner: str,
    repo: str,
    *,
    action: str,
) -> None:
    """Enforce server-side write, repository, and high-risk action policy."""

    _validate_repository(owner, repo)
    _require_action_enabled(app, action)

    allowed_repositories = _configured_values(app.settings.allowed_repositories)
    allowed_owners = _configured_values(app.settings.allowed_owners)
    target = f"{owner}/{repo}".casefold()
    if (allowed_repositories or allowed_owners) and (
        target not in allowed_repositories and owner.casefold() not in allowed_owners
    ):
        raise RuntimeError(f"GitHub writes are not allowed for repository {owner}/{repo}")


def _require_action_enabled(app: AppContext, action: str) -> None:
    """Enforce global and high-risk operation switches before target discovery."""

    if not app.settings.allow_write_commands:
        raise RuntimeError("GitHub writes are disabled by MCP_GH_ALLOW_WRITE_COMMANDS")

    action_settings = {
        "repo_create": app.settings.allow_repo_creation,
        "release_create": app.settings.allow_release_creation,
        "workflow_dispatch": app.settings.allow_workflow_dispatch,
        "content_commit": app.settings.allow_content_commits,
        "pr_merge": app.settings.allow_pr_merge,
    }
    if action in action_settings and not action_settings[action]:
        env_name = {
            "repo_create": "MCP_GH_ALLOW_REPO_CREATION",
            "release_create": "MCP_GH_ALLOW_RELEASE_CREATION",
            "workflow_dispatch": "MCP_GH_ALLOW_WORKFLOW_DISPATCH",
            "content_commit": "MCP_GH_ALLOW_CONTENT_COMMITS",
            "pr_merge": "MCP_GH_ALLOW_PR_MERGE",
        }[action]
        raise RuntimeError(f"GitHub action {action!r} is disabled by {env_name}")


@mcp.tool(
    title="Get MCP server version",
    description=(
        "Read-only local diagnostic: return this MCP server's deployed version, tool-schema "
        "version, transport, tool count, and write-policy status. This tool does not call "
        "GitHub, spawn a subprocess, request approval, or modify any state."
    ),
    annotations=_READ_LOCAL,
)
async def gh_server_info(ctx: Context[AppContext]) -> ServerInfo:
    """Return deterministic local deployment metadata without contacting GitHub."""

    logger.info("MCP tool invocation reached server: tool=gh_server_info")
    app = _app(ctx)
    return ServerInfo(
        server_version=__version__,
        tool_schema_version=__version__,
        transport=app.settings.transport,
        tool_count=len(await mcp.list_tools()),
        write_commands_enabled=app.settings.allow_write_commands,
        content_commits_enabled=app.settings.allow_content_commits,
        pr_merge_enabled=app.settings.allow_pr_merge,
    )


@mcp.tool(annotations=_READ_EXTERNAL)
async def gh_info(ctx: Context[AppContext]) -> dict[str, Any]:
    """Return gh CLI version, authentication status, and active account."""

    app = _app(ctx)
    # gh auth status uses --json hosts (not version)
    auth_result = await app.client.run(
        "auth",
        "status",
        "--json",
        "hosts",
    )
    hosts = auth_result.get("hosts", {})
    # gh --version prints to stdout (not JSON)
    version_result = await app.client.run("version", json_output=False)
    version_line = version_result.get("stdout", "") or ""
    version = (
        version_line.strip().split()[2] if len(version_line.strip().split()) > 2 else "unknown"
    )

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


def _write_stdout(result: Any, resource: str) -> str:
    """Return non-empty stdout from a successful non-JSON write command."""

    if isinstance(result, dict):
        stdout = result.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            return stdout.strip()
    raise RuntimeError(
        f"{resource} was created, but gh did not return the locator needed to read it back"
    )


def _created_url(result: Any, resource: str) -> str:
    """Extract and validate the URL printed by a successful gh create command."""

    stdout = _write_stdout(result, resource)
    for token in reversed(stdout.split()):
        url = token.rstrip(".,;)")
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
    raise RuntimeError(f"{resource} was created, but gh returned no valid resource URL")


def _optional_created_url(result: Any) -> str | None:
    try:
        return _created_url(result, "Resource")
    except RuntimeError:
        return None


def _trailing_number(url: str | None) -> int:
    if not url:
        return 0
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return 0


def _readback_warning(resource: str, locator: str | None = None) -> str:
    location = f" at {locator}" if locator else ""
    return (
        f"{resource} write completed{location}, but structured readback failed. "
        "Do not retry automatically; verify the resource first."
    )


def _created_json(result: Any, resource: str) -> dict[str, Any]:
    """Parse the response body from a successful gh api write command."""

    raw = _write_stdout(result, resource)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{resource} was created, but gh returned a non-JSON API response"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{resource} was created, but gh returned an unexpected API response")
    return parsed


async def _get_label(client: GhClient, owner: str, repo: str, name: str) -> dict[str, Any]:
    """Read a label after a write using its exact REST resource path."""

    result = await client.run(
        "api",
        f"repos/{owner}/{repo}/labels/{quote(name, safe='')}",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"gh returned an unexpected response while reading label {name!r}")
    return result


def _validate_repo_path(path: str) -> None:
    parts = path.split("/")
    if (
        not path
        or len(path.encode()) > 4096
        or path.startswith("/")
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
    ):
        raise ValueError(f"invalid repository-relative file path: {path!r}")


def _validate_branch(branch: str) -> None:
    invalid = re.search(r"[\x00-\x20~^:?*\[]", branch)
    if (
        not branch
        or len(branch.encode()) > 1024
        or branch == "@"
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".")
        or branch.endswith(".lock")
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or "\\" in branch
        or invalid is not None
    ):
        raise ValueError("branch is not a valid Git branch name")


async def _api_json_write(
    client: GhClient,
    method: str,
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send a JSON GitHub API write without exposing its body in argv or logs."""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as file:
        json.dump(payload, file)
        file.flush()
        payload_path = file.name
    try:
        result = await client.run(
            "api",
            endpoint,
            "-X",
            method,
            "--input",
            payload_path,
        )
    finally:
        os.unlink(payload_path)
    if not isinstance(result, dict):
        raise RuntimeError(f"GitHub API {method} {endpoint} returned a non-object response")
    return result


def _workflow_run_id(url: str) -> int:
    """Extract a workflow run identifier from its URL."""

    parts = [part for part in urlparse(url).path.split("/") if part]
    try:
        runs_index = parts.index("runs")
        return int(parts[runs_index + 1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"Workflow was dispatched, but its run URL is malformed: {url!r}"
        ) from exc


@mcp.tool(annotations=_READ_EXTERNAL)
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
    items, total = _parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(
        total_count=total,
        items=items,
        truncated=truncated,
        query=query,
    )


@mcp.tool(annotations=_READ_EXTERNAL)
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
    result = await app.client.run(*args)
    items, total = _parse_search_result(result)
    truncated = len(items) >= limit
    return SearchResults(
        total_count=total,
        items=items,
        truncated=truncated,
        query=query,
    )


@mcp.tool(annotations=_READ_EXTERNAL)
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
    result = await app.client.run(*args)
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


@mcp.tool(annotations=_READ_EXTERNAL)
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

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} ({state})",
    )


@mcp.tool(annotations=_READ_EXTERNAL)
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
    result = await app.client.run(
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


@mcp.tool(annotations=_ADD_EXTERNAL)
async def gh_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    *,
    ctx: Context[AppContext],
) -> IssueCreate:
    """Create a new issue in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="issue_create")

    args = [
        "issue",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
        "--body-file",
        "-",
    ]
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if assignees:
        for assignee in assignees:
            args.extend(["--assignee", assignee])

    create_result = await app.client.run(*args, json_output=False, stdin_text=body or "")
    created_url = _optional_created_url(create_result)
    if created_url is None:
        warning = _readback_warning("Issue")
        return IssueCreate(
            number=0,
            title=title,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    try:
        result = await app.client.run(
            "issue",
            "view",
            created_url,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,number,url",
        )
    except RuntimeError:
        warning = _readback_warning("Issue", created_url)
        return IssueCreate(
            number=_trailing_number(created_url),
            title=title,
            url=created_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return IssueCreate(
        number=result.get("number", 0),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Issue created successfully.",
    )


# ---------------------------------------------------------------------------
# Pull request tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_EXTERNAL)
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
        "headRefOid,baseRefOid,additions,deletions,changedFiles"
    )
    result = await app.client.run(
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


@mcp.tool(
    title="Get pull request snapshot",
    description=(
        "Read-only: return bounded metadata and exact base/head commit SHAs for one "
        "GitHub pull request. Performs one noninteractive GET request and cannot create "
        "comments, submit reviews, merge the pull request, request approval, or modify "
        "GitHub state."
    ),
    annotations=_READ_EXTERNAL,
)
async def gh_get_pr(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    number: Annotated[int, Field(ge=1, description="Positive pull request number.")],
    *,
    ctx: Context[AppContext],
) -> PullRequestInfo:
    """Return a bounded, fully typed snapshot for one pull request."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr")
    app = _app(ctx)
    _validate_repository(owner, repo)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}",
        "-X",
        "GET",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return pull-request metadata")

    base = result.get("base")
    head = result.get("head")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(base_sha, str) or not _OBJECT_SHA_RE.fullmatch(base_sha):
        raise RuntimeError("GitHub did not return a valid pull-request base SHA")
    if not isinstance(head_sha, str) or not _OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub did not return a valid pull-request head SHA")

    labels = result.get("labels")
    label_names = (
        [
            label["name"]
            for label in labels
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        ]
        if isinstance(labels, list)
        else []
    )
    issue_comments = result.get("comments")
    review_comments = result.get("review_comments")
    comment_count = (
        issue_comments if isinstance(issue_comments, int) and issue_comments >= 0 else 0
    ) + (review_comments if isinstance(review_comments, int) and review_comments >= 0 else 0)
    user = result.get("user")

    return PullRequestInfo(
        number=number,
        title=str(result.get("title") or ""),
        state=str(result.get("state") or "unknown"),
        author=user.get("login") if isinstance(user, dict) else None,
        createdAt=result.get("created_at"),
        updatedAt=result.get("updated_at"),
        closedAt=result.get("closed_at"),
        labels=label_names,
        comments=comment_count,
        url=str(result.get("html_url") or ""),
        headRefName=head.get("ref") if isinstance(head, dict) else None,
        baseRefName=base.get("ref") if isinstance(base, dict) else None,
        headRefOid=head_sha,
        baseRefOid=base_sha,
        isDraft=bool(result.get("draft", False)),
        additions=int(result.get("additions") or 0),
        deletions=int(result.get("deletions") or 0),
        changedFiles=int(result.get("changed_files") or 0),
    )


async def _get_pr_metadata(app: AppContext, owner: str, repo: str, number: int) -> dict[str, Any]:
    """Read one pull-request metadata object through an explicit GET."""

    metadata = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}",
        "-X",
        "GET",
    )
    if not isinstance(metadata, dict):
        raise RuntimeError("GitHub did not return pull-request metadata")
    return metadata


def _extract_pr_shas(metadata: dict[str, Any]) -> tuple[str, str]:
    """Validate immutable base and head object IDs from pull-request metadata."""

    base = metadata.get("base")
    head = metadata.get("head")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(base_sha, str) or not _OBJECT_SHA_RE.fullmatch(base_sha):
        raise RuntimeError("GitHub did not return a valid pull-request base SHA")
    if not isinstance(head_sha, str) or not _OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub did not return a valid pull-request head SHA")
    return base_sha, head_sha


async def _get_pr_shas(app: AppContext, owner: str, repo: str, number: int) -> tuple[str, str]:
    """Resolve and validate the immutable base and head object IDs for a PR."""

    return _extract_pr_shas(await _get_pr_metadata(app, owner, repo, number))


async def _verify_pr_shas(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
    expected: tuple[str, str],
) -> None:
    """Reject a numbered-PR read if its snapshot changed during the request."""

    if await _get_pr_shas(app, owner, repo, number) != expected:
        raise RuntimeError(
            "Pull request base or head changed during the read; retry from a fresh snapshot"
        )


def _bounded_utf8(content: str, limit: int) -> tuple[str, int, int, bool, str]:
    """Bound text at a complete UTF-8 code point and fingerprint the full response."""

    encoded = content.encode("utf-8")
    total_bytes = len(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    if total_bytes <= limit:
        return content, total_bytes, total_bytes, False, digest
    bounded = encoded[:limit].decode("utf-8", errors="ignore")
    returned_bytes = len(bounded.encode("utf-8"))
    return bounded, returned_bytes, total_bytes, True, digest


@mcp.tool(
    title="Read pull request diff",
    description=(
        "Read-only: return a bounded unified diff or patch for the exact immutable base "
        "and head commit SHAs currently identified by a pull request. The result reports "
        "truncation, byte counts, and a SHA-256 fingerprint. This tool never checks out code, "
        "runs tests, requests approval, or modifies GitHub."
    ),
    annotations=_READ_EXTERNAL,
)
async def gh_get_pr_diff(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    *,
    ctx: Context[AppContext],
    format: Annotated[
        Literal["diff", "patch"],
        Field(description="Unified diff or email-style patch output."),
    ] = "diff",
    max_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description=(
                "Maximum UTF-8 bytes returned, capped by MCP_GH_MAX_PR_DIFF_BYTES. "
                "Omit to use the server cap."
            ),
        ),
    ] = None,
) -> PullRequestDiff:
    """Return a bounded diff for the immutable object IDs resolved from a PR."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr_diff")
    app = _app(ctx)
    _validate_repository(owner, repo)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    accept = (
        "application/vnd.github.v3.diff" if format == "diff" else "application/vnd.github.v3.patch"
    )
    response = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        "-X",
        "GET",
        "-H",
        f"Accept: {accept}",
        json_output=False,
    )
    content = response.get("stdout") if isinstance(response, dict) else None
    if not isinstance(content, str):
        raise RuntimeError("GitHub did not return pull-request diff text")
    limit = min(max_bytes or app.settings.max_pr_diff_bytes, app.settings.max_pr_diff_bytes)
    bounded, returned, total, truncated, digest = _bounded_utf8(content, limit)
    return PullRequestDiff(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        format=format,
        content=bounded,
        truncated=truncated,
        bytes_returned=returned,
        total_bytes=total,
        sha256=digest,
    )


@mcp.tool(
    title="List pull request files",
    description=(
        "Read-only: return one bounded page of files changed by a pull request, together "
        "with its exact base and head SHAs. A file patch may be absent or truncated by "
        "GitHub; use gh_get_pr_diff for the bounded unified diff. This tool never modifies GitHub."
    ),
    annotations=_READ_EXTERNAL,
)
async def gh_list_pr_files(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    *,
    ctx: Context[AppContext],
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based result page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Results per page, capped by server policy."),
    ] = None,
) -> PullRequestFilesPage:
    """Return one explicitly bounded page of changed files."""

    logger.info("MCP tool invocation reached server: tool=gh_list_pr_files")
    app = _app(ctx)
    _validate_repository(owner, repo)
    limit = min(app.client.clamp_max_results(per_page), 100)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}/files",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={limit}",
    )
    await _verify_pr_shas(app, owner, repo, number, (base_sha, head_sha))
    items = result if isinstance(result, list) else []
    files: list[PullRequestFile] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        patch = item.get("patch")
        patch_text = patch if isinstance(patch, str) else None
        patch_returned = 0
        patch_truncated = False
        if patch_text is not None:
            patch_text, patch_returned, _, patch_truncated, _ = _bounded_utf8(
                patch_text, app.settings.max_pr_file_patch_bytes
            )
        files.append(
            PullRequestFile.model_validate(
                {
                    **item,
                    "patch": patch_text,
                    "patch_truncated": patch_truncated,
                    "patch_bytes_returned": patch_returned,
                }
            )
        )
    return PullRequestFilesPage(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        page=page,
        per_page=limit,
        has_more=len(files) == limit,
        files=files,
    )


@mcp.tool(
    title="List pull request commits",
    description=(
        "Read-only: return one bounded page of commits in a pull request, together with "
        "its exact base and head SHAs. This tool never checks out code or modifies GitHub."
    ),
    annotations=_READ_EXTERNAL,
)
async def gh_list_pr_commits(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    *,
    ctx: Context[AppContext],
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based result page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Results per page, capped by server policy."),
    ] = None,
) -> PullRequestCommitsPage:
    """Return one explicitly bounded page of pull-request commits."""

    logger.info("MCP tool invocation reached server: tool=gh_list_pr_commits")
    app = _app(ctx)
    _validate_repository(owner, repo)
    limit = min(app.client.clamp_max_results(per_page), 100)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}/commits",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={limit}",
    )
    await _verify_pr_shas(app, owner, repo, number, (base_sha, head_sha))
    items = result if isinstance(result, list) else []
    commits: list[PullRequestCommit] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        git_commit = item.get("commit")
        git_commit = git_commit if isinstance(git_commit, dict) else {}
        author = git_commit.get("author")
        author = author if isinstance(author, dict) else {}
        committer = git_commit.get("committer")
        committer = committer if isinstance(committer, dict) else {}
        author_account = item.get("author")
        author_account = author_account if isinstance(author_account, dict) else {}
        committer_account = item.get("committer")
        committer_account = committer_account if isinstance(committer_account, dict) else {}
        message, message_returned, _, message_truncated, _ = _bounded_utf8(
            str(git_commit.get("message", "")), app.settings.max_pr_commit_message_bytes
        )
        commits.append(
            PullRequestCommit(
                sha=str(item.get("sha", "")),
                message=message,
                message_truncated=message_truncated,
                message_bytes_returned=message_returned,
                author_login=author_account.get("login"),
                author_name=author.get("name"),
                authored_at=author.get("date"),
                committer_login=committer_account.get("login"),
                committed_at=committer.get("date"),
                url=str(item.get("html_url", "")),
            )
        )
    return PullRequestCommitsPage(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        page=page,
        per_page=limit,
        has_more=len(commits) == limit,
        commits=commits,
    )


@mcp.tool(
    title="Get pull request checks",
    description=(
        "Read-only: return a bounded structured summary of CI checks for one exact "
        "pull-request head revision. This performs no watching, log download, workflow "
        "dispatch, approval, or GitHub write."
    ),
    annotations=_READ_EXTERNAL,
)
async def gh_get_pr_checks(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    number: Annotated[int, Field(ge=1, description="Positive pull request number.")],
    *,
    ctx: Context[AppContext],
    required_only: Annotated[
        bool,
        Field(description="Return only checks required by branch protection."),
    ] = False,
    max_checks: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000,
            description="Maximum checks returned, capped by server result policy.",
        ),
    ] = None,
) -> PullRequestChecks:
    """Return a bounded check summary pinned to an unchanged PR revision."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr_checks")
    app = _app(ctx)
    _validate_repository(owner, repo)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    args = [
        "pr",
        "checks",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        "bucket,completedAt,description,event,link,name,startedAt,state,workflow",
    ]
    if required_only:
        args.append("--required")
    result = await app.client.run(*args, expected_returncode={0, 1, 8})
    if not isinstance(result, list):
        raise RuntimeError("GitHub CLI did not return structured pull-request checks")
    await _verify_pr_shas(app, owner, repo, number, (base_sha, head_sha))

    limit = min(max_checks or app.settings.hard_max_results, app.settings.hard_max_results, 1_000)
    checks = [
        PullRequestCheck(
            name=str(item.get("name", "")),
            state=str(item.get("state", "UNKNOWN")),
            bucket=item.get("bucket", "pending"),
            workflow=item.get("workflow"),
            event=item.get("event"),
            description=item.get("description"),
            started_at=item.get("startedAt"),
            completed_at=item.get("completedAt"),
            link=item.get("link"),
        )
        for item in result[:limit]
        if isinstance(item, dict)
    ]
    return PullRequestChecks(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        total_count=len(result),
        truncated=len(result) > limit,
        checks=checks,
    )


@mcp.tool(
    title="Submit pull request review",
    description=(
        "Write action: submit a formal APPROVED, CHANGES_REQUESTED, or COMMENTED "
        "GitHub review for one pull request at an exact expected head commit. This is "
        "not an issue comment, never prompts, and never merges the pull request."
    ),
    annotations=_ADD_EXTERNAL,
)
async def gh_submit_pr_review(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA that was reviewed.",
        ),
    ],
    action: Annotated[
        Literal["approve", "request_changes", "comment"],
        Field(description="Formal GitHub review disposition."),
    ],
    *,
    ctx: Context[AppContext],
    body: Annotated[
        str,
        Field(
            max_length=65_536,
            description=(
                "Review body. Required for request_changes and comment; optional for approve."
            ),
        ),
    ] = "",
) -> PullRequestReviewSubmission:
    """Submit and read back a formal review pinned to an exact PR commit."""

    logger.info("MCP tool invocation reached server: tool=gh_submit_pr_review")
    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="pr_review")
    if action in {"request_changes", "comment"} and not body.strip():
        raise ValueError(f"a non-empty review body is required for {action}")

    metadata = await _get_pr_metadata(app, owner, repo, number)
    _, current_head_sha = _extract_pr_shas(metadata)
    expected = expected_head_sha.lower()
    if current_head_sha.lower() != expected:
        raise RuntimeError(
            f"Pull request head changed: expected {expected}, current {current_head_sha}"
        )

    if action == "approve":
        viewer = await app.client.run("api", "user", "-X", "GET")
        viewer_login = viewer.get("login") if isinstance(viewer, dict) else None
        author = metadata.get("user")
        author_login = author.get("login") if isinstance(author, dict) else None
        if (
            isinstance(viewer_login, str)
            and isinstance(author_login, str)
            and viewer_login.casefold() == author_login.casefold()
        ):
            raise ValueError(
                f"authenticated GitHub account {viewer_login!r} is the pull request author "
                "and cannot approve its own pull request; no review was attempted"
            )

    event = {
        "approve": "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }[action]
    created = await _api_json_write(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/pulls/{number}/reviews",
        {"body": body, "event": event, "commit_id": expected},
    )
    review_id = created.get("id")
    review_url = str(created.get("html_url", f"https://github.com/{owner}/{repo}/pull/{number}"))
    if not isinstance(review_id, int):
        warning = _readback_warning("Pull request review", review_url)
        return PullRequestReviewSubmission(
            number=number,
            review_id=0,
            action=action,
            state=str(created.get("state", event)),
            body=body,
            commit_sha=expected,
            url=review_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    try:
        review = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}",
            "-X",
            "GET",
        )
        if not isinstance(review, dict):
            raise RuntimeError("GitHub returned a non-object review readback")
    except RuntimeError:
        warning = _readback_warning("Pull request review", review_url)
        return PullRequestReviewSubmission(
            number=number,
            review_id=review_id,
            action=action,
            state=str(created.get("state", event)),
            body=str(created.get("body", body)),
            commit_sha=str(created.get("commit_id", expected)),
            url=review_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    user = review.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    return PullRequestReviewSubmission(
        number=number,
        review_id=review_id,
        action=action,
        state=str(review.get("state", event)),
        body=str(review.get("body", body)),
        author=author,
        submitted_at=review.get("submitted_at"),
        commit_sha=str(review.get("commit_id", expected)),
        url=str(review.get("html_url", review_url)),
        message=f"Formal pull request review submitted with state {review.get('state', event)}.",
    )


@mcp.tool(
    title="Merge pull request at exact head",
    description=(
        "Destructive write: merge one pull request using an explicit strategy only when "
        "its head still matches expected_head_sha. This tool cannot use administrator "
        "bypass, delete the branch, or silently merge a changed revision. It requires "
        "MCP_GH_ALLOW_PR_MERGE=true in addition to ordinary write authorization."
    ),
    annotations=_MUTATE_EXTERNAL,
)
async def gh_merge_pr(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA authorized for merge.",
        ),
    ],
    method: Annotated[
        Literal["merge", "squash", "rebase"],
        Field(description="Repository-supported merge strategy."),
    ],
    *,
    ctx: Context[AppContext],
    subject: Annotated[
        str | None,
        Field(max_length=256, description="Optional merge commit subject."),
    ] = None,
    body: Annotated[
        str,
        Field(max_length=65_536, description="Optional merge commit body."),
    ] = "",
) -> PullRequestMerge:
    """Merge a PR with GitHub's atomic expected-head guard, then read it back."""

    logger.info("MCP tool invocation reached server: tool=gh_merge_pr")
    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="pr_merge")
    _, current_head_sha = await _get_pr_shas(app, owner, repo, number)
    expected = expected_head_sha.lower()
    if current_head_sha.lower() != expected:
        raise RuntimeError(
            f"Pull request head changed: expected {expected}, current {current_head_sha}"
        )

    args = [
        "pr",
        "merge",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        f"--{method}",
        "--match-head-commit",
        expected,
        "--body-file",
        "-",
    ]
    if subject is not None:
        args.extend(["--subject", subject])
    await app.client.run(*args, json_output=False, stdin_text=body)

    pull_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    try:
        result = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            ("number,url,state,mergedAt,mergeCommit,headRefOid,mergeStateStatus,autoMergeRequest"),
        )
    except RuntimeError:
        warning = _readback_warning("Pull request merge", pull_url)
        return PullRequestMerge(
            number=number,
            method=method,
            head_sha=expected,
            state="UNKNOWN",
            merged=False,
            url=pull_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    merge_commit = result.get("mergeCommit")
    merge_commit_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    merged_at = result.get("mergedAt")
    state = str(result.get("state", "UNKNOWN"))
    merge_state_status = result.get("mergeStateStatus")
    merged = state.upper() == "MERGED" or isinstance(merged_at, str)
    queued = isinstance(merge_state_status, str) and merge_state_status.upper() == "QUEUED"
    auto_merge_enabled = isinstance(result.get("autoMergeRequest"), dict)
    if merged:
        message = "Pull request merged successfully."
    elif queued or auto_merge_enabled:
        message = "Merge command completed; the pull request is queued or awaiting requirements."
    else:
        message = f"Merge command completed; pull request state is {state}."
    return PullRequestMerge(
        number=number,
        method=method,
        head_sha=str(result.get("headRefOid", expected)),
        state=state,
        merged=merged,
        merge_queued=queued,
        auto_merge_enabled=auto_merge_enabled,
        merged_at=merged_at,
        merge_commit_sha=merge_commit_sha,
        merge_state_status=merge_state_status,
        url=str(result.get("url", pull_url)),
        message=message,
    )


@mcp.tool(annotations=_ADD_EXTERNAL)
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
) -> PullRequestCreate:
    """Create a new pull request in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="pr_create")

    args = [
        "pr",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
        "--body-file",
        "-",
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

    create_result = await app.client.run(*args, json_output=False, stdin_text=body)
    created_url = _optional_created_url(create_result)
    if created_url is None:
        warning = _readback_warning("Pull request")
        return PullRequestCreate(
            number=0,
            title=title,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    try:
        result = await app.client.run(
            "pr",
            "view",
            created_url,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,number,url",
        )
    except RuntimeError:
        warning = _readback_warning("Pull request", created_url)
        return PullRequestCreate(
            number=_trailing_number(created_url),
            title=title,
            url=created_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return PullRequestCreate(
        number=result.get("number", 0),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Pull request created successfully.",
    )


# ---------------------------------------------------------------------------
# Repository tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_EXTERNAL)
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
    result = await app.client.run(
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


@mcp.tool(annotations=_READ_EXTERNAL)
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
    args.extend(
        [
            "--json",
            fields,
            "--limit",
            str(limit),
        ]
    )

    # Translate type
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
    annotations=_READ_EXTERNAL,
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
    app = _app(ctx)
    _validate_repository(owner, repo)
    _validate_repo_path(path)
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
    if not isinstance(sha, str) or not _OBJECT_SHA_RE.fullmatch(sha):
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


@mcp.tool(
    title="Commit repository files atomically",
    description=(
        "Write action: create or replace complete UTF-8 files in one Git commit and "
        "conditionally advance one branch only when its head matches expected_head_sha. "
        "This tool requires host approval and server-side content-commit authorization."
    ),
    annotations=_MUTATE_EXTERNAL,
)
async def gh_commit_files(
    owner: Annotated[
        str,
        Field(description="GitHub repository owner or organization login.", min_length=1),
    ],
    repo: Annotated[
        str,
        Field(description="GitHub repository name without the owner prefix.", min_length=1),
    ],
    branch: Annotated[
        str,
        Field(description="Existing branch to advance conditionally.", min_length=1),
    ],
    expected_head_sha: Annotated[
        str,
        Field(
            description="Exact 40-character branch head SHA required before the write.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    files: Annotated[
        list[CommitFile],
        Field(
            description="Complete UTF-8 file replacements to include in the atomic commit.",
            min_length=1,
            max_length=1000,
        ),
    ],
    commit_message: Annotated[
        str,
        Field(description="Git commit message.", min_length=1, max_length=65_536),
    ],
    *,
    ctx: Context[AppContext],
) -> CommitFilesResult:
    """Create one commit from complete file contents and atomically advance a branch.

    The branch advances only when its current head matches expected_head_sha and
    GitHub accepts a non-forced fast-forward ref update. Files create or replace
    repository paths; deletion is intentionally unsupported.
    """

    logger.info("MCP tool invocation reached server: tool=gh_commit_files")
    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="content_commit")
    _validate_branch(branch)
    if not _OBJECT_SHA_RE.fullmatch(expected_head_sha):
        raise ValueError("expected_head_sha must be a full 40-character Git object SHA")
    if not commit_message.strip():
        raise ValueError("commit_message must not be empty")
    if len(commit_message.encode()) > 65_536:
        raise ValueError("commit_message exceeds 65536 UTF-8 bytes")
    if not files:
        raise ValueError("files must contain at least one file")
    if len(files) > app.settings.max_commit_files:
        raise ValueError(f"files exceeds MCP_GH_MAX_COMMIT_FILES={app.settings.max_commit_files}")

    total_bytes = 0
    paths: set[str] = set()
    for file in files:
        _validate_repo_path(file.path)
        if file.path in paths:
            raise ValueError(f"duplicate file path: {file.path!r}")
        paths.add(file.path)
        size = len(file.content.encode())
        if size > app.settings.max_file_bytes:
            raise ValueError(
                f"file {file.path!r} exceeds MCP_GH_MAX_FILE_BYTES={app.settings.max_file_bytes}"
            )
        total_bytes += size
    if total_bytes > app.settings.max_commit_bytes:
        raise ValueError(
            f"file contents exceed MCP_GH_MAX_COMMIT_BYTES={app.settings.max_commit_bytes}"
        )

    branch_path = quote(branch, safe="/")
    ref_result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
    )
    ref_object = ref_result.get("object") if isinstance(ref_result, dict) else None
    actual_head_sha = ref_object.get("sha") if isinstance(ref_object, dict) else None
    if not isinstance(actual_head_sha, str):
        raise RuntimeError(f"GitHub did not return the current head of branch {branch!r}")
    if actual_head_sha.casefold() != expected_head_sha.casefold():
        raise RuntimeError(
            f"Branch {branch!r} head mismatch: expected {expected_head_sha}, "
            f"found {actual_head_sha}; no commit objects were created"
        )

    repository = await app.client.run("api", f"repos/{owner}/{repo}")
    repository_node_id = repository.get("node_id") if isinstance(repository, dict) else None
    if not isinstance(repository_node_id, str) or not repository_node_id:
        raise RuntimeError(f"GitHub did not return the node ID for repository {owner}/{repo}")

    parent = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/git/commits/{actual_head_sha}",
    )
    parent_tree = parent.get("tree") if isinstance(parent, dict) else None
    base_tree_sha = parent_tree.get("sha") if isinstance(parent_tree, dict) else None
    if not isinstance(base_tree_sha, str):
        raise RuntimeError(f"GitHub did not return the tree for commit {actual_head_sha}")

    tree_entries: list[dict[str, str]] = []
    for file in files:
        blob = await _api_json_write(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/git/blobs",
            {"content": file.content, "encoding": "utf-8"},
        )
        blob_sha = blob.get("sha")
        if not isinstance(blob_sha, str):
            raise RuntimeError(f"GitHub did not return a blob SHA for {file.path!r}")
        tree_entries.append({"path": file.path, "mode": file.mode, "type": "blob", "sha": blob_sha})

    tree = await _api_json_write(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/trees",
        {"base_tree": base_tree_sha, "tree": tree_entries},
    )
    tree_sha = tree.get("sha")
    if not isinstance(tree_sha, str):
        raise RuntimeError("GitHub did not return the newly created tree SHA")
    if tree_sha == base_tree_sha:
        raise ValueError("the supplied files do not change the branch tree")

    commit = await _api_json_write(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/commits",
        {"message": commit_message, "tree": tree_sha, "parents": [actual_head_sha]},
    )
    commit_sha = commit.get("sha")
    if not isinstance(commit_sha, str):
        raise RuntimeError("GitHub did not return the newly created commit SHA")
    commit_url = commit.get("html_url")
    url = commit_url if isinstance(commit_url, str) else ""

    try:
        updated_ref = await _api_json_write(
            app.client,
            "POST",
            "graphql",
            {
                "query": (
                    "mutation($input: UpdateRefsInput!) { "
                    "updateRefs(input: $input) { clientMutationId } }"
                ),
                "variables": {
                    "input": {
                        "repositoryId": repository_node_id,
                        "refUpdates": [
                            {
                                "name": f"refs/heads/{branch}",
                                "beforeOid": actual_head_sha,
                                "afterOid": commit_sha,
                                "force": False,
                            }
                        ],
                    }
                },
            },
        )
        update_payload = updated_ref.get("data")
        update_result = (
            update_payload.get("updateRefs") if isinstance(update_payload, dict) else None
        )
        if not isinstance(update_result, dict):
            raise RuntimeError("GitHub returned an unexpected atomic ref update response")
    except RuntimeError as update_error:
        try:
            failure_readback = await app.client.run(
                "api",
                f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
            )
        except RuntimeError:
            warning = (
                f"Commit object {commit_sha} was created, but the atomic branch update failed "
                f"or returned an unreadable response ({update_error}). The branch update outcome "
                "is unknown. Do not retry automatically; read the branch head first."
            )
            return CommitFilesResult(
                branch=branch,
                previous_head_sha=actual_head_sha,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                ref_updated=None,
                files_committed=0,
                url=url,
                write_completed=False,
                readback_completed=False,
                warning=warning,
                message=warning,
            )

        failure_object = (
            failure_readback.get("object") if isinstance(failure_readback, dict) else None
        )
        failure_sha = failure_object.get("sha") if isinstance(failure_object, dict) else None
        if failure_sha == commit_sha:
            warning = (
                f"The atomic update command reported an error ({update_error}), but readback "
                f"confirms commit {commit_sha} is the branch head. Do not retry."
            )
            return CommitFilesResult(
                branch=branch,
                previous_head_sha=actual_head_sha,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                ref_updated=True,
                files_committed=len(files),
                url=url,
                write_completed=True,
                readback_completed=True,
                warning=warning,
                message=f"Committed {len(files)} file(s) to {branch}.",
            )

        if failure_sha == actual_head_sha:
            branch_status = "The branch head is unchanged."
        elif isinstance(failure_sha, str):
            branch_status = f"The branch now points to a different commit, {failure_sha}."
        else:
            branch_status = "The branch head could not be interpreted."
        warning = (
            f"Commit object {commit_sha} was created, but it was not installed on branch "
            f"{branch!r}. {branch_status} Do not retry automatically; re-read the branch first."
        )
        return CommitFilesResult(
            branch=branch,
            previous_head_sha=actual_head_sha,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            ref_updated=False,
            files_committed=0,
            url=url,
            write_completed=False,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    try:
        readback = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
        )
    except RuntimeError:
        warning = _readback_warning(f"Commit {commit_sha}", url or None)
        return CommitFilesResult(
            branch=branch,
            previous_head_sha=actual_head_sha,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            ref_updated=True,
            files_committed=len(files),
            url=url,
            write_completed=True,
            readback_completed=False,
            warning=warning,
            message=f"Committed {len(files)} file(s) to {branch}; ref readback failed.",
        )

    readback_object = readback.get("object") if isinstance(readback, dict) else None
    readback_sha = readback_object.get("sha") if isinstance(readback_object, dict) else None
    if readback_sha != commit_sha:
        warning = (
            f"Atomic update completed for commit {commit_sha}, but ref readback returned "
            f"{readback_sha!r}. Do not retry automatically; inspect the branch first."
        )
        return CommitFilesResult(
            branch=branch,
            previous_head_sha=actual_head_sha,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            ref_updated=True,
            files_committed=len(files),
            url=url,
            write_completed=True,
            readback_completed=False,
            warning=warning,
            message=f"Committed {len(files)} file(s) to {branch}; ref readback diverged.",
        )
    return CommitFilesResult(
        branch=branch,
        previous_head_sha=actual_head_sha,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        ref_updated=True,
        files_committed=len(files),
        url=url,
        message=f"Committed {len(files)} file(s) to {branch}.",
    )


@mcp.tool(annotations=_ADD_EXTERNAL)
async def gh_create_repo(
    name: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    private: bool = False,
    auto_init: bool = False,
) -> RepoCreate:
    """Create a new repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = _app(ctx)
    _require_action_enabled(app, "repo_create")
    if name.count("/") > 1:
        raise ValueError("repository name must be REPO or OWNER/REPO")
    if "/" in name:
        owner, repo_name = name.split("/", 1)
    else:
        account = await app.client.run("api", "user")
        owner_login = account.get("login") if isinstance(account, dict) else None
        if not isinstance(owner_login, str) or not owner_login:
            raise RuntimeError(
                "Unable to determine the authenticated owner before repository creation"
            )
        owner = owner_login
        repo_name = name
    _require_write_enabled(app, owner, repo_name, action="repo_create")
    full_name = f"{owner}/{repo_name}"

    args = [
        "repo",
        "create",
        full_name,
        "--private" if private else "--public",
    ]
    if description:
        args.extend(["--description", description])
    if auto_init:
        args.append("--add-readme")

    await app.client.run(*args, json_output=False)
    try:
        result = await app.client.run(
            "repo",
            "view",
            full_name,
            "--json",
            "nameWithOwner,url",
        )
    except RuntimeError:
        warning = _readback_warning("Repository", full_name)
        return RepoCreate(
            name=full_name,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return RepoCreate(
        name=result.get("nameWithOwner", full_name),
        url=result.get("url", ""),
        message="Repository created successfully.",
    )


# ---------------------------------------------------------------------------
# Release tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_EXTERNAL)
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
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} releases",
    )


@mcp.tool(annotations=_READ_EXTERNAL)
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


@mcp.tool(annotations=_ADD_EXTERNAL)
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

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="release_create")

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
        warning = _readback_warning("Release", tag_name)
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


# ---------------------------------------------------------------------------
# Workflow tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_EXTERNAL)
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

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} workflows ({state})",
    )


@mcp.tool(annotations=_READ_EXTERNAL)
async def gh_get_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    *,
    ctx: Context[AppContext],
) -> WorkflowInfo:
    """Get details of a specific GitHub Actions workflow."""

    app = _app(ctx)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/workflows/{workflow_id}",
    )
    return WorkflowInfo.model_validate(result)


@mcp.tool(annotations=_MUTATE_EXTERNAL)
async def gh_run_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    ref: str = "main",
    *,
    ctx: Context[AppContext],
    fields: list[str] | None = None,
) -> WorkflowRunCreate:
    """Trigger a workflow dispatch event for a GitHub Actions workflow.

    The workflow must support an `on.workflow_dispatch` trigger.
    Use `fields` to pass inputs as key=value pairs (e.g. ["key=value"]).
    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true.
    """

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="workflow_dispatch")

    args = [
        "workflow",
        "run",
        str(workflow_id),
        "--repo",
        f"{owner}/{repo}",
        "--ref",
        ref,
    ]
    if fields:
        for field in fields:
            if "=" not in field or not field.split("=", 1)[0]:
                raise ValueError("workflow fields must use non-empty key=value form")
            args.extend(["-f", field])
        stdin_text = None
    else:
        args.append("--json")
        stdin_text = "{}"

    dispatch_result = await app.client.run(*args, json_output=False, stdin_text=stdin_text)
    stdout = dispatch_result.get("stdout", "") if isinstance(dispatch_result, dict) else ""
    if not isinstance(stdout, str) or not stdout.strip():
        warning = _readback_warning("Workflow dispatch")
        return WorkflowRunCreate(
            run_id=None,
            url=None,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    created_url = _created_url(dispatch_result, "Workflow run")
    run_id = _workflow_run_id(created_url)
    try:
        result = await app.client.run(
            "run",
            "view",
            str(run_id),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "databaseId,url",
        )
    except RuntimeError:
        warning = _readback_warning("Workflow dispatch", created_url)
        return WorkflowRunCreate(
            run_id=run_id,
            url=created_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return WorkflowRunCreate(
        run_id=result.get("databaseId", run_id),
        url=result.get("url", created_url),
        message="Workflow dispatch triggered successfully.",
    )


# ---------------------------------------------------------------------------
# Run tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_EXTERNAL)
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

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} runs",
    )


@mcp.tool(annotations=_READ_EXTERNAL)
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
    result = await app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    return WorkflowRun.model_validate(result)


async def _get_run_snapshot(
    app: AppContext,
    owner: str,
    repo: str,
    run_id: int,
    attempt: int | None,
) -> tuple[int, str, str, str | None, str | None]:
    """Resolve one workflow-run attempt and its immutable head revision."""

    args = [
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        "attempt,headSha,status,conclusion,url",
    ]
    if attempt is not None:
        args.extend(["--attempt", str(attempt)])
    result = await app.client.run(*args)
    if not isinstance(result, dict):
        raise RuntimeError("GitHub CLI did not return workflow-run metadata")
    actual_attempt = result.get("attempt")
    head_sha = result.get("headSha")
    status = result.get("status")
    if not isinstance(actual_attempt, int) or actual_attempt < 1:
        raise RuntimeError("GitHub CLI did not return a valid workflow-run attempt")
    if attempt is not None and actual_attempt != attempt:
        raise RuntimeError(
            f"GitHub returned workflow-run attempt {actual_attempt}, expected {attempt}"
        )
    if not isinstance(head_sha, str) or not _OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub CLI did not return a valid workflow-run head SHA")
    if not isinstance(status, str) or not status:
        raise RuntimeError("GitHub CLI did not return a workflow-run status")
    conclusion = result.get("conclusion")
    url = result.get("url")
    return (
        actual_attempt,
        head_sha,
        status,
        conclusion if isinstance(conclusion, str) else None,
        url if isinstance(url, str) else None,
    )


@mcp.tool(
    title="List workflow run jobs",
    description=(
        "Read-only: return one bounded page of jobs and step metadata for an exact "
        "GitHub Actions run attempt. This downloads no logs, performs no watching or "
        "workflow dispatch, and never modifies GitHub."
    ),
    annotations=_READ_EXTERNAL,
)
async def gh_list_run_jobs(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    run_id: Annotated[int, Field(ge=1, description="Positive workflow run identifier.")],
    *,
    ctx: Context[AppContext],
    attempt: Annotated[
        int | None,
        Field(ge=1, description="Exact run attempt; omit for the latest attempt."),
    ] = None,
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based result page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Jobs per page, capped by server policy."),
    ] = None,
) -> WorkflowJobsPage:
    """Return one bounded page of jobs pinned to an exact run attempt."""

    logger.info("MCP tool invocation reached server: tool=gh_list_run_jobs")
    app = _app(ctx)
    _validate_repository(owner, repo)
    resolved_attempt, head_sha, _, _, _ = await _get_run_snapshot(app, owner, repo, run_id, attempt)
    limit = min(app.client.clamp_max_results(per_page), 100)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{resolved_attempt}/jobs",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={limit}",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return structured workflow jobs")
    raw_jobs = result.get("jobs")
    if not isinstance(raw_jobs, list):
        raise RuntimeError("GitHub did not return a workflow jobs list")
    total_count = result.get("total_count")
    if not isinstance(total_count, int) or total_count < 0:
        raise RuntimeError("GitHub did not return a valid workflow job count")

    jobs: list[WorkflowJob] = []
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        raw_steps = item.get("steps")
        steps = (
            [
                WorkflowJobStep(
                    number=step.get("number", 0),
                    name=str(step.get("name", "")),
                    status=str(step.get("status", "unknown")),
                    conclusion=step.get("conclusion"),
                    started_at=step.get("started_at"),
                    completed_at=step.get("completed_at"),
                )
                for step in raw_steps
                if isinstance(step, dict)
            ]
            if isinstance(raw_steps, list)
            else []
        )
        jobs.append(
            WorkflowJob(
                id=item.get("id", 0),
                name=str(item.get("name", "")),
                status=str(item.get("status", "unknown")),
                conclusion=item.get("conclusion"),
                started_at=item.get("started_at"),
                completed_at=item.get("completed_at"),
                url=item.get("html_url"),
                runner_name=item.get("runner_name"),
                steps=steps,
            )
        )

    verified_attempt, verified_sha, _, _, _ = await _get_run_snapshot(
        app, owner, repo, run_id, resolved_attempt
    )
    if (verified_attempt, verified_sha) != (resolved_attempt, head_sha):
        raise RuntimeError("Workflow run attempt changed during the jobs read; retry")
    return WorkflowJobsPage(
        run_id=run_id,
        attempt=resolved_attempt,
        head_sha=head_sha,
        page=page,
        per_page=limit,
        total_count=total_count,
        has_more=page * limit < total_count,
        jobs=jobs,
    )


@mcp.tool(
    title="Read failed workflow logs",
    description=(
        "Read-only: return bounded failed-step log text for one exact GitHub Actions "
        "run attempt, with truncation metadata and a SHA-256 fingerprint. This never "
        "reruns, cancels, deletes, or dispatches a workflow and never requests input."
    ),
    annotations=_READ_EXTERNAL,
)
async def gh_get_failed_run_logs(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    run_id: Annotated[int, Field(ge=1, description="Positive workflow run identifier.")],
    *,
    ctx: Context[AppContext],
    attempt: Annotated[
        int | None,
        Field(ge=1, description="Exact run attempt; omit for the latest attempt."),
    ] = None,
    max_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description=(
                "Maximum UTF-8 bytes returned, capped by MCP_GH_MAX_FAILED_RUN_LOG_BYTES."
            ),
        ),
    ] = None,
) -> WorkflowRunFailedLogs:
    """Return bounded failed-step logs pinned to an exact run attempt."""

    logger.info("MCP tool invocation reached server: tool=gh_get_failed_run_logs")
    app = _app(ctx)
    _validate_repository(owner, repo)
    resolved_attempt, head_sha, status, conclusion, url = await _get_run_snapshot(
        app, owner, repo, run_id, attempt
    )
    result = await app.client.run(
        "run",
        "view",
        str(run_id),
        "--repo",
        f"{owner}/{repo}",
        "--attempt",
        str(resolved_attempt),
        "--log-failed",
        json_output=False,
    )
    content = result.get("stdout") if isinstance(result, dict) else None
    if not isinstance(content, str):
        raise RuntimeError("GitHub CLI did not return failed-step log text")
    verified_attempt, verified_sha, _, _, _ = await _get_run_snapshot(
        app, owner, repo, run_id, resolved_attempt
    )
    if (verified_attempt, verified_sha) != (resolved_attempt, head_sha):
        raise RuntimeError("Workflow run attempt changed during the log read; retry")

    limit = min(
        max_bytes or app.settings.max_failed_run_log_bytes,
        app.settings.max_failed_run_log_bytes,
    )
    bounded, returned, total, truncated, digest = _bounded_utf8(content, limit)
    return WorkflowRunFailedLogs(
        run_id=run_id,
        attempt=resolved_attempt,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
        url=url,
        content=bounded,
        truncated=truncated,
        bytes_returned=returned,
        total_bytes=total,
        sha256=digest,
    )


@mcp.tool(annotations=_READ_EXTERNAL)
async def gh_watch_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    ctx: Context[AppContext],
    interval: int = 10,
    exit_status: bool = False,
    timeout_seconds: int = 1800,
) -> WorkflowRunWatchResult:
    """Poll a GitHub Actions workflow run until completion or timeout."""

    app = _app(ctx)
    _validate_repository(owner, repo)
    if interval < 1 or timeout_seconds < 1:
        raise ValueError("interval and timeout_seconds must be positive")

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        view_result = await app.client.run(
            "run",
            "view",
            str(run_id),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "status,conclusion,url",
        )
        status = view_result.get("status") if isinstance(view_result, dict) else None
        conclusion = view_result.get("conclusion") if isinstance(view_result, dict) else None
        url = view_result.get("url") if isinstance(view_result, dict) else None
        if status == "completed":
            if exit_status and conclusion != "success":
                raise RuntimeError(f"Run #{run_id} completed with conclusion: {conclusion}")
            return WorkflowRunWatchResult(
                run_id=run_id,
                conclusion=conclusion,
                status=status,
                url=url,
                message=f"Run #{run_id} completed with conclusion: {conclusion or 'unknown'}",
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"Run #{run_id} did not complete within {timeout_seconds}s")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Issue edit tool
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATE_EXTERNAL)
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
) -> IssueEdit:
    """Edit an existing issue in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="issue_edit")
    if milestone is not None and remove_milestone:
        raise ValueError("milestone and remove_milestone are mutually exclusive")
    if not any(
        (
            title is not None,
            body is not None,
            labels_add,
            labels_remove,
            assignees_add,
            assignees_remove,
            milestone is not None,
            remove_milestone,
        )
    ):
        raise ValueError("at least one issue edit must be provided")
    if title == "":
        raise ValueError("issue title cannot be empty")

    args = [
        "issue",
        "edit",
        str(number),
        "--repo",
        f"{owner}/{repo}",
    ]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body-file", "-"])
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
        milestone_result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/milestones/{milestone}",
        )
        milestone_title = milestone_result.get("title")
        if not isinstance(milestone_title, str) or not milestone_title:
            raise RuntimeError(f"Unable to resolve milestone #{milestone} to its title")
        args.extend(["--milestone", milestone_title])
    if remove_milestone:
        args.append("--remove-milestone")

    await app.client.run(*args, json_output=False, stdin_text=body if body is not None else None)
    try:
        result = await app.client.run(
            "issue",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,number,state,url",
        )
    except RuntimeError:
        warning = _readback_warning("Issue edit", f"{owner}/{repo}#{number}")
        return IssueEdit(
            number=number,
            title=title or "",
            state="",
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
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


@mcp.tool(annotations=_READ_EXTERNAL)
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


@mcp.tool(annotations=_ADD_EXTERNAL)
async def gh_create_label(
    owner: str,
    repo: str,
    name: str,
    color: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
) -> LabelCreate:
    """Create a new label without overwriting an existing label."""

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="label_create")
    return await _create_label(app, owner, repo, name, color, description, force=False)


@mcp.tool(annotations=_MUTATE_EXTERNAL)
async def gh_upsert_label(
    owner: str,
    repo: str,
    name: str,
    color: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
) -> LabelCreate:
    """Create a label or overwrite the existing label's color and description."""

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="label_upsert")
    return await _create_label(app, owner, repo, name, color, description, force=True)


async def _create_label(
    app: AppContext,
    owner: str,
    repo: str,
    name: str,
    color: str,
    description: str | None,
    *,
    force: bool,
) -> LabelCreate:
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ValueError("label color must be exactly six hexadecimal characters")

    args = [
        "label",
        "create",
        name,
        "--repo",
        f"{owner}/{repo}",
        "--color",
        color,
    ]
    if description is not None:
        args.extend(["--description", description])
    if force:
        args.append("--force")

    await app.client.run(*args, json_output=False)
    try:
        result = await _get_label(app.client, owner, repo, name)
    except RuntimeError:
        warning = _readback_warning("Label", name)
        return LabelCreate(
            name=name,
            color=color,
            description=description,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return LabelCreate(
        name=result.get("name", name),
        color=result.get("color", color),
        description=result.get("description", description),
        url=result.get("url", ""),
        message="Label created successfully.",
    )


@mcp.tool(annotations=_MUTATE_EXTERNAL)
async def gh_edit_label(
    owner: str,
    repo: str,
    name: str,
    *,
    ctx: Context[AppContext],
    new_name: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> LabelEdit:
    """Edit an existing label in a repository."""

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="label_edit")
    if new_name is None and color is None and description is None:
        raise ValueError("at least one label edit must be provided")
    if new_name == "":
        raise ValueError("new label name cannot be empty")
    if color is not None and not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ValueError("label color must be exactly six hexadecimal characters")

    args = [
        "label",
        "edit",
        name,
        "--repo",
        f"{owner}/{repo}",
    ]
    if new_name is not None:
        args.extend(["--name", new_name])
    if color is not None:
        args.extend(["--color", color])
    if description is not None:
        args.extend(["--description", description])

    await app.client.run(*args, json_output=False)
    result_name = new_name or name
    try:
        result = await _get_label(app.client, owner, repo, result_name)
    except RuntimeError:
        warning = _readback_warning("Label edit", result_name)
        return LabelEdit(
            name=result_name,
            color=color or "",
            description=description,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
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


@mcp.tool(annotations=_READ_EXTERNAL)
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


@mcp.tool(annotations=_ADD_EXTERNAL)
async def gh_create_milestone(
    owner: str,
    repo: str,
    title: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    due_on: str | None = None,
    state: str = "open",
) -> MilestoneCreate:
    """Create a new milestone in a repository via the GitHub API.

    due_on: due date in ISO format (e.g. '2026-12-31').
    state: open or closed (default: open).

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="milestone_create")
    if state not in {"open", "closed"}:
        raise ValueError("milestone state must be open or closed")

    payload: dict[str, Any] = {"title": title, "state": state}
    if description is not None:
        payload["description"] = description
    if due_on:
        payload["due_on"] = due_on

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        f.flush()
        payload_path = f.name

    try:
        create_result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/milestones",
            "-X",
            "POST",
            "--input",
            payload_path,
            json_output=False,
        )
    finally:
        os.unlink(payload_path)

    try:
        created = _created_json(create_result, "Milestone")
    except RuntimeError:
        warning = _readback_warning("Milestone")
        return MilestoneCreate(
            number=0,
            title=title,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    number = created.get("number")
    if not isinstance(number, int):
        warning = _readback_warning("Milestone")
        return MilestoneCreate(
            number=0,
            title=title,
            url=str(created.get("url", "")),
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    try:
        result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/milestones/{number}",
        )
    except RuntimeError:
        warning = _readback_warning("Milestone", f"#{number}")
        return MilestoneCreate(
            number=number,
            title=str(created.get("title", title)),
            url=str(created.get("url", "")),
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    return MilestoneCreate(
        number=result.get("number", number),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Milestone created successfully.",
    )


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Comment / Branch / Edit PR Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_ADD_EXTERNAL)
async def gh_create_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    *,
    ctx: Context[AppContext],
) -> CommentCreate:
    """Post a comment on an issue or pull request."""
    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="comment_create")

    create_result = await app.client.run(
        "issue",
        "comment",
        str(issue_number),
        "--repo",
        f"{owner}/{repo}",
        "--body-file",
        "-",
        json_output=False,
        stdin_text=body,
    )
    created_url = _optional_created_url(create_result)
    if created_url is None:
        warning = _readback_warning("Comment")
        return CommentCreate(
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return CommentCreate(
        url=created_url,
        message="Comment posted successfully.",
    )


@mcp.tool(annotations=_ADD_EXTERNAL)
async def gh_create_branch(
    owner: str,
    repo: str,
    issue_number: int,
    name: str,
    *,
    ctx: Context[AppContext],
    base: str | None = None,
) -> BranchCreate:
    """Create a new branch for an issue."""
    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="branch_create")

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


@mcp.tool(annotations=_MUTATE_EXTERNAL)
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
) -> PullRequestEdit:
    """Edit an existing pull request."""
    app = _app(ctx)
    _require_write_enabled(app, owner, repo, action="pr_edit")
    if not any(
        (
            title is not None,
            body is not None,
            labels_add,
            labels_remove,
            assignees_add,
            assignees_remove,
            base is not None,
        )
    ):
        raise ValueError("at least one pull request edit must be provided")
    if title == "":
        raise ValueError("pull request title cannot be empty")
    if base == "":
        raise ValueError("pull request base cannot be empty")

    args = [
        "pr",
        "edit",
        str(number),
        "--repo",
        f"{owner}/{repo}",
    ]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body-file", "-"])
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
    if base is not None:
        args.extend(["--base", base])

    await app.client.run(*args, json_output=False, stdin_text=body if body is not None else None)

    # Fetch updated details
    fields = "title,url"
    try:
        info_result = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            fields,
        )
    except RuntimeError:
        warning = _readback_warning("Pull request edit", f"{owner}/{repo}#{number}")
        return PullRequestEdit(
            number=number,
            title=title or "",
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    return PullRequestEdit(
        number=number,
        title=info_result.get("title", ""),
        url=info_result.get("url", ""),
        message="Pull request updated successfully.",
    )
