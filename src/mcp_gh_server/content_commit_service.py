"Shared materialized-content commit service for exact-head repository writes."

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .models import CommitFile
from .request_governor import GitHubRequestError, GitHubRequestResult
from .tooling import OBJECT_SHA_RE, AppContext
from .write_contracts import (
    WriteOutcomeMetadata,
    combine_warnings,
    execute_write_readback,
    run_api_json_write_with_metadata,
)

_REF_READBACK_MAX_ATTEMPTS = 3
_REF_READBACK_BACKOFF_SECONDS = (0.05, 0.10)


class _InconclusiveRefReadback(RuntimeError):
    """Bounded exact-ref reconciliation observed only the expected old head."""


@dataclass(frozen=True, slots=True)
class ContentCommitBase:
    """Immutable base evidence required to construct one content commit."""

    previous_head_sha: str
    repository_node_id: str
    base_tree_sha: str


@dataclass(frozen=True, slots=True)
class MaterializedCommitResult:
    """Internal result of creating one materialized content commit and advancing its ref."""

    branch: str
    previous_head_sha: str
    commit_sha: str
    tree_sha: str
    ref_updated: bool | None
    observed_head_sha: str | None
    readback_attempts: int
    url: str
    message: str
    outcome: WriteOutcomeMetadata
    blob_shas: dict[str, str]


def metadata_note(result: GitHubRequestResult[Any]) -> str | None:
    warning = result.metadata.warning
    if result.metadata.request_id is not None:
        warning = combine_warnings(
            warning,
            f"GitHub request id: {result.metadata.request_id}.",
        )
    return warning


def parse_exact_ref_sha(result: object, branch: str) -> str:
    if not isinstance(result, dict):
        raise RuntimeError("GitHub returned a non-object branch ref readback")
    obj = result.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not OBJECT_SHA_RE.fullmatch(sha):
        raise RuntimeError(f"GitHub did not return an exact head SHA for branch {branch!r}")
    return sha.casefold()


async def read_exact_branch_head(
    app: AppContext,
    owner: str,
    repo: str,
    branch: str,
) -> str:
    """Read and validate one exact branch head SHA."""

    branch_path = quote(branch, safe="/")
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
    )
    return parse_exact_ref_sha(result, branch)


async def prepare_content_commit_base(
    app: AppContext,
    owner: str,
    repo: str,
    branch: str,
    expected_head_sha: str,
) -> ContentCommitBase:
    """Require the exact branch head and resolve repository/base-tree identity without mutation."""

    normalized_expected_head = expected_head_sha.casefold()
    actual_head_sha = await read_exact_branch_head(app, owner, repo, branch)
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

    return ContentCommitBase(
        previous_head_sha=actual_head_sha,
        repository_node_id=repository_node_id,
        base_tree_sha=base_tree_sha.casefold(),
    )


