"""Read-only exact commit comparison with independently bounded evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..compare_commits_models import (
    CommitComparisonResult,
    ComparedCommit,
    ComparedFile,
    ComparisonCollectionEvidence,
    ComparisonStatus,
)
from ..evidence import bound_text_evidence
from ..request_governor import GitHubRequestError
from ..tooling import (
    OWNER_RE,
    READ_EXTERNAL,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repository,
)
from .git import gh_get_commit

_COMPARE_COMMIT_API_MAX = 100
_COMPARE_FILE_API_MAX = 300
# GitHub does not expose a total changed-file count and saturates the first-page file list
# at 300. Returning at most 299 makes an upstream 300-item response provably incomplete.
_COMPARE_FILE_PROVABLE_MAX = _COMPARE_FILE_API_MAX - 1

_COMPARE_JQ = """{
  status: .status,
  ahead_by: .ahead_by,
  behind_by: .behind_by,
  total_commits: .total_commits,
  base_commit: {sha: .base_commit.sha},
  merge_base_commit: {sha: .merge_base_commit.sha},
  commits: [(.commits // [])[] | {
    sha: .sha,
    html_url: .html_url,
    commit: {message: .commit.message, author: .commit.author, committer: .commit.committer},
    author: (if .author == null then null else {login: .author.login} end),
    committer: (if .committer == null then null else {login: .committer.login} end)
  }],
  files: [(.files // [])[] | {
    sha: .sha,
    filename: .filename,
    status: .status,
    additions: .additions,
    deletions: .deletions,
    changes: .changes,
    previous_filename: .previous_filename,
    blob_url: .blob_url,
    raw_url: .raw_url,
    contents_url: .contents_url
  }]
}"""


def _canonical_sha256(value: Any) -> str:
    """Digest one JSON-safe returned-evidence value using a stable canonical encoding."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_sha(raw: object, *, label: str) -> str:
    """Validate and normalize one exact SHA returned by GitHub."""

    if not isinstance(raw, str) or len(raw) != 40:
        raise RuntimeError(f"GitHub comparison returned no exact 40-character {label} SHA")
    try:
        int(raw, 16)
    except ValueError as error:
        raise RuntimeError(f"GitHub comparison returned a non-hexadecimal {label} SHA") from error
    return raw.casefold()


