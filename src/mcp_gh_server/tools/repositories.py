"""Repository metadata and content read tools."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..gh_client import GhClient
from ..models import RepoInfo, RepositoryFile, SearchResults
from ..repository_tree_models import (
    RepositoryTreeEntry,
    RepositoryTreeMode,
    RepositoryTreeObjectType,
    RepositoryTreeRequest,
    RepositoryTreeResult,
)
from ..tooling import (
    OBJECT_SHA_RE,
    OWNER_RE,
    READ_EXTERNAL,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repo_directory_path,
    validate_repo_path,
    validate_repository,
)
from .git import exact_commit_tree_sha, read_exact_commit_payload

_TREE_MODES_BY_TYPE: dict[str, frozenset[str]] = {
    "blob": frozenset({"100644", "100755", "120000"}),
    "tree": frozenset({"040000"}),
    "commit": frozenset({"160000"}),
}


def _repository_tree_entry(
    raw: object,
    *,
    path_prefix: str,
) -> RepositoryTreeEntry:
    """Validate one Git Trees API entry and normalize it to a repository-relative path."""

    if not isinstance(raw, dict):
        raise RuntimeError("GitHub returned a malformed Git tree entry")
    relative_path = raw.get("path")
    object_type = raw.get("type")
    mode = raw.get("mode")
    sha = raw.get("sha")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path.encode()) > 4096
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(ord(character) < 32 or ord(character) == 127 for character in relative_path)
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        or not isinstance(object_type, str)
        or object_type not in _TREE_MODES_BY_TYPE
        or not isinstance(mode, str)
        or mode not in _TREE_MODES_BY_TYPE[object_type]
        or not isinstance(sha, str)
        or not OBJECT_SHA_RE.fullmatch(sha)
    ):
        raise RuntimeError("GitHub returned a malformed Git tree entry")

    size = raw.get("size") if object_type == "blob" else None
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise RuntimeError("GitHub returned a malformed Git tree entry blob size")

    repository_path = f"{path_prefix}/{relative_path}" if path_prefix else relative_path
    if len(repository_path.encode()) > 4096:
        raise RuntimeError("GitHub returned a Git tree path beyond the repository path bound")
    return RepositoryTreeEntry(
        path=repository_path,
        name=repository_path.rsplit("/", 1)[-1],
        type=cast(RepositoryTreeObjectType, object_type),
        mode=cast(RepositoryTreeMode, mode),
        sha=sha.casefold(),
        size=size,
    )


async def _read_repository_tree(
    client: GhClient,
    owner: str,
    repo: str,
    tree_sha: str,
    *,
    path_prefix: str,
    recursive: bool,
) -> tuple[list[RepositoryTreeEntry], bool]:
    """Read and validate one exact Git tree object."""

    args = [
        "api",
        f"repos/{owner}/{repo}/git/trees/{tree_sha}",
        "-X",
        "GET",
    ]
    if recursive:
        args.extend(["-f", "recursive=1"])
    payload = await client.run(*args)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned a non-object Git tree response")
    returned_sha = payload.get("sha")
    if (
        not isinstance(returned_sha, str)
        or not OBJECT_SHA_RE.fullmatch(returned_sha)
        or returned_sha.casefold() != tree_sha
    ):
        raise RuntimeError(
            "GitHub Git tree read did not preserve the requested tree SHA; "
            "refusing ambiguous evidence"
        )
    raw_entries = payload.get("tree")
    source_truncated = payload.get("truncated")
    if not isinstance(raw_entries, list) or not isinstance(source_truncated, bool):
        raise RuntimeError("GitHub returned malformed Git tree completeness evidence")
    return (
        [_repository_tree_entry(entry, path_prefix=path_prefix) for entry in raw_entries],
        source_truncated,
    )


async def _resolve_repository_directory_tree(
    client: GhClient,
    owner: str,
    repo: str,
    root_tree_sha: str,
    path: str,
) -> str:
    """Traverse exact tree objects to resolve one normalized repository directory."""

    if path == "":
        return root_tree_sha

    current_tree_sha = root_tree_sha
    current_path = ""
    for component in path.split("/"):
        entries, truncated = await _read_repository_tree(
            client,
            owner,
            repo,
            current_tree_sha,
            path_prefix=current_path,
            recursive=False,
        )
        if truncated:
            raise RuntimeError(
                "GitHub reported truncated directory traversal evidence; "
                "refusing ambiguous path resolution"
            )
        wanted_path = f"{current_path}/{component}" if current_path else component
        matches = [entry for entry in entries if entry.path == wanted_path]
        if not matches:
            raise ValueError(f"repository directory component not found: {wanted_path!r}")
        if len(matches) != 1:
            raise RuntimeError(f"GitHub returned duplicate Git tree entries for {wanted_path!r}")
        match = matches[0]
        if match.type != "tree":
            raise ValueError(
                "repository path is not a directory: "
                f"{wanted_path!r} (Git object type {match.type})"
            )
        current_tree_sha = match.sha
        current_path = wanted_path
    return current_tree_sha


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
    title="List exact repository tree",
    description=(
        "Read-only: list one repository directory tree at an exact 40-character commit SHA. "
        "The tool supports immediate or recursive structural discovery, returns bounded exact "
        "Git object evidence with explicit completeness metadata, and never reads file content "
        "or modifies GitHub."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_repository_tree(
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner or organization login.",
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
        ),
    ],
    repo: Annotated[
        str,
        Field(
            description="GitHub repository name without the owner prefix.",
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
        ),
    ],
    commit_sha: Annotated[
        str,
        Field(
            description="Exact 40-character hexadecimal Git commit SHA.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    path: Annotated[
        str,
        Field(
            description=(
                "Normalized repository-relative directory path; empty means repository root."
            ),
            max_length=4096,
        ),
    ] = "",
    recursive: Annotated[
        bool,
        Field(description="Whether to list all descendants of the selected directory tree."),
    ] = False,
    max_entries: Annotated[
        int | None,
        Field(
            description="Requested result-entry cap, additionally bounded by server policy.",
            ge=1,
        ),
    ] = None,
    *,
    ctx: Context[AppContext],
) -> RepositoryTreeResult:
    """List bounded exact Git tree evidence without mutable-ref resolution."""

    logger.info("MCP tool invocation reached server: tool=gh_list_repository_tree")
    request = RepositoryTreeRequest(
        owner=owner,
        repo=repo,
        commit_sha=commit_sha,
        path=path,
        recursive=recursive,
        max_entries=max_entries,
    )
    validate_repository(request.owner, request.repo)
    validate_repo_directory_path(request.path)
    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(request.max_entries)

    normalized_sha, commit_payload = await read_exact_commit_payload(
        app.client,
        request.owner,
        request.repo,
        request.commit_sha,
    )
    if commit_payload is None:
        raise ValueError(f"repository commit not found: {normalized_sha}")
    root_tree_sha = exact_commit_tree_sha(commit_payload)
    directory_tree_sha = await _resolve_repository_directory_tree(
        app.client,
        request.owner,
        request.repo,
        root_tree_sha,
        request.path,
    )
    entries, source_truncated = await _read_repository_tree(
        app.client,
        request.owner,
        request.repo,
        directory_tree_sha,
        path_prefix=request.path,
        recursive=request.recursive,
    )

    bound_truncated = len(entries) > limit
    returned_entries = entries[:limit]
    warnings: list[str] = []
    if bound_truncated:
        warnings.append(
            f"Result exceeded the applied max_entries bound of {limit}; "
            "additional GitHub tree entries were omitted."
        )
    if source_truncated:
        warnings.append(
            "GitHub reported truncated tree evidence; the repository structure is incomplete."
        )
    truncated = bound_truncated or source_truncated
    return RepositoryTreeResult(
        commit_sha=normalized_sha,
        root_tree_sha=root_tree_sha,
        path=request.path,
        directory_tree_sha=directory_tree_sha,
        recursive=request.recursive,
        entries=returned_entries,
        entries_returned=len(returned_entries),
        truncated=truncated,
        evidence_complete=not truncated,
        warning=" ".join(warnings) or None,
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
