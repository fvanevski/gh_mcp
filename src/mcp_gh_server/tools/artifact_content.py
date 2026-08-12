"""Safe bounded inspection of GitHub Actions artifact ZIP contents."""

from __future__ import annotations

import hashlib
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated

from mcp.server.mcpserver import Context
from pydantic import Field

from ..artifact_content_models import (
    ArtifactArchiveEvidence,
    ArtifactFileContent,
    ArtifactFileEntry,
    ArtifactFilesPage,
)
from ..binary_evidence import stream_governed_bytes
from ..evidence import bound_text_evidence, pagination_evidence
from ..models import WorkflowArtifact
from ..tooling import (
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repository,
)
from .artifacts import _artifact_from_payload

_MAX_ARTIFACT_ARCHIVE_BYTES = 25_000_000
_MAX_ARTIFACT_UNCOMPRESSED_BYTES = 50_000_000
_MAX_ARTIFACT_FILE_BYTES = 1_000_000
_MAX_ARTIFACT_ENTRIES = 1_000
_GITHUB_ARTIFACT_FILES_PER_PAGE_MAX = 100
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class _ArchiveDownload:
    path: Path
    bytes_downloaded: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _ArchiveInspection:
    files: tuple[ArtifactFileEntry, ...]
    by_path: dict[str, zipfile.ZipInfo]


async def _get_artifact_metadata(
    app: AppContext,
    owner: str,
    repo: str,
    artifact_id: int,
) -> WorkflowArtifact:
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/artifacts/{artifact_id}",
        "-X",
        "GET",
    )
    return _artifact_from_payload(result, expected_artifact_id=artifact_id)


def _require_retrievable_artifact(artifact: WorkflowArtifact) -> None:
    if artifact.expired:
        raise RuntimeError(
            f"Artifact {artifact.id} is expired; artifact content cannot be retrieved"
        )
    if artifact.size_in_bytes > _MAX_ARTIFACT_ARCHIVE_BYTES:
        raise RuntimeError(
            f"Artifact {artifact.id} reports {artifact.size_in_bytes} bytes, exceeding "
            f"the {_MAX_ARTIFACT_ARCHIVE_BYTES}-byte archive hard limit"
        )


async def _download_archive(
    app: AppContext,
    owner: str,
    repo: str,
    artifact: WorkflowArtifact,
    destination: Path,
) -> _ArchiveDownload:
    downloaded = 0
    hasher = hashlib.sha256()

    with destination.open("wb") as output:

        def consume(chunk: bytes) -> None:
            nonlocal downloaded
            next_size = downloaded + len(chunk)
            if next_size > _MAX_ARTIFACT_ARCHIVE_BYTES:
                raise RuntimeError(
                    f"Artifact archive exceeded the {_MAX_ARTIFACT_ARCHIVE_BYTES}-byte hard limit"
                )
            output.write(chunk)
            hasher.update(chunk)
            downloaded = next_size

        await stream_governed_bytes(
            app.client,
            "api",
            f"repos/{owner}/{repo}/actions/artifacts/{artifact.id}/zip",
            "-X",
            "GET",
            on_chunk=consume,
        )

    return _ArchiveDownload(
        path=destination,
        bytes_downloaded=downloaded,
        sha256=hasher.hexdigest(),
    )


def _normalize_archive_path(raw_path: str) -> str:
    if not raw_path or "\x00" in raw_path:
        raise RuntimeError("Artifact archive contains an empty or NUL-containing path")

    candidate = raw_path.replace("\\", "/")
    if candidate.startswith("/") or _DRIVE_PREFIX_RE.match(candidate):
        raise RuntimeError(f"Artifact archive contains an absolute path: {raw_path!r}")

    parts: list[str] = []
    for part in candidate.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise RuntimeError(f"Artifact archive contains path traversal: {raw_path!r}")
        parts.append(part)

    if not parts:
        raise RuntimeError(f"Artifact archive contains an invalid root-only path: {raw_path!r}")
    return "/".join(parts)


def _require_normalized_requested_path(path: str) -> str:
    try:
        normalized = _normalize_archive_path(path)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    if normalized != path:
        raise ValueError(
            "path must be an exact normalized archive path using forward slashes, "
            "without '.', repeated separators, traversal, or an absolute prefix"
        )
    return normalized


