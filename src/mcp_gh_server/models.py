"""Structured MCP tool result models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

# ---------------------------------------------------------------------------
# Info tools
# ---------------------------------------------------------------------------


class GhVersionInfo(BaseModel):
    """Version and authentication status of the gh CLI."""

    version: str
    authenticated: bool
    active_account: str | None = None
    hostname: str | None = None


class ServerInfo(BaseModel):
    """Local MCP server version, action surface, and write-policy status."""

    server_name: Literal["mcp-gh-server"] = "mcp-gh-server"
    server_version: str
    tool_schema_version: str
    transport: Literal["stdio", "streamable-http"]
    tool_count: int = Field(ge=1)
    write_commands_enabled: bool
    content_commits_enabled: bool


# ---------------------------------------------------------------------------
# Search tools
# ---------------------------------------------------------------------------


class SearchResultItem(BaseModel):
    """A single item in a search result."""

    title: str
    url: str


class RepoSearchItem(SearchResultItem):
    """A repository from search results."""

    full_name: str = Field(alias="fullName")
    description: str | None = None
    stargazers: int = Field(0, alias="stargazersCount")
    forks: int = Field(0, alias="forksCount")
    language: str | None = None
    created_at: str | None = Field(None, alias="createdAt")
    updated_at: str | None = Field(None, alias="updatedAt")
    license: str | None = None


class IssueSearchItem(SearchResultItem):
    """An issue or pull request from search results."""

    number: int
    state: str
    author: str | None = None
    created_at: str | None = Field(None, alias="createdAt")
    updated_at: str | None = Field(None, alias="updatedAt")
    labels: list[Any] = Field(default_factory=list)
    repository: str | None = None
    comments_count: int = 0


class CodeSearchItem(SearchResultItem):
    """A code match from search results."""

    name: str
    path: str
    repository: str
    sha: str | None = None
    line: int | None = None


class SearchResults(BaseModel):
    """Paginated search results."""

    total_count: int = 0
    items: list[JsonValue]
    truncated: bool
    query: str


# ---------------------------------------------------------------------------
# Issue tools
# ---------------------------------------------------------------------------


class IssueInfo(BaseModel):
    """An issue or pull request."""

    number: int
    title: str
    state: str
    author: str | None = None
    created_at: str | None = Field(None, alias="createdAt")
    updated_at: str | None = Field(None, alias="updatedAt")
    closed_at: str | None = Field(None, alias="closedAt")
    labels: list[Any] = Field(default_factory=list)
    comments: Any = 0
    url: str


class WriteResult(BaseModel):
    """Shared status for an external write and its optional readback."""

    write_completed: bool = True
    readback_completed: bool = True
    warning: str | None = None


class IssueCreate(WriteResult):
    """Result of creating an issue."""

    number: int
    title: str
    url: str
    message: str


# ---------------------------------------------------------------------------
# Pull request tools
# ---------------------------------------------------------------------------


class PullRequestInfo(IssueInfo):
    """A pull request with additional fields."""

    model_config = ConfigDict(populate_by_name=True)

    head_ref: str | None = Field(None, alias="headRefName")
    base_ref: str | None = Field(None, alias="baseRefName")
    is_draft: bool = Field(False, alias="isDraft")
    additions: int = 0
    deletions: int = 0
    changed_files: int = Field(0, alias="changedFiles")


class PullRequestCreate(WriteResult):
    """Result of creating a pull request."""

    number: int
    title: str
    url: str
    message: str


class PullRequestEdit(WriteResult):
    """Result of editing a pull request."""

    number: int
    title: str
    url: str
    message: str


# ---------------------------------------------------------------------------
# Repository tools
# ---------------------------------------------------------------------------


class RepoInfo(BaseModel):
    """A GitHub repository."""

    model_config = ConfigDict(populate_by_name=True)

    name_with_owner: str = Field(alias="nameWithOwner")
    name: str
    owner: str
    description: str | None = None
    url: str
    is_private: bool = Field(alias="isPrivate")
    is_fork: bool = Field(alias="isFork")
    primary_language: str | None = Field(None, alias="primaryLanguage")
    stargazers: int = Field(0, alias="stargazerCount")
    forks: int = Field(0, alias="forkCount")
    created_at: str | None = Field(None, alias="createdAt")
    pushed_at: str | None = Field(None, alias="pushedAt")
    default_branch: str | None = Field(None, alias="defaultBranchRef")
    license: Any | None = Field(None, alias="licenseInfo")


class RepoCreate(WriteResult):
    """Result of creating a repository."""

    name: str
    url: str
    message: str


class RepositoryFile(BaseModel):
    """Complete contents and metadata for one repository file."""

    path: str
    ref: str
    sha: str
    size: int
    content: str
    encoding: Literal["utf-8", "base64"]


class CommitFile(BaseModel):
    """One file to create or replace in an atomic commit."""

    path: str = Field(
        description="Repository-relative path to create or replace.",
        min_length=1,
        max_length=4096,
    )
    content: str = Field(description="Complete UTF-8 contents for the file.")
    mode: Literal["100644", "100755", "120000"] = Field(
        default="100644",
        description="Git file mode: regular, executable, or symbolic link.",
    )


class CommitFilesResult(WriteResult):
    """Result of creating a commit and compare-and-swap updating a branch."""

    branch: str
    previous_head_sha: str
    commit_sha: str | None = None
    tree_sha: str | None = None
    ref_updated: bool | None = False
    files_committed: int = 0
    url: str = ""
    message: str


class BranchCreate(WriteResult):
    """Result of creating a branch."""

    name: str
    message: str


# ---------------------------------------------------------------------------
# Release tools
# ---------------------------------------------------------------------------


class ReleaseInfo(BaseModel):
    """A release."""

    model_config = ConfigDict(populate_by_name=True)

    tag_name: str = Field(alias="tagName")
    name: str | None = None
    url: str
    is_draft: bool = Field(False, alias="isDraft")
    is_prerelease: bool = Field(False, alias="isPrerelease")
    created_at: str | None = Field(None, alias="createdAt")
    published_at: str | None = Field(None, alias="publishedAt")


class ReleaseCreate(WriteResult):
    """Result of creating a release."""

    tag_name: str
    url: str
    message: str


# ---------------------------------------------------------------------------
# Workflow tools
# ---------------------------------------------------------------------------


class WorkflowInfo(BaseModel):
    """A GitHub Actions workflow."""

    id: int
    name: str
    path: str
    state: str  # active, disabled, disabled_fork, disabled_inactivity


class WorkflowRun(BaseModel):
    """A GitHub Actions workflow run."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(alias="databaseId")
    name: str | None = None
    display_title: str = Field("", alias="displayTitle")
    head_branch: str | None = Field(None, alias="headBranch")
    head_sha: str | None = Field(None, alias="headSha")
    path: str | None = None
    conclusion: str | None = None
    status: str | None = None
    event: str | None = None
    url: str | None = None
    created_at: str | None = Field(None, alias="createdAt")
    updated_at: str | None = Field(None, alias="updatedAt")
    started_at: str | None = Field(None, alias="startedAt")
    workflow_name: str | None = Field(None, alias="workflowName")


