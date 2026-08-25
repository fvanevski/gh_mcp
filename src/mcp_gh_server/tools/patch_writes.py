"Canonical exact-context partial repository file write."

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from mcp.server.mcpserver import Context
from pydantic import Field

from ..content_commit_service import (
    ContentCommitBase,
    commit_materialized_files,
    prepare_content_commit_base,
    read_exact_branch_head,
)
from ..models import CommitFile
from ..patch_models import FilePatch, PatchFileEvidence, PatchFilesResult
from ..tooling import (
    OBJECT_SHA_RE,
    AppContext,
    app_from_context,
    logger,
    require_write_enabled,
    validate_branch,
    validate_repo_path,
)
from .content_writes import _validate_commit_message


@dataclass(frozen=True, slots=True)
class _ResolvedPatchFile:
    path: str
    mode: Literal["100644", "100755"]
    before_blob_sha: str
    materialized_content: str
    edit_count: int


def _find_all_occurrences(content: str, needle: str) -> list[int]:
    starts: list[int] = []
    cursor = 0
    while True:
        index = content.find(needle, cursor)
        if index < 0:
            return starts
        starts.append(index)
        cursor = index + 1


def _materialize_edits(path: str, original: str, patch: FilePatch) -> tuple[str, int]:
    spans: list[tuple[int, int, str]] = []
    for edit_index, edit in enumerate(patch.edits, start=1):
        starts = _find_all_occurrences(original, edit.old_text)
        if not starts:
            raise ValueError(
                f"patch {path!r} edit {edit_index} old_text was not found in the original file"
            )
        if len(starts) != 1:
            raise ValueError(
                f"patch {path!r} edit {edit_index} old_text occurs {len(starts)} times "
                "in the original file; exact-context edits require exactly one occurrence"
            )
        start = starts[0]
        spans.append((start, start + len(edit.old_text), edit.new_text))

    ordered = sorted(spans, key=lambda item: (item[0], item[1]))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current[0] < previous[1]:
            raise ValueError(
                f"patch {path!r} contains overlapping exact-context source spans"
            )

    materialized = original
    for start, end, replacement in reversed(ordered):
        materialized = materialized[:start] + replacement + materialized[end:]
    return materialized, len(spans)


def _tree_entries(result: object, tree_sha: str) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise RuntimeError(f"GitHub returned a non-object tree for {tree_sha}")
    if result.get("truncated") is True:
        raise RuntimeError(
            f"GitHub tree {tree_sha} was truncated; exact patch path resolution is unavailable"
        )
    raw_entries = result.get("tree")
    if not isinstance(raw_entries, list):
        raise RuntimeError(f"GitHub did not return tree entries for {tree_sha}")
    entries: list[dict[str, Any]] = []
    for entry in raw_entries:
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


async def _resolve_patch_target(
    app: AppContext,
    owner: str,
    repo: str,
    base_tree_sha: str,
    path: str,
    tree_cache: dict[str, list[dict[str, Any]]],
) -> tuple[str, Literal["100644", "100755"]]:
    """Resolve an existing exact-snapshot file to its blob SHA and preserved mode."""

    components = path.split("/")
    tree_sha = base_tree_sha
    for index, component in enumerate(components):
        entries = tree_cache.get(tree_sha)
        if entries is None:
            result = await app.client.run(
                "api",
                f"repos/{owner}/{repo}/git/trees/{tree_sha}",
            )
            entries = _tree_entries(result, tree_sha)
            tree_cache[tree_sha] = entries

        matches = [entry for entry in entries if entry.get("path") == component]
        if len(matches) != 1:
            if not matches:
                raise ValueError(f"patch target {path!r} does not exist at the expected head")
            raise RuntimeError(
                f"GitHub returned ambiguous tree entries while resolving patch target {path!r}"
            )
        entry = matches[0]
        entry_type = entry.get("type")
        entry_sha = entry.get("sha")
        entry_mode = entry.get("mode")
        if not isinstance(entry_sha, str) or not OBJECT_SHA_RE.fullmatch(entry_sha):
            raise RuntimeError(f"GitHub returned an invalid object SHA for patch target {path!r}")
        entry_sha = entry_sha.casefold()

        is_final = index == len(components) - 1
        if not is_final:
            if entry_type != "tree" or entry_mode != "040000":
                raise ValueError(
                    f"patch target {path!r} does not exist as a file at the expected head"
                )
            tree_sha = entry_sha
            continue

        if entry_type != "blob":
            raise ValueError(f"patch target {path!r} is not an existing file")
        if entry_mode not in {"100644", "100755"}:
            raise ValueError(
                f"patch target {path!r} uses unsupported Git mode {entry_mode!r}; "
                "only regular and executable UTF-8 text files may be patched"
            )
        return entry_sha, cast(Literal["100644", "100755"], entry_mode)

    raise AssertionError("patch path traversal did not resolve a final entry")