def _entry_kind(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x1:
        raise RuntimeError(f"Artifact archive entry is encrypted: {info.filename!r}")

    if info.create_system == 3:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise RuntimeError(
                f"Artifact archive contains a symbolic link, which is not inspectable: "
                f"{info.filename!r}"
            )
        file_type = stat.S_IFMT(mode)
        if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise RuntimeError(
                f"Artifact archive contains a non-regular special entry: {info.filename!r}"
            )

    return "directory" if info.is_dir() else "file"


def _inspect_archive(path: Path) -> _ArchiveInspection:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("GitHub artifact content is not a valid ZIP archive") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ARTIFACT_ENTRIES:
            raise RuntimeError(
                f"Artifact archive contains {len(infos)} entries, exceeding the "
                f"{_MAX_ARTIFACT_ENTRIES}-entry hard limit"
            )

        explicit: dict[str, str] = {}
        implied_directories: set[str] = set()
        files: list[ArtifactFileEntry] = []
        by_path: dict[str, zipfile.ZipInfo] = {}
        total_uncompressed = 0

        for info in infos:
            normalized = _normalize_archive_path(info.filename)
            kind = _entry_kind(info)

            if normalized in explicit:
                raise RuntimeError(
                    f"Artifact archive contains duplicate/conflicting normalized path: "
                    f"{normalized!r}"
                )

            parents = tuple(PurePosixPath(normalized).parents)
            for parent in parents:
                parent_text = parent.as_posix()
                if parent_text == ".":
                    continue
                if explicit.get(parent_text) == "file":
                    raise RuntimeError(
                        f"Artifact archive path conflict: file {parent_text!r} is an ancestor "
                        f"of {normalized!r}"
                    )
                implied_directories.add(parent_text)

            if kind == "file" and normalized in implied_directories:
                raise RuntimeError(
                    f"Artifact archive path conflict: {normalized!r} is both a file and directory"
                )

            explicit[normalized] = kind
            if kind == "directory":
                continue

            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_ARTIFACT_UNCOMPRESSED_BYTES:
                raise RuntimeError(
                    "Artifact archive uncompressed size exceeds the "
                    f"{_MAX_ARTIFACT_UNCOMPRESSED_BYTES}-byte hard limit"
                )

            files.append(
                ArtifactFileEntry(
                    path=normalized,
                    size_in_bytes=info.file_size,
                    compressed_size_in_bytes=info.compress_size,
                )
            )
            by_path[normalized] = info

        files.sort(key=lambda entry: entry.path)
        return _ArchiveInspection(files=tuple(files), by_path=by_path)


def _read_utf8_file(
    archive_path: Path,
    info: zipfile.ZipInfo,
) -> str:
    if info.file_size > _MAX_ARTIFACT_FILE_BYTES:
        raise RuntimeError(
            f"Artifact file {info.filename!r} reports {info.file_size} bytes, exceeding "
            f"the {_MAX_ARTIFACT_FILE_BYTES}-byte file hard limit"
        )

    chunks: list[bytes] = []
    total = 0
    try:
        with zipfile.ZipFile(archive_path) as archive, archive.open(info, "r") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_ARTIFACT_FILE_BYTES:
                    raise RuntimeError(
                        f"Artifact file {info.filename!r} exceeded the "
                        f"{_MAX_ARTIFACT_FILE_BYTES}-byte file hard limit while reading"
                    )
                chunks.append(chunk)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Artifact ZIP integrity validation failed while reading the file") from exc

    if total != info.file_size:
        raise RuntimeError(
            f"Artifact file size mismatch for {info.filename!r}: ZIP metadata reports "
            f"{info.file_size}, read {total}"
        )

    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Artifact file {info.filename!r} is not valid UTF-8 text/JSON"
        ) from exc
    if "\x00" in text:
        raise RuntimeError(
            f"Artifact file {info.filename!r} contains NUL bytes and is not accepted as text/JSON"
        )
    return text


def _archive_evidence(
    artifact: WorkflowArtifact,
    download: _ArchiveDownload,
) -> ArtifactArchiveEvidence:
    return ArtifactArchiveEvidence(
        artifact_id=artifact.id,
        artifact_name=artifact.name,
        artifact_size_in_bytes=artifact.size_in_bytes,
        artifact_digest=artifact.digest,
        artifact_expires_at=artifact.expires_at,
        workflow_run_id=artifact.workflow_run_id,
        workflow_head_sha=artifact.workflow_head_sha,
        archive_bytes=download.bytes_downloaded,
        archive_sha256=download.sha256,
    )