def reconciliation_warning(
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
        if write_completed is False and observed_head_sha == previous_head_sha:
            return (
                "Repository content commit CAS failed before confirmed completion, and "
                f"authoritative exact-ref readback confirms branch {branch!r} head is "
                f"unchanged at {previous_head_sha}. Do not retry automatically; re-read "
                "authoritative state first."
            )
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


async def commit_materialized_files(
    app: AppContext,
    owner: str,
    repo: str,
    branch: str,
    base: ContentCommitBase,
    files: list[CommitFile],
    commit_message: str,
) -> MaterializedCommitResult:
    """Create materialized blobs/tree/commit, attempt one exact CAS, and reconcile read-only."""

    sequence_warnings: list[str] = []
    tree_entries: list[dict[str, str]] = []
    blob_shas: dict[str, str] = {}

    for file in files:
        blob_result = await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/git/blobs",
            {"content": file.content, "encoding": "utf-8"},
        )
        note = metadata_note(blob_result)
        if note is not None:
            sequence_warnings.append(note)
        blob = blob_result.value
        blob_sha = blob.get("sha") if isinstance(blob, dict) else None
        if not isinstance(blob_sha, str) or not OBJECT_SHA_RE.fullmatch(blob_sha):
            raise RuntimeError(f"GitHub did not return a blob SHA for {file.path!r}")
        normalized_blob_sha = blob_sha.casefold()
        blob_shas[file.path] = normalized_blob_sha
        tree_entries.append(
            {
                "path": file.path,
                "mode": file.mode,
                "type": "blob",
                "sha": normalized_blob_sha,
            }
        )

    tree_result = await run_api_json_write_with_metadata(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/trees",
        {"base_tree": base.base_tree_sha, "tree": tree_entries},
    )
    note = metadata_note(tree_result)
    if note is not None:
        sequence_warnings.append(note)
    tree = tree_result.value
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or not OBJECT_SHA_RE.fullmatch(tree_sha):
        raise RuntimeError("GitHub did not return the newly created tree SHA")
    tree_sha = tree_sha.casefold()
    if tree_sha == base.base_tree_sha:
        raise ValueError("the supplied files do not change the branch tree")

    commit_result = await run_api_json_write_with_metadata(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/git/commits",
        {
            "message": commit_message,
            "tree": tree_sha,
            "parents": [base.previous_head_sha],
        },
    )
    note = metadata_note(commit_result)
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
                "repositoryId": base.repository_node_id,
                "refUpdates": [
                    {
                        "name": f"refs/heads/{branch}",
                        "beforeOid": base.previous_head_sha,
                        "afterOid": commit_sha,
                        "force": False,
                    }
                ],
            }
        },
    }

    cas_metadata_warning: str | None = None
    cas_known_failed = False

    async def write_ref() -> GitHubRequestResult[Any]:
        nonlocal cas_metadata_warning
        nonlocal cas_known_failed
        try:
            result = await run_api_json_write_with_metadata(
                app.client,
                "POST",
                "graphql",
                cas_payload,
            )
        except RuntimeError as exc:
            if isinstance(exc, GitHubRequestError):
                cas_metadata_warning = exc.metadata.warning
                cas_known_failed = not exc.ambiguous
            else:
                cas_known_failed = True
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

    branch_path = quote(branch, safe="/")
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
                observed_head_sha = parse_exact_ref_sha(result, branch)
            except RuntimeError:
                readback_failed = True
                raise

            if observed_head_sha != base.previous_head_sha:
                return observed_head_sha

            if cas_known_failed:
                return observed_head_sha

            if attempt == _REF_READBACK_MAX_ATTEMPTS:
                reconciliation_exhausted = True
                raise _InconclusiveRefReadback(
                    f"branch {branch!r} remained at the pre-write head "
                    f"{base.previous_head_sha} through "
                    f"{_REF_READBACK_MAX_ATTEMPTS} exact-ref reads"
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
    reconcile_note = reconciliation_warning(
        branch=branch,
        previous_head_sha=base.previous_head_sha,
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
        reconcile_note,
        final_request_note,
    )
    outcome = outcome.model_copy(update={"warning": warning})

    if outcome.state_matches_requested is True:
        ref_updated: bool | None = True
        base_message = f"Committed {len(files)} file(s) to {branch}."
    elif outcome.readback_completed and outcome.state_matches_requested is False:
        ref_updated = False
        if outcome.write_completed is False and observed_head_sha == base.previous_head_sha:
            base_message = (
                f"Commit object {commit_sha} was created, but the branch CAS failed before "
                f"confirmed completion and branch {branch!r} head is unchanged at "
                f"{base.previous_head_sha}."
            )
        else:
            base_message = (
                f"Commit object {commit_sha} was created, but branch {branch!r} was "
                f"authoritatively observed at distinct commit {observed_head_sha}."
            )
    else:
        ref_updated = None
        if reconciliation_exhausted and observed_head_sha == base.previous_head_sha:
            base_message = (
                f"Commit object {commit_sha} was created, but branch {branch!r} remained "
                f"unresolved after {readback_attempts} exact-ref readback attempt(s); "
                f"the final observation was the pre-write head {base.previous_head_sha}."
            )
        else:
            base_message = (
                f"Commit object {commit_sha} was created, but the branch update could not "
                f"be verified after {readback_attempts} exact-ref readback attempt(s)."
            )

    return MaterializedCommitResult(
        branch=branch,
        previous_head_sha=base.previous_head_sha,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        ref_updated=ref_updated,
        observed_head_sha=observed_head_sha,
        readback_attempts=readback_attempts,
        url=url,
        message=combine_warnings(base_message, warning) or base_message,
        outcome=outcome,
        blob_shas=blob_shas,
    )
