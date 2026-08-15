"""Canonical exact-outcome repository content write implementation."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..git_write_models import CommitFilesResult
from ..models import CommitFile
from ..request_governor import GitHubRequestError, GitHubRequestResult
from ..tooling import (
    OBJECT_SHA_RE,
    AppContext,
    app_from_context,
    logger,
    require_write_enabled,
    validate_branch,
    validate_repo_path,
)
from ..write_contracts import (
    combine_warnings,
    execute_write_readback,
    run_api_json_write_with_metadata,
)

_REF_READBACK_MAX_ATTEMPTS = 3
_REF_READBACK_BACKOFF_SECONDS = (0.05, 0.10)


class _InconclusiveRefReadback(RuntimeError):
    """Bounded exact-ref reconciliation observed only the expected old head."""


def _metadata_note(result: GitHubRequestResult[Any]) -> str | None:
    warning = result.metadata.warning
    if result.metadata.request_id is not None:
        warning = combine_warnings(
            warning,
            f"GitHub request id: {result.metadata.request_id}.",
        )
    return warning


def _parse_exact_ref_sha(result: object, branch: str) -> str:
    if not isinstance(result, dict):
        raise RuntimeError("GitHub returned a non-object branch ref readback")
    obj = result.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not OBJECT_SHA_RE.fullmatch(sha):
        raise RuntimeError(f"GitHub did not return an exact head SHA for branch {branch!r}")
    return sha.casefold()


def _reconciliation_warning(
    *,
    branch: str,
    previous_head_sha: str,
    commit_sha: str,
    write_completed: bool | None,
    readback_completed: bool,
    state_matches_requested: bool | None,
    observed_head_sha: str | None,
    readback_attempts: int,
    reconciliation_exhausted: bool,
    readback_failed: bool,
) -> str | None:
    if state_matches_requested is True:
        if write_completed is None:
            return (
                "Repository content commit CAS transport outcome is unknown, but bounded "
                f"exact-ref reconciliation verified branch {branch!r} at created commit "
                f"{commit_sha}. Do not retry the mutation."
            )
        if write_completed is False:
            return (
                "Repository content commit CAS was not confirmed complete, but bounded "
                f"exact-ref reconciliation verified branch {branch!r} at created commit "
                f"{commit_sha}. Do not retry the mutation."
            )
        return None

    if readback_completed and state_matches_requested is False:
        observed = observed_head_sha or "an unrecognized head"
        prefix = (
            "Repository content commit CAS transport outcome is unknown"
            if write_completed is None
            else (
                "Repository content commit CAS completed"
                if write_completed is True
                else "Repository content commit CAS was not confirmed complete"
            )
        )
        return (
            f"{prefix}, and authoritative exact-ref reconciliation observed branch "
            f"{branch!r} at distinct commit {observed}, not created commit {commit_sha}. "
            "This is a conclusive semantic mismatch. Do not retry automatically; re-read "
            "authoritative state first."
        )

    if reconciliation_exhausted and observed_head_sha == previous_head_sha:
        prefix = (
            "Repository content commit CAS transport outcome is unknown"
            if write_completed is None
            else (
                "Repository content commit CAS completed"
                if write_completed is True
                else "Repository content commit CAS was not confirmed complete"
            )
        )
        return (
            f"{prefix}, but bounded exact-ref reconciliation remained inconclusive after "
            f"{readback_attempts} attempt(s): branch {branch!r} was still observed at the "
            f"pre-write head {previous_head_sha}. The branch update is unresolved, not "
            "disproven. Do not retry automatically; re-read authoritative state first."
        )

    if readback_failed:
        prefix = (
            "Repository content commit CAS transport outcome is unknown"
            if write_completed is None
            else (
                "Repository content commit CAS completed"
                if write_completed is True
                else "Repository content commit CAS was not confirmed complete"
            )
        )
        observation = (
            f" The last valid observed head was {observed_head_sha}."
            if observed_head_sha is not None
            else ""
        )
        return (
            f"{prefix}, but authoritative exact-ref reconciliation failed on readback "
            f"attempt {readback_attempts}.{observation} The branch update remains "
            "unverified. Do not retry automatically; re-read authoritative state first."
        )

    return (
        "Repository content commit exact-ref reconciliation did not establish a conclusive "
        "postcondition. Do not retry automatically; re-read authoritative state first."
    )


async def gh_commit_files(
    owner: Annotated[str, Field(min_length=1)],
    repo: Annotated[str, Field(min_length=1)],
    branch: Annotated[str, Field(min_length=1)],
    expected_head_sha: Annotated[str, Field(pattern=r"^[0-9A-Fa-f]{40}$")],
    files: Annotated[list[CommitFile], Field(min_length=1, max_length=1000)],
    commit_message: Annotated[str, Field(min_length=1, max_length=65_536)],
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

    normalized_expected_head = expected_head_sha.casefold()
    branch_path = quote(branch, safe="/")
    ref_result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
    )
    ref_object = ref_result.get("object") if isinstance(ref_result, dict) else None
    actual_head_sha = ref_object.get("sha") if isinstance(ref_object, dict) else None
    if not isinstance(actual_head_sha, str) or not OBJECT_SHA_RE.fullmatch(actual_head_sha):
        raise RuntimeError(f"GitHub did not return the current head of branch {branch!r}")
    actual_head_sha = actual_head_sha.casefold()
    if actual_head_sha != normalized_expected_head:
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
    if not isinstance(base_tree_sha, str) or not OBJECT_SHA_RE.fullmatch(base_tree_sha):
        raise RuntimeError(f"GitHub did not return the tree for commit {actual_head_sha}")
    base_tree_sha = base_tree_sha.casefold()

    sequence_warnings: list[str] = []
    tree_entries: list[dict[str, str]] = []
    for file in files:
        blob_result = await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/git/blobs",
            {"content": file.content, "encoding": "utf-8"},
        )
        note = _metadata_note(blob_result)
        if note is not None:
            sequence_warnings.append(note)
        blob = blob_result.value
        blob_sha = blob.get("sha") if isinstance(blob, dict) else None
        if not isinstance(blob_sha, str) or not OBJECT_SHA_RE.fullmatch(blob_sha):
            raise RuntimeError(f"GitHub did not return a blob SHA for {file.path!r}")
        tree_entries.append(
            {
                "path": file.path,
                "mode": file.mode,
                "type": "blob",
                "sha": blob_sha.casefold(),
            }
        )

    tree_result = await run_api_json_write_with_metadata(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/trees",
        {"base_tree": base_tree_sha, "tree": tree_entries},
    )
    note = _metadata_note(tree_result)
    if note is not None:
        sequence_warnings.append(note)
    tree = tree_result.value
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or not OBJECT_SHA_RE.fullmatch(tree_sha):
        raise RuntimeError("GitHub did not return the newly created tree SHA")
    tree_sha = tree_sha.casefold()
    if tree_sha == base_tree_sha:
        raise ValueError("the supplied files do not change the branch tree")

    commit_result = await run_api_json_write_with_metadata(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/commits",
        {"message": commit_message, "tree": tree_sha, "parents": [actual_head_sha]},
    )
    note = _metadata_note(commit_result)
    if note is not None:
        sequence_warnings.append(note)
    commit = commit_result.value
    commit_sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(commit_sha, str) or not OBJECT_SHA_RE.fullmatch(commit_sha):
        raise RuntimeError("GitHub did not return the newly created commit SHA")
    commit_sha = commit_sha.casefold()
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

    cas_metadata_warning: str | None = None

    async def write_ref() -> GitHubRequestResult[Any]:
        nonlocal cas_metadata_warning
        try:
            result = await run_api_json_write_with_metadata(
                app.client,
                "POST",
                "graphql",
                cas_payload,
            )
        except GitHubRequestError as exc:
            cas_metadata_warning = exc.metadata.warning
            raise
        cas_metadata_warning = result.metadata.warning
        value = result.value
        if isinstance(value, dict) and value.get("errors"):
            raise GitHubRequestError(
                "GitHub GraphQL returned mutation errors during exact ref update",
                ambiguous=True,
                metadata=result.metadata,
            )
        return result

    readback_attempts = 0
    observed_head_sha: str | None = None
    reconciliation_exhausted = False
    readback_failed = False

    async def readback_ref() -> str:
        nonlocal readback_attempts
        nonlocal observed_head_sha
        nonlocal reconciliation_exhausted
        nonlocal readback_failed

        for attempt in range(1, _REF_READBACK_MAX_ATTEMPTS + 1):
            readback_attempts = attempt
            try:
                result = await app.client.run(
                    "api",
                    f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
                )
                observed_head_sha = _parse_exact_ref_sha(result, branch)
            except RuntimeError:
                readback_failed = True
                raise

            if observed_head_sha != actual_head_sha:
                return observed_head_sha

            if attempt == _REF_READBACK_MAX_ATTEMPTS:
                reconciliation_exhausted = True
                raise _InconclusiveRefReadback(
                    f"branch {branch!r} remained at the pre-write head "
                    f"{actual_head_sha} through {_REF_READBACK_MAX_ATTEMPTS} exact-ref reads"
                )

            await asyncio.sleep(_REF_READBACK_BACKOFF_SECONDS[attempt - 1])

        raise AssertionError("bounded exact-ref reconciliation loop did not terminate")

    def ref_matches(readback_sha: str) -> bool:
        return readback_sha == commit_sha

    execution = await execute_write_readback(
        resource="Repository content commit",
        write=write_ref,
        readback=readback_ref,
        state_matches_requested=ref_matches,
    )
    outcome = execution.outcome.model_copy(update={"precondition_checked": True})

    final_request_note = (
        f"GitHub request id: {outcome.request_id}." if outcome.request_id is not None else None
    )
    reconciliation_warning = _reconciliation_warning(
        branch=branch,
        previous_head_sha=actual_head_sha,
        commit_sha=commit_sha,
        write_completed=outcome.write_completed,
        readback_completed=outcome.readback_completed,
        state_matches_requested=outcome.state_matches_requested,
        observed_head_sha=observed_head_sha,
        readback_attempts=readback_attempts,
        reconciliation_exhausted=reconciliation_exhausted,
        readback_failed=readback_failed,
    )
    warning = combine_warnings(
        *sequence_warnings,
        cas_metadata_warning,
        reconciliation_warning,
        final_request_note,
    )

    if outcome.state_matches_requested is True:
        ref_updated: bool | None = True
        files_committed = len(files)
        base_message = f"Committed {len(files)} file(s) to {branch}."
    elif outcome.readback_completed and outcome.state_matches_requested is False:
        ref_updated = False
        files_committed = 0
        base_message = (
            f"Commit object {commit_sha} was created, but branch {branch!r} was "
            f"authoritatively observed at distinct commit {observed_head_sha}."
        )
    else:
        ref_updated = None
        files_committed = 0
        if reconciliation_exhausted and observed_head_sha == actual_head_sha:
            base_message = (
                f"Commit object {commit_sha} was created, but branch {branch!r} remained "
                f"unresolved after {readback_attempts} exact-ref readback attempt(s); "
                f"the final observation was the pre-write head {actual_head_sha}."
            )
        else:
            base_message = (
                f"Commit object {commit_sha} was created, but the branch update could not "
                f"be verified after {readback_attempts} exact-ref readback attempt(s)."
            )

    outcome_data = outcome.model_dump()
    outcome_data["warning"] = warning
    return CommitFilesResult(
        branch=branch,
        previous_head_sha=actual_head_sha,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        ref_updated=ref_updated,
        observed_head_sha=observed_head_sha,
        readback_attempts=readback_attempts,
        files_committed=files_committed,
        url=url,
        message=combine_warnings(base_message, warning) or base_message,
        **outcome_data,
    )