def _nonnegative_int(raw: object, *, label: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise RuntimeError(f"GitHub comparison returned an invalid {label}")
    return raw


def _optional_string(raw: object) -> str | None:
    return raw if isinstance(raw, str) else None


def _resolve_limit(
    requested: int | None,
    *,
    default: int,
    server_hard_max: int,
    upstream_max: int,
    label: str,
) -> tuple[int, list[str]]:
    requested_value = default if requested is None else requested
    hard_limit = min(server_hard_max, upstream_max)
    limit = min(requested_value, hard_limit)
    warnings: list[str] = []
    if requested_value > hard_limit:
        warnings.append(
            f"Requested max_{label}={requested_value} was capped at the effective hard limit "
            f"of {hard_limit}."
        )
    return limit, warnings


def _parse_commit(raw: object, *, message_max_bytes: int) -> ComparedCommit:
    if not isinstance(raw, dict):
        raise RuntimeError("GitHub comparison returned a malformed commit record")
    sha = _exact_sha(raw.get("sha"), label="commit")
    url = raw.get("html_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"GitHub comparison returned no URL for commit {sha}")

    commit = raw.get("commit")
    if not isinstance(commit, dict):
        raise RuntimeError(f"GitHub comparison returned no commit metadata for {sha}")
    message = commit.get("message")
    if not isinstance(message, str):
        raise RuntimeError(f"GitHub comparison returned no commit message for {sha}")
    message_evidence = bound_text_evidence(
        message,
        requested_max_bytes=message_max_bytes,
        hard_max_bytes=message_max_bytes,
        label=f"Commit {sha} message",
    )

    author_identity = commit.get("author")
    committer_identity = commit.get("committer")
    author = raw.get("author")
    committer = raw.get("committer")
    author_dict = author_identity if isinstance(author_identity, dict) else {}
    committer_dict = committer_identity if isinstance(committer_identity, dict) else {}
    author_account = author if isinstance(author, dict) else {}
    committer_account = committer if isinstance(committer, dict) else {}

    return ComparedCommit(
        sha=sha,
        message=message_evidence.content,
        message_truncated=message_evidence.truncated,
        message_bytes_returned=message_evidence.bytes_returned,
        message_sha256=message_evidence.sha256,
        author_login=_optional_string(author_account.get("login")),
        author_name=_optional_string(author_dict.get("name")),
        authored_at=_optional_string(author_dict.get("date")),
        committer_login=_optional_string(committer_account.get("login")),
        committer_name=_optional_string(committer_dict.get("name")),
        committed_at=_optional_string(committer_dict.get("date")),
        url=url,
    )


def _parse_file(raw: object) -> ComparedFile:
    if not isinstance(raw, dict):
        raise RuntimeError("GitHub comparison returned a malformed file record")
    filename = raw.get("filename")
    status = raw.get("status")
    if not isinstance(filename, str) or not filename:
        raise RuntimeError("GitHub comparison returned a file without a filename")
    if not isinstance(status, str) or not status:
        raise RuntimeError(f"GitHub comparison returned no status for file {filename}")

    raw_sha = raw.get("sha")
    sha = _exact_sha(raw_sha, label="file blob") if raw_sha is not None else None
    return ComparedFile(
        filename=filename,
        status=status,
        additions=_nonnegative_int(raw.get("additions"), label=f"additions for {filename}"),
        deletions=_nonnegative_int(raw.get("deletions"), label=f"deletions for {filename}"),
        changes=_nonnegative_int(raw.get("changes"), label=f"changes for {filename}"),
        sha=sha,
        previous_filename=_optional_string(raw.get("previous_filename")),
        blob_url=_optional_string(raw.get("blob_url")),
        raw_url=_optional_string(raw.get("raw_url")),
        contents_url=_optional_string(raw.get("contents_url")),
    )


def _collection_evidence(
    items: list[ComparedCommit] | list[ComparedFile],
    *,
    total_count: int | None,
    truncated: bool,
    complete: bool,
    warnings: list[str],
) -> ComparisonCollectionEvidence:
    payload = [item.model_dump(mode="json") for item in items]
    return ComparisonCollectionEvidence(
        returned_count=len(items),
        total_count=total_count,
        complete=complete,
        truncated=truncated,
        sha256=_canonical_sha256(payload),
        warning=" ".join(warnings) or None,
    )


def _missing_result(
    *,
    base_sha: str,
    head_sha: str,
    base_found: bool,
    head_found: bool,
) -> CommitComparisonResult:
    missing: list[str] = []
    if not base_found:
        missing.append("base")
    if not head_found:
        missing.append("head")
    warning = (
        "Comparison unavailable because the exact "
        + " and ".join(missing)
        + " commit SHA"
        + ("s are" if len(missing) > 1 else " is")
        + " missing."
    )
    empty_digest = _canonical_sha256([])
    collection = ComparisonCollectionEvidence(
        returned_count=0,
        total_count=None,
        complete=False,
        truncated=False,
        sha256=empty_digest,
        warning=warning,
    )
    digest = _canonical_sha256(
        {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "base_found": base_found,
            "head_found": head_found,
            "comparison_available": False,
        }
    )
    return CommitComparisonResult(
        base_sha=base_sha,
        head_sha=head_sha,
        base_found=base_found,
        head_found=head_found,
        comparison_available=False,
        commits=[],
        commits_evidence=collection,
        files=[],
        files_evidence=collection.model_copy(deep=True),
        truncated=False,
        evidence_complete=False,
        sha256=digest,
        warning=warning,
    )


async def _classify_compare_404(
    *,
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    ctx: Context[AppContext],
    original_error: GitHubRequestError,
) -> CommitComparisonResult:
    """Use the established exact-commit contract before treating compare 404 as absence."""

    base_commit = await gh_get_commit(owner, repo, base_sha, ctx=ctx)
    if head_sha == base_sha:
        head_commit = base_commit
    else:
        head_commit = await gh_get_commit(owner, repo, head_sha, ctx=ctx)
    if base_commit.found and head_commit.found:
        raise original_error
    return _missing_result(
        base_sha=base_sha,
        head_sha=head_sha,
        base_found=base_commit.found,
        head_found=head_commit.found,
    )


@mcp.tool(
    title="Compare exact Git commits",
    description=(
        "Read-only: compare two exact 40-character commit SHAs without branch or tag "
        "resolution. Returns explicit merge-base/status evidence plus independently bounded "
        "commit and changed-file metadata with completeness and SHA-256 fingerprints."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_compare_commits(
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
    base_sha: Annotated[
        str,
        Field(
            description="Exact 40-character hexadecimal base commit SHA.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    head_sha: Annotated[
        str,
        Field(
            description="Exact 40-character hexadecimal head commit SHA.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    *,
    ctx: Context[AppContext],
    max_commits: Annotated[
        int | None,
        Field(
            description=(
                "Maximum commit records to return. Defaults to the server result limit and "
                "cannot exceed GitHub's 100-commit comparison page limit."
            ),
            ge=1,
        ),
    ] = None,
    max_files: Annotated[
        int | None,
        Field(
            description=(
                "Maximum changed-file metadata records to return. Defaults to the server "
                "result limit; the implementation retains a strict bound below GitHub's "
                "300-file upstream saturation point so incompleteness is never ambiguous."
            ),
            ge=1,
        ),
    ] = None,
) -> CommitComparisonResult:
    """Compare two immutable commits while preserving bounded-evidence semantics."""

    logger.info("MCP tool invocation reached server: tool=gh_compare_commits")
    validate_repository(owner, repo)
    normalized_base = base_sha.casefold()
    normalized_head = head_sha.casefold()
    app = app_from_context(ctx)

    commit_limit, commit_warnings = _resolve_limit(
        max_commits,
        default=app.settings.default_max_results,
        server_hard_max=app.settings.hard_max_results,
        upstream_max=_COMPARE_COMMIT_API_MAX,
        label="commits",
    )
    file_limit, file_warnings = _resolve_limit(
        max_files,
        default=app.settings.default_max_results,
        server_hard_max=app.settings.hard_max_results,
        upstream_max=_COMPARE_FILE_PROVABLE_MAX,
        label="files",
    )

    try:
        payload = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/compare/{normalized_base}...{normalized_head}",
            "-X",
            "GET",
            "-f",
            "page=1",
            "-f",
            f"per_page={commit_limit}",
            "--jq",
            _COMPARE_JQ,
        )
    except GitHubRequestError as error:
        if error.status_code != 404:
            raise
        return await _classify_compare_404(
            owner=owner,
            repo=repo,
            base_sha=normalized_base,
            head_sha=normalized_head,
            ctx=ctx,
            original_error=error,
        )

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub commit comparison returned a non-object response")

    base_commit = payload.get("base_commit")
    returned_base = _exact_sha(
        base_commit.get("sha") if isinstance(base_commit, dict) else None,
        label="base commit",
    )
    if returned_base != normalized_base:
        raise RuntimeError(
            "GitHub comparison did not preserve the requested base commit SHA; "
            "refusing ambiguous evidence"
        )

    merge_base = payload.get("merge_base_commit")
    merge_base_sha = _exact_sha(
        merge_base.get("sha") if isinstance(merge_base, dict) else None,
        label="merge-base commit",
    )
    status = payload.get("status")
    if status not in {"identical", "ahead", "behind", "diverged"}:
        raise RuntimeError("GitHub comparison returned an unsupported comparison status")
    comparison_status: ComparisonStatus = status
    ahead_by = _nonnegative_int(payload.get("ahead_by"), label="ahead_by count")
    behind_by = _nonnegative_int(payload.get("behind_by"), label="behind_by count")
    total_commits = _nonnegative_int(payload.get("total_commits"), label="total_commits count")

    raw_commits = payload.get("commits")
    if not isinstance(raw_commits, list):
        raise RuntimeError("GitHub comparison returned no commit collection")
    if len(raw_commits) > total_commits:
        raise RuntimeError("GitHub comparison returned more commits than total_commits")
    commits = [
        _parse_commit(item, message_max_bytes=app.settings.max_pr_commit_message_bytes)
        for item in raw_commits[:commit_limit]
    ]
    commit_messages_truncated = any(item.message_truncated for item in commits)
    commits_truncated = len(commits) < total_commits or commit_messages_truncated
    if len(commits) < total_commits:
        commit_warnings.append(
            f"Commit evidence was truncated: returned {len(commits)} of {total_commits} commits."
        )
    if commit_messages_truncated:
        commit_warnings.append(
            "At least one returned commit message was byte-truncated; each message_sha256 "
            "fingerprints its complete source message."
        )
    commits_evidence = _collection_evidence(
        commits,
        total_count=total_commits,
        truncated=commits_truncated,
        complete=not commits_truncated,
        warnings=commit_warnings,
    )

    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise RuntimeError("GitHub comparison returned no changed-file collection")
    files = [_parse_file(item) for item in raw_files[:file_limit]]
    upstream_file_saturated = len(raw_files) >= _COMPARE_FILE_API_MAX
    files_total_count = None if upstream_file_saturated else len(raw_files)
    files_truncated = len(raw_files) > file_limit or upstream_file_saturated
    if len(raw_files) > file_limit:
        file_warnings.append(
            f"File evidence was truncated: returned {len(files)} of "
            + (f"{len(raw_files)} files." if not upstream_file_saturated else "at least 300 files.")
        )
    if upstream_file_saturated:
        file_warnings.append(
            "GitHub saturated the comparison file list at its 300-file upstream limit; "
            "the complete changed-file count is therefore unknown."
        )
    files_evidence = _collection_evidence(
        files,
        total_count=files_total_count,
        truncated=files_truncated,
        complete=not files_truncated,
        warnings=file_warnings,
    )

    returned_evidence = {
        "base_sha": normalized_base,
        "head_sha": normalized_head,
        "base_found": True,
        "head_found": True,
        "comparison_available": True,
        "merge_base_sha": merge_base_sha,
        "status": comparison_status,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "total_commits": total_commits,
        "commits": [item.model_dump(mode="json") for item in commits],
        "commits_evidence": {
            "returned_count": commits_evidence.returned_count,
            "total_count": commits_evidence.total_count,
            "complete": commits_evidence.complete,
            "truncated": commits_evidence.truncated,
            "sha256": commits_evidence.sha256,
        },
        "files": [item.model_dump(mode="json") for item in files],
        "files_evidence": {
            "returned_count": files_evidence.returned_count,
            "total_count": files_evidence.total_count,
            "complete": files_evidence.complete,
            "truncated": files_evidence.truncated,
            "sha256": files_evidence.sha256,
        },
    }
    warning_parts = [
        warning
        for warning in (commits_evidence.warning, files_evidence.warning)
        if warning is not None
    ]
    truncated = commits_evidence.truncated or files_evidence.truncated
    evidence_complete = commits_evidence.complete and files_evidence.complete
    return CommitComparisonResult(
        base_sha=normalized_base,
        head_sha=normalized_head,
        base_found=True,
        head_found=True,
        comparison_available=True,
        merge_base_sha=merge_base_sha,
        status=comparison_status,
        ahead_by=ahead_by,
        behind_by=behind_by,
        total_commits=total_commits,
        commits=commits,
        commits_evidence=commits_evidence,
        files=files,
        files_evidence=files_evidence,
        truncated=truncated,
        evidence_complete=evidence_complete,
        sha256=_canonical_sha256(returned_evidence),
        warning=" ".join(warning_parts) or None,
    )
