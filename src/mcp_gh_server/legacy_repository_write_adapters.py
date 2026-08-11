"""0.6.x compatibility adapters for repository writes."""

from __future__ import annotations

import logging
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from .legacy_write_support import (
    execute_atomic_write_readback,
    raise_known_unapplied,
    run_json_write_with_metadata,
    run_write_with_metadata,
)
from .models import CommitFile, CommitFilesResult, RepoCreate
from .request_governor import GitHubRequestResult
from .tooling import (
    OBJECT_SHA_RE,
    AppContext,
    app_from_context,
    require_action_enabled,
    require_write_enabled,
    validate_branch,
    validate_repo_path,
)
from .write_contracts import combine_warnings, execute_write_readback, legacy_write_status

logger = logging.getLogger("mcp_gh_server.server")


def _metadata_note(result: GitHubRequestResult[Any]) -> str | None:
    warning = result.metadata.warning
    if result.metadata.request_id is not None:
        warning = combine_warnings(
            warning,
            f"GitHub request id: {result.metadata.request_id}.",
        )
    return warning


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
    """Create one commit and atomically advance an unchanged branch head."""

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

    sequence_warnings: list[str] = []
    tree_entries: list[dict[str, str]] = []
    for file in files:
        blob_result = await run_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/git/blobs",
            {"content": file.content, "encoding": "utf-8"},
        )
        blob_note = _metadata_note(blob_result)
        if blob_note is not None:
            sequence_warnings.append(blob_note)
        blob = blob_result.value
        blob_sha = blob.get("sha") if isinstance(blob, dict) else None
        if not isinstance(blob_sha, str):
            raise RuntimeError(f"GitHub did not return a blob SHA for {file.path!r}")
        tree_entries.append({"path": file.path, "mode": file.mode, "type": "blob", "sha": blob_sha})

    tree_result = await run_json_write_with_metadata(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/trees",
        {"base_tree": base_tree_sha, "tree": tree_entries},
    )
    tree_note = _metadata_note(tree_result)
    if tree_note is not None:
        sequence_warnings.append(tree_note)
    tree = tree_result.value
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str):
        raise RuntimeError("GitHub did not return the newly created tree SHA")
    if tree_sha == base_tree_sha:
        raise ValueError("the supplied files do not change the branch tree")

    commit_result = await run_json_write_with_metadata(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/commits",
        {"message": commit_message, "tree": tree_sha, "parents": [actual_head_sha]},
    )
    commit_note = _metadata_note(commit_result)
    if commit_note is not None:
        sequence_warnings.append(commit_note)
    commit = commit_result.value
    commit_sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(commit_sha, str):
        raise RuntimeError("GitHub did not return the newly created commit SHA")
    commit_url = commit.get("html_url") if isinstance(commit, dict) else None
    url = commit_url if isinstance(commit_url, str) else ""

    cas_payload = {
        "query": (
            "mutation($input: UpdateRefsInput!) { updateRefs(input: $input) { clientMutationId } }"
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
    }

    async def write_ref() -> GitHubRequestResult[Any]:
        return await run_json_write_with_metadata(
            app.client,
            "POST",
            "graphql",
            cas_payload,
        )

    async def readback_ref() -> dict[str, Any]:
        result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
        )
        if not isinstance(result, dict):
            raise RuntimeError("GitHub returned a non-object branch ref readback")
        return result

    def ref_matches(result: dict[str, Any]) -> bool:
        obj = result.get("object")
        sha = obj.get("sha") if isinstance(obj, dict) else None
        return isinstance(sha, str) and sha.casefold() == commit_sha.casefold()

    execution = await execute_atomic_write_readback(
        resource="Repository content commit",
        write=write_ref,
        readback=readback_ref,
        state_matches_requested=ref_matches,
    )
    outcome = execution.outcome
    status = legacy_write_status(outcome)

    readback_sha: str | None = None
    if execution.readback_value is not None:
        obj = execution.readback_value.get("object")
        value = obj.get("sha") if isinstance(obj, dict) else None
        readback_sha = value if isinstance(value, str) else None

    warning = combine_warnings(*sequence_warnings, status.warning)
    if outcome.readback_completed and outcome.state_matches_requested is False:
        if readback_sha == actual_head_sha:
            branch_status = "The branch head is unchanged."
        elif readback_sha is not None:
            branch_status = f"The branch now points to a different commit, {readback_sha}."
        else:
            branch_status = "The branch head could not be interpreted."
        warning = combine_warnings(
            warning,
            (
                f"Commit object {commit_sha} was created, but it was not installed on branch "
                f"{branch!r}. {branch_status} Do not retry automatically; "
                "re-read the branch first."
            ),
        )

    if outcome.state_matches_requested is True:
        ref_updated: bool | None = True
        files_committed = len(files)
        base_message = f"Committed {len(files)} file(s) to {branch}."
    elif outcome.readback_completed:
        ref_updated = False
        files_committed = 0
        base_message = f"Commit {commit_sha} was created but not installed on {branch}."
    else:
        ref_updated = None
        files_committed = 0
        base_message = (
            f"Commit object {commit_sha} was created, but the branch update could not be verified."
        )

    message = warning or base_message
    return CommitFilesResult(
        branch=branch,
        previous_head_sha=actual_head_sha,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        ref_updated=ref_updated,
        files_committed=files_committed,
        url=url,
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=warning,
        message=message,
    )


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

    args = ["repo", "create", full_name, "--private" if private else "--public"]
    if description:
        args.extend(["--description", description])
    if auto_init:
        args.append("--add-readme")

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(app.client, *args, json_output=False)

    async def readback() -> dict[str, Any]:
        value = await app.client.run(
            "repo",
            "view",
            full_name,
            "--json",
            "nameWithOwner,url",
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object repository readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        # The frozen 0.6.x repository readback exposes only canonical identity.
        # New 0.7.x create tools must expose and verify every requested property.
        return value.get("nameWithOwner") == full_name and bool(value.get("url"))

    execution = await execute_write_readback(
        resource="Repository creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    value = execution.readback_value or {}
    message = status.warning or "Repository created successfully."
    return RepoCreate(
        name=str(value.get("nameWithOwner") or full_name),
        url=str(value.get("url") or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