class WorkflowRunCreate(WriteResult):
    """Result of triggering a workflow dispatch."""

    run_id: int | None = None
    url: str | None = None
    message: str


class WorkflowRunWatchResult(BaseModel):
    """Result of watching a workflow run until completion."""

    run_id: int
    conclusion: str | None = None
    status: str | None = None
    url: str | None = None
    message: str


# ---------------------------------------------------------------------------
# Issue edit model
# ---------------------------------------------------------------------------


class IssueEdit(WriteResult):
    """Result of editing an issue."""

    number: int
    title: str
    state: str
    url: str
    message: str


class CommentCreate(WriteResult):
    """Result of creating a comment."""

    url: str
    message: str


# ---------------------------------------------------------------------------
# Label models
# ---------------------------------------------------------------------------


class LabelInfo(BaseModel):
    """A repository label."""

    name: str
    color: str = ""
    description: str | None = None
    is_default: bool = False
    created_at: str | None = Field(None, alias="createdAt")
    updated_at: str | None = Field(None, alias="updatedAt")
    url: str


class LabelCreate(WriteResult):
    """Result of creating a label."""

    name: str
    color: str = ""
    description: str | None = None
    url: str
    message: str


class LabelEdit(WriteResult):
    """Result of editing a label."""

    name: str
    color: str = ""
    description: str | None = None
    url: str
    message: str


# ---------------------------------------------------------------------------
# Milestone models
# ---------------------------------------------------------------------------


class MilestoneInfo(BaseModel):
    """A repository milestone."""

    number: int
    title: str
    description: str | None = None
    state: str = "open"
    creator: str | None = None
    open_issues: int = 0
    closed_issues: int = 0
    created_at: str | None = Field(None, alias="createdAt")
    updated_at: str | None = Field(None, alias="updatedAt")
    closed_at: str | None = Field(None, alias="closedAt")
    due_on: str | None = Field(None, alias="dueOn")
    url: str


class MilestoneCreate(WriteResult):
    """Result of creating a milestone."""

    number: int
    title: str
    url: str
    message: str
