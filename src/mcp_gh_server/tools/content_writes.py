"Canonical exact-outcome repository content write implementation."

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context
from pydantic import Field

from ..content_commit_service import commit_materialized_files, prepare_content_commit_base
from ..git_write_models import CommitFilesResult
from ..models import CommitFile
from ..tooling import (
    OBJECT_SHA_RE,
    AppContext,
    app_from_context,
    logger,
    require_write_enabled,
    validate_branch,
    validate_repo_path,
)


def _validate_commit_message(commit_message: str) -> None:
    if not commit_message.strip():
        raise ValueError("commit_message must not be empty")
    if len(commit_message.encode()) > 65_536:
        raise ValueError("commit_message exceeds 65536 UTF-8 bytes")


def _validate_materialized_files(app: AppContext, files: list[CommitFile]) -> None:
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
    _validate_commit_message(commit_message)
    _validate_materialized_files(app, files)

    base = await prepare_content_commit_base(
        app,
        owner,
        repo,
        branch,
        expected_head_sha,
    )
    committed = await commit_materialized_files(
        app,
        owner,
        repo,
        branch,
        base,
        files,
        commit_message,
    )

    files_committed = (
        len(files) if committed.outcome.state_matches_requested is True else 0
    )
    return CommitFilesResult(
        branch=branch,
        previous_head_sha=committed.previous_head_sha,
        commit_sha=committed.commit_sha,
        tree_sha=committed.tree_sha,
        ref_updated=committed.ref_updated,
        observed_head_sha=committed.observed_head_sha,
        readback_attempts=committed.readback_attempts,
        files_committed=files_committed,
        url=committed.url,
        message=committed.message,
        **committed.outcome.model_dump(),
    )