@mcp.tool(
    title="List workflow artifact files",
    description=(
        "Read-only: inspect one exact, unexpired GitHub Actions artifact ZIP and return one "
        "bounded page of normalized regular-file paths and sizes. The archive is downloaded "
        "only into temporary server state, never extracted, and is rejected for traversal, "
        "absolute paths, duplicate/conflicting paths, symbolic links, special entries, "
        "encryption, or configured hard-limit violations. GitHub is never modified."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_artifact_files(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    artifact_id: Annotated[int, Field(ge=1, description="Exact workflow artifact identifier.")],
    *,
    ctx: Context[AppContext],
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based file page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="File paths per page, capped by server policy."),
    ] = None,
) -> ArtifactFilesPage:
    """Return bounded normalized file metadata from one exact unexpired artifact."""

    logger.info("MCP tool invocation reached server: tool=gh_list_artifact_files")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    artifact = await _get_artifact_metadata(app, owner, repo, artifact_id)
    _require_retrievable_artifact(artifact)

    with tempfile.TemporaryDirectory(prefix="mcp-gh-artifact-") as temp_dir:
        download = await _download_archive(
            app,
            owner,
            repo,
            artifact,
            Path(temp_dir) / "artifact.zip",
        )
        inspection = _inspect_archive(download.path)

        hard_per_page = min(
            app.settings.hard_max_results,
            _GITHUB_ARTIFACT_FILES_PER_PAGE_MAX,
        )
        requested_per_page = (
            app.settings.default_max_results if per_page is None else per_page
        )
        effective_per_page = min(requested_per_page, hard_per_page)
        start = (page - 1) * effective_per_page
        selected = list(inspection.files[start : start + effective_per_page])
        evidence = pagination_evidence(
            page=page,
            requested_per_page=per_page,
            default_per_page=app.settings.default_max_results,
            hard_max_results=hard_per_page,
            returned_count=len(selected),
            total_count=len(inspection.files),
        )
        identity = _archive_evidence(artifact, download)

        return ArtifactFilesPage(
            **identity.model_dump(),
            total_count=len(inspection.files),
            page=evidence.page,
            per_page=evidence.per_page,
            has_more=evidence.has_more,
            truncated=evidence.truncated,
            warning=evidence.warning,
            files=selected,
        )


@mcp.tool(
    title="Read workflow artifact text file",
    description=(
        "Read-only: retrieve one exact normalized regular-file path from one exact, "
        "unexpired GitHub Actions artifact. Only valid UTF-8 text/JSON is returned. "
        "Returned content is bounded by max_bytes and the server hard cap, while sha256 "
        "fingerprints the complete validated file. The ZIP is temporary and never extracted; "
        "GitHub is never modified."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_read_artifact_file(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    artifact_id: Annotated[int, Field(ge=1, description="Exact workflow artifact identifier.")],
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4096,
            description="Exact normalized artifact file path using forward slashes.",
        ),
    ],
    *,
    ctx: Context[AppContext],
    max_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description="Maximum returned UTF-8 bytes, capped by the server file hard limit.",
        ),
    ] = None,
) -> ArtifactFileContent:
    """Return bounded complete-file evidence for one exact normalized artifact path."""

    logger.info("MCP tool invocation reached server: tool=gh_read_artifact_file")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    normalized_path = _require_normalized_requested_path(path)
    artifact = await _get_artifact_metadata(app, owner, repo, artifact_id)
    _require_retrievable_artifact(artifact)

    with tempfile.TemporaryDirectory(prefix="mcp-gh-artifact-") as temp_dir:
        download = await _download_archive(
            app,
            owner,
            repo,
            artifact,
            Path(temp_dir) / "artifact.zip",
        )
        inspection = _inspect_archive(download.path)
        info = inspection.by_path.get(normalized_path)
        if info is None:
            raise RuntimeError(
                f"Artifact {artifact.id} does not contain normalized file path "
                f"{normalized_path!r}"
            )

        text = _read_utf8_file(download.path, info)
        evidence = bound_text_evidence(
            text,
            requested_max_bytes=max_bytes,
            hard_max_bytes=_MAX_ARTIFACT_FILE_BYTES,
            label=f"Artifact file {normalized_path!r}",
        )
        identity = _archive_evidence(artifact, download)
        return ArtifactFileContent(
            **identity.model_dump(),
            path=normalized_path,
            encoding="utf-8",
            content=evidence.content,
            bytes_returned=evidence.bytes_returned,
            total_bytes=evidence.total_bytes,
            truncated=evidence.truncated,
            sha256=evidence.sha256,
            warning=evidence.warning,
        )