async def _read_utf8_blob(
    app: AppContext,
    owner: str,
    repo: str,
    path: str,
    blob_sha: str,
) -> str:
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/git/blobs/{blob_sha}",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"GitHub returned a non-object blob for patch target {path!r}")
    returned_sha = result.get("sha")
    if (
        not isinstance(returned_sha, str)
        or not OBJECT_SHA_RE.fullmatch(returned_sha)
        or returned_sha.casefold() != blob_sha
    ):
        raise RuntimeError(f"GitHub blob identity mismatch for patch target {path!r}")
    if result.get("encoding") != "base64":
        raise ValueError(
            f"patch target {path!r} is not available as a supported UTF-8 text blob"
        )
    encoded = result.get("content")
    if not isinstance(encoded, str):
        raise RuntimeError(f"GitHub did not return blob content for patch target {path!r}")
    try:
        raw = base64.b64decode("".join(encoded.split()), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError(f"GitHub returned invalid base64 for patch target {path!r}") from exc
    if b"\x00" in raw:
        raise ValueError(f"patch target {path!r} appears to be binary and is unsupported")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"patch target {path!r} is not valid UTF-8 text") from exc


async def _resolve_all_patches(
    app: AppContext,
    owner: str,
    repo: str,
    base: ContentCommitBase,
    patches: list[FilePatch],
) -> list[_ResolvedPatchFile]:
    tree_cache: dict[str, list[dict[str, Any]]] = {}
    resolved: list[_ResolvedPatchFile] = []
    aggregate_materialized_bytes = 0

    for patch in patches:
        blob_sha, mode = await _resolve_patch_target(
            app,
            owner,
            repo,
            base.base_tree_sha,
            patch.path,
            tree_cache,
        )
        original = await _read_utf8_blob(app, owner, repo, patch.path, blob_sha)
        original_bytes = len(original.encode())
        if original_bytes > app.settings.max_file_bytes:
            raise ValueError(
                f"file {patch.path!r} exceeds MCP_GH_MAX_FILE_BYTES="
                f"{app.settings.max_file_bytes}"
            )

        materialized, edit_count = _materialize_edits(patch.path, original, patch)
        if materialized == original:
            raise ValueError(f"patch {patch.path!r} does not change the original file")
        materialized_bytes = len(materialized.encode())
        if materialized_bytes > app.settings.max_file_bytes:
            raise ValueError(
                f"patched file {patch.path!r} exceeds MCP_GH_MAX_FILE_BYTES="
                f"{app.settings.max_file_bytes}"
            )
        aggregate_materialized_bytes += materialized_bytes
        resolved.append(
            _ResolvedPatchFile(
                path=patch.path,
                mode=mode,
                before_blob_sha=blob_sha,
                materialized_content=materialized,
                edit_count=edit_count,
            )
        )

    if aggregate_materialized_bytes > app.settings.max_commit_bytes:
        raise ValueError(
            "patched file contents exceed "
            f"MCP_GH_MAX_COMMIT_BYTES={app.settings.max_commit_bytes}"
        )
    return resolved


