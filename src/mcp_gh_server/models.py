"""Structured MCP tool result models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

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
    pr_merge_enabled: bool


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
    body: str | None = None
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
    body: str | None = None
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

    labels: list[str] = Field(default_factory=list)
    comments: int = Field(default=0, ge=0)
    head_ref: str | None = Field(None, alias="headRefName")
    base_ref: str | None = Field(None, alias="baseRefName")
    head_sha: str = Field(alias="headRefOid", pattern=r"^[0-9A-Fa-f]{40}$")
    base_sha: str = Field(alias="baseRefOid", pattern=r"^[0-9A-Fa-f]{40}$")
    is_draft: bool = Field(False, alias="isDraft")
    additions: int = 0
    deletions: int = 0
    changed_files: int = Field(0, alias="changedFiles")


class PullRequestDiff(BaseModel):
    """Bounded diff or patch for an immutable pull-request snapshot."""

    number: int
    base_sha: str
    head_sha: str
    format: Literal["diff", "patch"]
    content: str
    truncated: bool
    bytes_returned: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    sha256: str


class PullRequestFile(BaseModel):
    """One file changed by a pull request."""

    filename: str
    status: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changes: int = Field(ge=0)
    sha: str
    previous_filename: str | None = None
    patch: str | None = None
    patch_truncated: bool = False
    patch_bytes_returned: int = Field(default=0, ge=0)
    blob_url: str | None = None
    raw_url: str | None = None
    contents_url: str | None = None


class PullRequestFilesPage(BaseModel):
    """One bounded page of files for an immutable pull-request snapshot."""

    number: int
    base_sha: str
    head_sha: str
    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    has_more: bool
    files: list[PullRequestFile]


class PullRequestCommit(BaseModel):
    """One commit in a pull request."""

    sha: str
    message: str
    message_truncated: bool = False
    message_bytes_returned: int = Field(default=0, ge=0)
    author_login: str | None = None
    author_name: str | None = None
    authored_at: str | None = None
    committer_login: str | None = None
    committed_at: str | None = None
    url: str


class PullRequestCommitsPage(BaseModel):
    """One bounded page of commits for an immutable pull-request snapshot."""

    number: int
    base_sha: str
    head_sha: str
    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    has_more: bool
    commits: list[PullRequestCommit]


class PullRequestReviewSubmission(WriteResult):
    """Result of submitting a formal pull-request review."""

    number: int
    review_id: int = Field(ge=0)
    action: Literal["approve", "request_changes", "comment"]
    state: str
    body: str
    author: str | None = None
    submitted_at: str | None = None
    commit_sha: str
    url: str
    message: str


class PullRequestMerge(WriteResult):
    """Result of an exact-head pull-request merge command."""

    number: int
    method: Literal["merge", "squash", "rebase"]
    head_sha: str
    state: str
    merged: bool
    merge_queued: bool = False
    auto_merge_enabled: bool = False
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    merge_state_status: str | None = None
    url: str
    message: str


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


GitObjectType = Literal["commit", "tag", "tree", "blob"]


class GitRefInput(BaseModel):
    """Validated request for one exact branch or tag reference."""

    owner: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
    )
    repo: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_.-]{1,100}$",
    )
    ref: str = Field(
        description=(
            "Exact Git reference path relative to refs/, formatted as heads/<branch> or tags/<tag>."
        ),
        min_length=6,
        max_length=1024,
        pattern=r"^(?:heads|tags)/.+$",
    )

    @field_validator("ref")
    @classmethod
    def require_exact_branch_or_tag_ref(cls, value: str) -> str:
        """Reject malformed or matching-pattern-like ref input before GitHub is contacted."""

        suffix = value.split("/", 1)[1] if "/" in value else ""
        components = suffix.split("/") if suffix else []
        invalid_character = any(
            ord(character) <= 32
            or ord(character) == 127
            or character in {"~", "^", ":", "?", "*", "[", "\\"}
            for character in value
        )
        if (
            not (value.startswith("heads/") or value.startswith("tags/"))
            or not suffix
            or len(value.encode()) > 1024
            or value.endswith(("/", "."))
            or ".." in value
            or "@{" in value
            or "//" in value
            or invalid_character
            or any(
                not component or component.startswith(".") or component.endswith(".lock")
                for component in components
            )
        ):
            raise ValueError(
                "ref must be one exact valid branch or tag path relative to refs/, such as "
                "'heads/main' or 'tags/v1.0.0'"
            )
        return value


class GitRefInfo(BaseModel):
    """Exact Git reference identity and optional annotated-tag peel result."""

    ref: str = Field(pattern=r"^refs/(?:heads|tags)/.+$")
    found: bool
    object_type: GitObjectType | None = None
    object_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    object_url: str | None = Field(default=None, min_length=1)
    peeled_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class GitCommitInput(BaseModel):
    """Validated request for one exact Git commit object."""

    owner: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
    )
    repo: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_.-]{1,100}$",
    )
    commit_sha: str = Field(
        description="Exact 40-character hexadecimal Git commit SHA.",
        pattern=r"^[0-9A-Fa-f]{40}$",
    )


class GitCommitPerson(BaseModel):
    """Git commit author or committer identity reported by the Git database route."""

    name: str
    email: str
    date: str


class GitCommitVerification(BaseModel):
    """GitHub signature-verification metadata preserved without reinterpretation."""

    verified: bool
    reason: str
    signature: str | None = None
    payload: str | None = None
    verified_at: str | None = None


class GitCommitInfo(BaseModel):
    """Exact immutable Git commit identity and verification evidence."""

    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    found: bool
    tree_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    parents: list[str] = Field(default_factory=list)
    author: GitCommitPerson | None = None
    committer: GitCommitPerson | None = None
    message: str | None = None
    verification: GitCommitVerification | None = None


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


class BranchCreateFromSha(WriteResult):
    """Result of creating a branch at an immutable commit."""

    name: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str
    created: bool
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


class PullRequestCheck(BaseModel):
    """One CI check reported for an immutable pull-request snapshot."""

    name: str
    state: str
    bucket: Literal["pass", "fail", "pending", "skipping", "cancel"]
    workflow: str | None = None
    event: str | None = None
    description: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    link: str | None = None


class PullRequestChecks(BaseModel):
    """Bounded CI check summary for an immutable pull-request snapshot."""

    number: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9A-Fa-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9A-Fa-f]{40}$")
    total_count: int = Field(ge=0)
    truncated: bool
    checks: list[PullRequestCheck]


class WorkflowJobStep(BaseModel):
    """One step in a GitHub Actions workflow job."""

    number: int = Field(ge=1)
    name: str
    status: str
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class WorkflowJob(BaseModel):
    """One GitHub Actions job with its bounded step metadata."""

    id: int = Field(ge=1)
    name: str
    status: str
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    url: str | None = None
    runner_name: str | None = None
    steps: list[WorkflowJobStep] = Field(default_factory=list)


class WorkflowJobsPage(BaseModel):
    """One bounded page of jobs for an exact workflow-run attempt."""

    run_id: int = Field(ge=1)
    attempt: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9A-Fa-f]{40}$")
    page: int = Field(ge=1)
    per_page: int = Field(ge=1, le=100)
    total_count: int = Field(ge=0)
    has_more: bool
    jobs: list[WorkflowJob]


class WorkflowRunFailedLogs(BaseModel):
    """Bounded failed-step logs for one exact workflow-run attempt."""

    run_id: int = Field(ge=1)
    attempt: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9A-Fa-f]{40}$")
    status: str
    conclusion: str | None = None
    url: str | None = None
    content: str
    truncated: bool
    bytes_returned: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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
