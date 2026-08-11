"""Repository metadata, content, and atomic content-commit tools."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..models import (
    CommitFile,
    CommitFilesResult,
    RepoCreate,
    RepoInfo,
    RepositoryFile,
    SearchResults,
)
from ..tooling import (
    ADD_EXTERNAL,
    MUTATE_EXTERNAL,
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    api_json_write,
    app_from_context,
    logger,
    mcp,
    readback_warning,
    require_action_enabled,
    require_write_enabled,
    validate_branch,
    validate_repo_path,
    validate_repository,
)


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


@mcp.tool(
    title="Commit repository files atomically",
    description=(
        "Write action: create or replace complete UTF-8 files in one Git commit and "
        "conditionally advance one branch only when its head matches expected_head_sha. "
        "This tool requires host approval and server-side content-commit authorization."
    ),
    annotations=MUTATE_EXTERNAL,
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
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="content_commit")
    validate_branch(branch)
    if not OBJECT_SHA_RE.fullmatch(expected_head_sha):
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
        validate_repo_path(file.path)
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
        blob = await api_json_write(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/git/blobs",
            {"content": file.content, "encoding": "utf-8"},
        )
        blob_sha = blob.get("sha")
        if not isinstance(blob_sha, str):
            raise RuntimeError(f"GitHub did not return a blob SHA for {file.path!r}")
        tree_entries.append({"path": file.path, "mode": file.mode, "type": "blob", "sha": blob_sha})

    tree = await api_json_write(
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

    commit = await api_json_write(
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
        updated_ref = await api_json_write(
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
        warning = readback_warning(f"Commit {commit_sha}", url or None)
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


@mcp.tool(annotations=ADD_EXTERNAL)
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

    app = app_from_context(ctx)
    require_action_enabled(app, "repo_create")
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
    require_write_enabled(app, owner, repo_name, action="repo_create")
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
        warning = readback_warning("Repository", full_name)
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
