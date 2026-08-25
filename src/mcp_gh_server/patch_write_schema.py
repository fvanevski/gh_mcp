"Host-facing facade for exact-context repository file patches."

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context
from pydantic import Field

from .patch_models import FilePatch, PatchFilesResult
from .tooling import MUTATE_EXTERNAL, AppContext
from .tools.patch_writes import gh_patch_files as _gh_patch_files
from .write_tool_schema import (
    BranchName,
    CommitMessage,
    ExactObjectSha,
    Owner,
    Repository,
    WriteToolMetadata,
)

PublicFilePatches = Annotated[
    list[FilePatch],
    Field(
        description=(
            "Existing UTF-8 files and bounded exact-context edits, all resolved against "
            "the original expected-head snapshot."
        ),
        min_length=1,
        max_length=1000,
    ),
]


async def gh_patch_files(
    owner: Owner,
    repo: Repository,
    branch: Annotated[
        BranchName,
        Field(description="Existing branch to advance conditionally."),
    ],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact branch head SHA required before patch materialization."),
    ],
    patches: PublicFilePatches,
    commit_message: Annotated[
        CommitMessage,
        Field(description="Git commit message."),
    ],
    *,
    ctx: Context[AppContext],
) -> PatchFilesResult:
    """Delegate one exact-context patch request to the canonical implementation."""

    return await _gh_patch_files(
        owner,
        repo,
        branch,
        expected_head_sha,
        patches,
        commit_message,
        ctx=ctx,
    )


PATCH_WRITE_METADATA = WriteToolMetadata(
    "Patch repository files atomically",
    (
        "Destructive write: apply bounded exact-context replacements to existing regular or "
        "executable UTF-8 files in one Git commit, only while the target branch remains at "
        "expected_head_sha. Every old_text must occur exactly once in the original file "
        "snapshot and source spans must not overlap. All patches are validated before any "
        "Git object is created; file modes are preserved exactly. Ordinary write "
        "authorization and the content-commit fine gate are required. The branch advance "
        "reuses the canonical single exact compare-and-swap and bounded authoritative "
        "readback contract; it cannot create/delete/rename files, patch symlinks or binary "
        "content, force-update a ref, use fuzzy matching, or retry an ambiguous mutation."
    ),
    MUTATE_EXTERNAL,
)