def _validate_patch_request(app: AppContext, patches: list[FilePatch]) -> None:
    if not patches:
        raise ValueError("patches must contain at least one file patch")
    if len(patches) > app.settings.max_commit_files:
        raise ValueError(
            f"patches exceeds MCP_GH_MAX_COMMIT_FILES={app.settings.max_commit_files}"
        )

    paths: set[str] = set()
    aggregate_patch_bytes = 0
    for patch in patches:
        validate_repo_path(patch.path)
        if patch.path in paths:
            raise ValueError(f"duplicate patch path: {patch.path!r}")
        paths.add(patch.path)
        if not patch.edits:
            raise ValueError(f"patch {patch.path!r} must contain at least one edit")
        for edit_index, edit in enumerate(patch.edits, start=1):
            if not edit.old_text:
                raise ValueError(
                    f"patch {patch.path!r} edit {edit_index} old_text must not be empty"
                )
            old_bytes = len(edit.old_text.encode())
            new_bytes = len(edit.new_text.encode())
            if old_bytes > app.settings.max_file_bytes:
                raise ValueError(
                    f"patch {patch.path!r} edit {edit_index} old_text exceeds "
                    f"MCP_GH_MAX_FILE_BYTES={app.settings.max_file_bytes}"
                )
            if new_bytes > app.settings.max_file_bytes:
                raise ValueError(
                    f"patch {patch.path!r} edit {edit_index} new_text exceeds "
                    f"MCP_GH_MAX_FILE_BYTES={app.settings.max_file_bytes}"
                )
            aggregate_patch_bytes += old_bytes + new_bytes

    if aggregate_patch_bytes > app.settings.max_commit_bytes:
        raise ValueError(
            "patch text exceeds "
            f"MCP_GH_MAX_COMMIT_BYTES={app.settings.max_commit_bytes}"
        )


async def gh_patch_files(
    owner: Annotated[str, Field(min_length=1)],
    repo: Annotated[str, Field(min_length=1)],
    branch: Annotated[str, Field(min_length=1)],
    expected_head_sha: Annotated[str, Field(pattern=r"^[0-9A-Fa-f]{40}$")],
    patches: Annotated[list[FilePatch], Field(min_length=1, max_length=1000)],
    commit_message: Annotated[str, Field(min_length=1, max_length=65_536)],
    *,
    ctx: Context[AppContext],
) -> PatchFilesResult:
    """Apply bounded exact-context edits to existing UTF-8 files in one exact-head commit."""

    logger.info("MCP tool invocation reached server: tool=gh_patch_files")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="content_commit")
    validate_branch(branch)
    if not OBJECT_SHA_RE.fullmatch(expected_head_sha):
        raise ValueError("expected_head_sha must be a full 40-character Git object SHA")
    _validate_commit_message(commit_message)
    _validate_patch_request(app, patches)

    base = await prepare_content_commit_base(
        app,
        owner,
        repo,
        branch,
        expected_head_sha,
    )
    resolved = await _resolve_all_patches(app, owner, repo, base, patches)

    # Patch validation may require several immutable reads. Recheck the mutable branch
    # immediately before creating the first Git object so a head change cannot leave
    # avoidable orphan blobs/tree/commit objects.
    final_prewrite_head = await read_exact_branch_head(app, owner, repo, branch)
    if final_prewrite_head != base.previous_head_sha:
        raise RuntimeError(
            f"Branch {branch!r} head changed during exact-context patch validation: "
            f"expected {base.previous_head_sha}, found {final_prewrite_head}; "
            "no commit objects were created"
        )

    materialized_files = [
        CommitFile(
            path=file.path,
            content=file.materialized_content,
            mode=file.mode,
        )
        for file in resolved
    ]
    committed = await commit_materialized_files(
        app,
        owner,
        repo,
        branch,
        base,
        materialized_files,
        commit_message,
    )

    evidence = [
        PatchFileEvidence(
            path=file.path,
            mode=file.mode,
            before_blob_sha=file.before_blob_sha,
            after_blob_sha=committed.blob_shas[file.path],
        )
        for file in resolved
    ]
    edit_count = sum(file.edit_count for file in resolved)
    changed_paths = [file.path for file in resolved]
    prefix = (
        f"Applied {edit_count} exact-context edit(s) across "
        f"{len(resolved)} file(s)."
    )

    return PatchFilesResult(
        branch=branch,
        previous_head_sha=committed.previous_head_sha,
        commit_sha=committed.commit_sha,
        tree_sha=committed.tree_sha,
        ref_updated=committed.ref_updated,
        observed_head_sha=committed.observed_head_sha,
        readback_attempts=committed.readback_attempts,
        changed_file_count=len(resolved),
        applied_edit_count=edit_count,
        changed_paths=changed_paths,
        files=evidence,
        url=committed.url,
        message=f"{prefix} {committed.message}",
        **committed.outcome.model_dump(),
    )
